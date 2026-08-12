#!/usr/bin/env python3
"""Claude Quota collector + localhost server.

Single process, stdlib only (Python 3.9+):
  - A background poller thread reads the Claude Code OAuth token from the local
    credentials file, calls Anthropic's OAuth usage endpoint, normalizes the
    response, and atomically writes it to a JSON data file.
  - A ThreadingHTTPServer bound to 127.0.0.1 serves that JSON file at
    GET /latest.json (and GET /) so the iCUE widget (a pure renderer that never
    holds a token) can poll it locally.

See ../CLAUDE.md for the full project architecture and the normalized
latest.json contract.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# Config (env-overridable)
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("claude_quota")

_MIN_POLL_SECONDS = 900  # hard floor: the endpoint rate-limits aggressively


def _resolve_poll_seconds() -> int:
    raw = os.environ.get("CLAUDE_QUOTA_POLL_SECONDS", str(_MIN_POLL_SECONDS))
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "CLAUDE_QUOTA_POLL_SECONDS=%r is not an integer; using default %d",
            raw,
            _MIN_POLL_SECONDS,
        )
        return _MIN_POLL_SECONDS
    if value < _MIN_POLL_SECONDS:
        log.warning(
            "CLAUDE_QUOTA_POLL_SECONDS=%d is below the %d-second floor; clamping to %d",
            value,
            _MIN_POLL_SECONDS,
            _MIN_POLL_SECONDS,
        )
        return _MIN_POLL_SECONDS
    return value


PORT = int(os.environ.get("CLAUDE_QUOTA_PORT", "8765"))
POLL_SECONDS = _resolve_poll_seconds()
CREDENTIALS_PATH = Path(
    os.environ.get(
        "CLAUDE_CREDENTIALS_PATH",
        str(Path.home() / ".claude" / ".credentials.json"),
    )
).expanduser()
DATA_PATH = Path(
    os.environ.get("CLAUDE_QUOTA_DATA", str(SCRIPT_DIR / ".." / "data" / "latest.json"))
).expanduser()
# Same data-directory resolution as DATA_PATH (i.e. same folder as latest.json)
# so --diag writes alongside the normal data file. Overridable independently
# for tests via CLAUDE_QUOTA_DIAG.
DIAG_PATH = Path(
    os.environ.get("CLAUDE_QUOTA_DIAG", str(DATA_PATH.with_name("diag.json")))
).expanduser()
LOG_PATH = Path(
    os.environ.get("CLAUDE_QUOTA_LOG", str(SCRIPT_DIR / "collector.log"))
).expanduser()

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public PKCE client id
HTTP_TIMEOUT_SECONDS = 30

# Shared User-Agent for every outbound request to Anthropic. Without a
# realistic UA, urllib falls back to "Python-urllib/3.x", which Cloudflare's
# WAF in front of console.anthropic.com blocks with a 403 -- even for
# otherwise well-formed requests.
USER_AGENT = "claude-quota-widget/1.0"

# Disables all OAuth token-refresh behavior (both the proactive pre-expiry
# refresh and the on-401 refresh+retry) when set to "1"/"true". Escape hatch
# for debugging or environments where the credentials file is managed
# externally.
NO_REFRESH = os.environ.get("CLAUDE_QUOTA_NO_REFRESH", "").strip().lower() in (
    "1",
    "true",
)

# The Anthropic /api/oauth/usage endpoint is observed to report utilization as
# a percent (0-100). This flag is an escape hatch in case the API ever starts
# reporting utilization as a fraction (0-1) instead -- set to "1"/"true" to
# multiply raw values by 100 before clamping/rounding. Off by default.
CLAUDE_QUOTA_ASSUME_FRACTION = os.environ.get(
    "CLAUDE_QUOTA_ASSUME_FRACTION", ""
).strip().lower() in ("1", "true")

# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_utilization(raw: Any) -> Optional[float]:
    """Normalize a raw utilization value into a 0-100 float rounded to 1 decimal.

    Rule (documented, see CLAUDE.md): the Anthropic /api/oauth/usage endpoint is
    observed to report utilization as a percent (0-100), so raw values are
    treated as percents by default -- there is no fraction heuristic. A value
    like 0.5 means "0.5 percent", not "50 percent". If the API ever starts
    reporting fractions instead, set the CLAUDE_QUOTA_ASSUME_FRACTION env var to
    "1"/"true" to opt into multiplying raw values by 100 before clamping.
    Values are then clamped to [0, 100] and rounded to 1 decimal place.
    Non-numeric / missing / bool input returns None.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is a subclass of int; reject explicitly
        return None
    if not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if CLAUDE_QUOTA_ASSUME_FRACTION:
        value *= 100.0
    value = max(0.0, min(100.0, value))
    return round(value, 1)


def normalize_window(raw: Any) -> Optional[dict]:
    """Normalize one window object (e.g. the 'five_hour' or 'seven_day' key)."""
    if not isinstance(raw, dict):
        return None
    utilization = normalize_utilization(raw.get("utilization"))
    if utilization is None:
        return None
    resets_at = raw.get("resets_at")
    if resets_at is not None and not isinstance(resets_at, str):
        resets_at = str(resets_at)
    return {"utilization": utilization, "resets_at": resets_at}


_fractional_warning_logged = False


def _maybe_warn_fractional_utilization(
    five_hour: Optional[dict],
    seven_day: Optional[dict],
    five_hour_raw: Any,
    seven_day_raw: Any,
) -> None:
    """Log a one-time (per process) warning if utilization values look like
    fractions (<=1.0) while CLAUDE_QUOTA_ASSUME_FRACTION is off, which would
    otherwise silently under-report by ~100x. Does not change any values."""
    global _fractional_warning_logged
    if CLAUDE_QUOTA_ASSUME_FRACTION or _fractional_warning_logged:
        return
    if five_hour is None or seven_day is None:
        return

    def _raw_utilization(raw: Any) -> Any:
        return raw.get("utilization") if isinstance(raw, dict) else None

    def _looks_fractional(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 < value <= 1.0
        )

    if _looks_fractional(_raw_utilization(five_hour_raw)) and _looks_fractional(
        _raw_utilization(seven_day_raw)
    ):
        _fractional_warning_logged = True
        log.warning(
            "utilization values look fractional (<=1.0); if percentages seem "
            "~100x too low, set CLAUDE_QUOTA_ASSUME_FRACTION=1"
        )


def normalize_usage_payload(payload: Any) -> dict:
    """Normalize a raw /api/oauth/usage JSON payload into the widget contract.

    Raises ValueError if the payload cannot be interpreted (e.g. it's a list,
    or neither expected window key is present/parseable) so the caller can
    treat it as a fetch failure.
    """
    if not isinstance(payload, dict):
        raise ValueError("usage payload is not a JSON object")

    five_hour_raw = payload.get("five_hour")
    seven_day_raw = payload.get("seven_day")

    five_hour = normalize_window(five_hour_raw)
    seven_day = normalize_window(seven_day_raw)

    if five_hour is None and seven_day is None:
        raise ValueError("usage payload has no usable five_hour/seven_day window")

    _maybe_warn_fractional_utilization(five_hour, seven_day, five_hour_raw, seven_day_raw)

    return {
        "ok": True,
        "stale": False,
        "fetched_at": _utc_now_iso(),
        "five_hour": five_hour,
        "seven_day": seven_day,
    }


# --------------------------------------------------------------------------
# Credentials + HTTP fetch
# --------------------------------------------------------------------------


class FetchError(Exception):
    """Raised for any failure in reading credentials, refreshing the OAuth
    token, or calling the API.

    The message must never contain the token or any fragment of it.
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def read_credentials(path: Path) -> dict:
    """Read and parse the full Claude credentials JSON file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FetchError("credentials file not found")
    except OSError as exc:
        raise FetchError(f"credentials file unreadable ({exc.__class__.__name__})")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise FetchError("credentials file is not valid JSON")

    if not isinstance(data, dict):
        raise FetchError("credentials file is not a JSON object")

    return data


def _extract_oauth_field(data: Any, field: str) -> Any:
    """Best-effort read of data['claudeAiOauth'][field]; returns None if
    anything along the way is missing or the wrong shape."""
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    return oauth.get(field)


def _extract_access_token(data: dict) -> str:
    try:
        token = data["claudeAiOauth"]["accessToken"]
    except (KeyError, TypeError):
        raise FetchError("credentials file missing claudeAiOauth.accessToken")

    if not isinstance(token, str) or not token:
        raise FetchError("credentials file has an empty/invalid access token")

    return token


def read_access_token(path: Path) -> str:
    data = read_credentials(path)
    return _extract_access_token(data)


def _credential_state_fields(data: Any) -> dict:
    """Extract sanitized (no token text/prefix/fragment) fields describing
    OAuth credential state from a parsed credentials JSON object. Shared by
    describe_credentials() (one-line log summary) and the --diag JSON writer
    so the sanitization/parsing logic lives in exactly one place."""
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        oauth = {}

    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    access_present = isinstance(access_token, str) and bool(access_token)
    refresh_present = isinstance(refresh_token, str) and bool(refresh_token)
    access_len = len(access_token) if isinstance(access_token, str) else 0
    refresh_len = len(refresh_token) if isinstance(refresh_token, str) else 0

    expires_at = oauth.get("expiresAt")
    expires_at_iso: Optional[str] = None
    expired: Optional[bool] = None
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        try:
            expires_at_iso = datetime.fromtimestamp(
                expires_at / 1000.0, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            expires_at_iso = None
        expired = (time.time() * 1000) > expires_at

    return {
        "access_token_present": access_present,
        "access_token_length": access_len,
        "refresh_token_present": refresh_present,
        "refresh_token_length": refresh_len,
        "expires_at_iso": expires_at_iso,
        "expired": expired,
    }


def describe_credentials(data: Any) -> str:
    """Build a one-line, token-free summary of credential state suitable for
    logging on auth failures, e.g.:

        creds state: access_token=present(len=108) refresh_token=absent
        expires_at=2026-08-11T21:03:11Z(expired)

    Never includes any token text, prefix, or fragment.
    """
    fields = _credential_state_fields(data)

    if fields["access_token_present"]:
        access_desc = f"present(len={fields['access_token_length']})"
    else:
        access_desc = "absent"

    if fields["refresh_token_present"]:
        refresh_desc = f"present(len={fields['refresh_token_length']})"
    else:
        refresh_desc = "absent"

    if fields["expires_at_iso"]:
        expired_desc = "expired" if fields["expired"] else "valid"
        expires_desc = f"{fields['expires_at_iso']}({expired_desc})"
    else:
        expires_desc = "unknown"

    return (
        f"creds state: access_token={access_desc} refresh_token={refresh_desc} "
        f"expires_at={expires_desc}"
    )


def fetch_usage_payload(token: str) -> Any:
    """Perform the actual HTTP GET and return the parsed JSON body.

    Isolated as its own function (rather than inlined into the poll loop) so
    tests can monkeypatch it without touching urllib/network.
    """
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}", status_code=exc.code)
    except urllib.error.URLError as exc:
        raise FetchError(f"network error ({exc.reason.__class__.__name__})")
    except TimeoutError:
        raise FetchError("request timed out")

    if status != 200:
        raise FetchError(f"HTTP {status}", status_code=status)

    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FetchError("response body was not valid JSON")


# --------------------------------------------------------------------------
# OAuth token refresh
# --------------------------------------------------------------------------

_TOKEN_REFRESH_ERROR_SUFFIX = "open Claude Code and run /login"


def _build_refresh_request(refresh_token: str) -> urllib.request.Request:
    """Build (without sending) the urllib Request for the OAuth token refresh
    POST. Isolated so tests can inspect headers/body without touching the
    network."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    return urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "anthropic-beta": "oauth-2025-04-20",
        },
        method="POST",
    )


def _refresh_failure_message(status_code: int) -> str:
    """Map an HTTP status code from the token refresh endpoint to an
    actionable, token-free error message."""
    if status_code in (400, 401):
        return (
            f"token refresh rejected (HTTP {status_code}) — refresh token is "
            f"invalid; {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )
    if status_code == 403:
        return "token refresh blocked (HTTP 403) — request rejected by server"
    return f"token refresh failed (HTTP {status_code}) — {_TOKEN_REFRESH_ERROR_SUFFIX}"


def post_oauth_refresh(refresh_token: str) -> dict:
    """Perform the raw HTTP POST to TOKEN_URL and return the parsed JSON body.

    Isolated as its own function (mirroring fetch_usage_payload) so tests can
    monkeypatch it without touching urllib/network. Never includes the token
    in any raised error message.
    """
    request = _build_refresh_request(refresh_token)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            resp_body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(_refresh_failure_message(exc.code), status_code=exc.code)
    except urllib.error.URLError as exc:
        raise FetchError(
            f"token refresh failed (network error: {exc.reason.__class__.__name__}) — "
            f"{_TOKEN_REFRESH_ERROR_SUFFIX}"
        )
    except TimeoutError:
        raise FetchError(f"token refresh failed (timed out) — {_TOKEN_REFRESH_ERROR_SUFFIX}")

    if status != 200:
        raise FetchError(_refresh_failure_message(status), status_code=status)

    try:
        parsed = json.loads(resp_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FetchError(
            f"token refresh failed (invalid response body) — {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )

    if not isinstance(parsed, dict):
        raise FetchError(
            f"token refresh failed (unexpected response shape) — {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )

    return parsed


def refresh_token_flow(creds_path: Path) -> str:
    """Refresh the OAuth access token using the refresh token stored in the
    credentials file, back up and update that file in place, and return the
    new access token.

    Only accessToken / refreshToken / expiresAt under claudeAiOauth are
    touched; every other key in the file is preserved. Raises FetchError with
    an actionable, token-free message on any failure.
    """
    data = read_credentials(creds_path)
    refresh_token = _extract_oauth_field(data, "refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        log.warning("token refresh failed: no refresh token available in credentials file")
        raise FetchError(
            f"token refresh failed (no refresh token available) — {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )

    log.info("refreshing OAuth access token")
    response_data = post_oauth_refresh(refresh_token)

    new_access_token = response_data.get("access_token")
    if not isinstance(new_access_token, str) or not new_access_token:
        log.warning("token refresh failed: response missing access_token")
        raise FetchError(
            f"token refresh failed (malformed response) — {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )

    # Back up the credentials file BEFORE modifying it, overwriting any prior
    # backup, so a bad refresh response can never destroy the last-known-good
    # credentials without a copy existing first.
    backup_path = creds_path.with_name(creds_path.name + ".claude-quota.bak")
    try:
        backup_path.write_bytes(creds_path.read_bytes())
    except OSError:
        log.warning("token refresh failed: could not back up credentials file")
        raise FetchError(
            f"token refresh failed (could not back up credentials file) — {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )

    oauth_block = data.get("claudeAiOauth")
    if not isinstance(oauth_block, dict):
        oauth_block = {}
        data["claudeAiOauth"] = oauth_block

    oauth_block["accessToken"] = new_access_token

    new_refresh_token = response_data.get("refresh_token")
    if isinstance(new_refresh_token, str) and new_refresh_token:
        oauth_block["refreshToken"] = new_refresh_token

    expires_in = response_data.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        oauth_block["expiresAt"] = int(time.time() * 1000) + int(expires_in * 1000)

    try:
        _atomic_write_json(creds_path, data)
    except OSError:
        log.warning("token refresh failed: could not write updated credentials file")
        raise FetchError(
            f"token refresh failed (could not write credentials file) — {_TOKEN_REFRESH_ERROR_SUFFIX}"
        )

    log.info("token refresh succeeded")
    return new_access_token


# The exact 429 friendly message, factored out as a constant so
# initial_poll_delay() can recognize it (string equality) without duplicating
# the literal in two places.
_RATE_LIMIT_MESSAGE = (
    "HTTP 429 — rate limited by Anthropic; will retry next cycle "
    "(avoid restarting repeatedly)"
)


def fetch_and_normalize() -> dict:
    """Full fetch cycle: read creds, refresh token if needed, call API,
    normalize. Raises FetchError."""
    creds = read_credentials(CREDENTIALS_PATH)
    token = _extract_access_token(creds)
    refreshed_this_cycle = False

    if not NO_REFRESH:
        expires_at = _extract_oauth_field(creds, "expiresAt")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            now_ms = time.time() * 1000
            if now_ms > expires_at - 60000:
                log.info("access token is expired or expiring soon; refreshing proactively")
                try:
                    token = refresh_token_flow(CREDENTIALS_PATH)
                    refreshed_this_cycle = True
                except FetchError as exc:
                    log.warning(
                        "proactive token refresh failed (%s); trying usage call with existing token",
                        exc,
                    )

    try:
        payload = fetch_usage_payload(token)
    except FetchError as exc:
        if exc.status_code == 429:
            # Not an auth failure: never attempt a token refresh or a retry
            # this cycle -- refreshing/retrying on 429 would only make the
            # rate limiting worse.
            raise FetchError(_RATE_LIMIT_MESSAGE, status_code=429)
        if exc.status_code == 401:
            if NO_REFRESH:
                raise FetchError(
                    "HTTP 401 — token expired; open Claude Code to refresh "
                    "(or unset CLAUDE_QUOTA_NO_REFRESH)"
                )
            if refreshed_this_cycle:
                # Already refreshed once this cycle (proactively) and still
                # got a 401 -- don't loop, surface the failure.
                raise
            log.info("usage call returned 401; refreshing token and retrying once")
            token = refresh_token_flow(CREDENTIALS_PATH)
            payload = fetch_usage_payload(token)
        else:
            raise

    try:
        return normalize_usage_payload(payload)
    except ValueError as exc:
        raise FetchError(str(exc))


# --------------------------------------------------------------------------
# Data file read/write
# --------------------------------------------------------------------------

_data_lock = threading.Lock()

_OK_FALSE_SKELETON = {
    "ok": False,
    "stale": True,
    "error": None,
    "five_hour": None,
    "seven_day": None,
    "fetched_at": None,
}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".latest-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def read_data_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_OK_FALSE_SKELETON)


def write_success(path: Path, normalized: dict) -> None:
    with _data_lock:
        _atomic_write_json(path, normalized)


def write_failure(path: Path, error: str) -> dict:
    """Write a failure state, preserving prior good utilizations if present."""
    with _data_lock:
        previous = read_data_file(path)
        result = {
            "ok": False,
            "stale": True,
            "error": error,
            "fetched_at": previous.get("fetched_at"),
            "five_hour": previous.get("five_hour"),
            "seven_day": previous.get("seven_day"),
        }
        _atomic_write_json(path, result)
        return result


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------


def _is_auth_failure(exc: FetchError) -> bool:
    """True if this failure is a 401 or an OAuth token-refresh failure (as
    opposed to e.g. a network error or a 429), which is when the sanitized
    credential-state log line is useful for diagnosing."""
    if exc.status_code == 401:
        return True
    message = str(exc)
    return "401" in message or "token refresh" in message


# -- Auth-failure latch -----------------------------------------------------
#
# After a poll cycle fails with a 401-class outcome, further polling is
# pointless (and, against an aggressively rate-limited endpoint, actively
# harmful) until the user completes a fresh login -- which rewrites the
# credentials file. So we latch on the credentials file's mtime at the moment
# of the auth failure: as long as that file's mtime hasn't changed, every
# subsequent cycle skips all network activity and just re-reports the same
# actionable error. The moment the mtime changes (a new login happened, or
# the file's stat outcome otherwise changes), the latch clears and normal
# polling resumes.

_AUTH_LATCH_ERROR = (
    "authentication failed — complete a fresh login: open a terminal, run "
    "claude, then /login (collector retries automatically once the "
    "credentials file changes)"
)

_auth_latch: dict = {"active": False, "mtime": None}

# Consecutive-429 counter driving next_poll_delay()'s exponential backoff.
_consecutive_429s = 0
_MAX_BACKOFF_SECONDS = 7200

# Kind of the most recent poll_once() outcome: "ok" | "429" | "auth" | "other".
# Read by poller_loop() (via next_poll_delay()) to decide the next delay.
_last_poll_status = "ok"


def _credentials_mtime(path: Path) -> Optional[float]:
    """Best-effort mtime of the credentials file; None if it can't be stat'd."""
    try:
        return os.path.getmtime(str(path))
    except OSError:
        return None


def poll_once(path: Path = None) -> dict:
    """Run a single poll cycle: fetch, normalize, write, log. Returns the
    data that was written.

    Records the outcome kind in the module-level _last_poll_status ("ok" |
    "429" | "auth" | "other") for next_poll_delay(), and maintains the
    auth-failure latch (see _auth_latch docs above).
    """
    global _last_poll_status
    path = path or DATA_PATH

    if _auth_latch["active"]:
        current_mtime = _credentials_mtime(CREDENTIALS_PATH)
        if current_mtime == _auth_latch["mtime"]:
            result = write_failure(path, _AUTH_LATCH_ERROR)
            log.info(
                "skipping poll: authentication latch active (credentials "
                "file unchanged since last auth failure)"
            )
            _last_poll_status = "auth"
            return result
        # Credentials file changed (fresh login) or its stat outcome
        # otherwise changed: clear the latch and fall through to a normal
        # cycle.
        _auth_latch["active"] = False
        _auth_latch["mtime"] = None

    try:
        normalized = fetch_and_normalize()
    except FetchError as exc:
        result = write_failure(path, str(exc))
        log.info(
            "poll failed: %s (5h=%s 7d=%s)",
            exc,
            _fmt_util(result.get("five_hour")),
            _fmt_util(result.get("seven_day")),
        )
        if _is_auth_failure(exc):
            _auth_latch["active"] = True
            _auth_latch["mtime"] = _credentials_mtime(CREDENTIALS_PATH)
            _last_poll_status = "auth"
            try:
                creds_data = read_credentials(CREDENTIALS_PATH)
            except FetchError:
                pass
            else:
                log.info(describe_credentials(creds_data))
        elif exc.status_code == 429:
            _last_poll_status = "429"
        else:
            _last_poll_status = "other"
        return result
    except Exception as exc:  # noqa: BLE001 - never crash the poller
        result = write_failure(path, f"unexpected error ({exc.__class__.__name__})")
        log.exception("poll failed with unexpected error")
        _last_poll_status = "other"
        return result
    else:
        write_success(path, normalized)
        _auth_latch["active"] = False
        _auth_latch["mtime"] = None
        _last_poll_status = "ok"
        log.info(
            "poll ok: 5h=%s 7d=%s",
            _fmt_util(normalized.get("five_hour")),
            _fmt_util(normalized.get("seven_day")),
        )
        return normalized


# --------------------------------------------------------------------------
# Reset-aware scheduling
# --------------------------------------------------------------------------
#
# POLL_SECONDS is tuned for an aggressively rate-limited endpoint, but a fixed
# cadence produces one visibly wrong state: when a usage window rolls over, the
# widget keeps showing the pre-reset utilization for up to a full poll interval
# even though the real figure has just dropped to ~0. So when a window's
# advertised resets_at falls sooner than the next scheduled poll, we poll just
# after that boundary instead.
#
# This does NOT raise the sustained request rate: it adds at most one extra
# poll per window rollover (a handful per day), because the post-reset response
# carries a resets_at ~5h/7d in the future, which puts the cadence straight
# back to POLL_SECONDS.

# Wait this long past resets_at before re-polling — the server may not have
# rolled the window over at the exact instant it advertises.
_RESET_GRACE_SECONDS = 15
# Floor for a reset-aware poll, so a stale or past resets_at can never spin the
# poller into a tight request loop.
_MIN_RESET_POLL_SECONDS = 60


def _parse_reset_at(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 resets_at into an aware UTC datetime, or None if it is
    missing/unparseable. Tolerates a trailing 'Z' (fromisoformat only accepts
    it natively on 3.11+) and naive timestamps (assumed UTC).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_until_next_reset(
    data: Any, now: Optional[datetime] = None
) -> Optional[int]:
    """Seconds until the soonest still-future window reset in `data`, or None
    when no window advertises a parseable future resets_at.

    Pure and side-effect free (`now` is injectable) so it stays unit-testable
    without freezing the clock.
    """
    if not isinstance(data, dict):
        return None
    now = now or datetime.now(timezone.utc)
    soonest: Optional[float] = None
    for key in ("five_hour", "seven_day"):
        window = data.get(key)
        if not isinstance(window, dict):
            continue
        parsed = _parse_reset_at(window.get("resets_at"))
        if parsed is None:
            continue
        delta = (parsed - now).total_seconds()
        if delta <= 0:
            continue
        if soonest is None or delta < soonest:
            soonest = delta
    return None if soonest is None else int(soonest)


def next_poll_delay(
    status_kind: str, data: Any = None, now: Optional[datetime] = None
) -> int:
    """Compute the delay (seconds) before the next poll cycle, given the
    outcome kind ("ok" | "429" | "auth" | "other") of the poll cycle that
    just ran.

    Consecutive 429s back off exponentially: min(POLL_SECONDS *
    2**consecutive_429s, _MAX_BACKOFF_SECONDS). Any non-429 outcome resets
    the counter and returns the normal POLL_SECONDS interval.

    On a successful cycle, `data` (the freshly written payload) is consulted so
    the next poll can be pulled forward to just after an imminent window reset
    -- never pushed out, and never below _MIN_RESET_POLL_SECONDS. Backoff and
    auth-latch cycles ignore it entirely: a rate-limited or unauthenticated
    collector must not be dragged back into polling by a reset boundary.
    """
    global _consecutive_429s
    if status_kind == "429":
        _consecutive_429s += 1
        delay = min(POLL_SECONDS * (2 ** _consecutive_429s), _MAX_BACKOFF_SECONDS)
        if delay > POLL_SECONDS:
            log.info("backing off: next poll in %d minutes", delay // 60)
        return delay
    _consecutive_429s = 0
    if status_kind != "ok":
        return POLL_SECONDS

    until_reset = seconds_until_next_reset(data, now)
    if until_reset is None:
        return POLL_SECONDS
    candidate = max(until_reset + _RESET_GRACE_SECONDS, _MIN_RESET_POLL_SECONDS)
    if candidate >= POLL_SECONDS:
        return POLL_SECONDS
    log.info(
        "window resets in %ds; pulling next poll forward to %ds",
        until_reset,
        candidate,
    )
    return candidate


def initial_poll_delay(path: Path = None) -> int:
    """Return how many seconds to wait before the very first poll on
    startup.

    Normally 0 (poll immediately -- needed for fresh installs). But if the
    data file on disk already reflects a recent 429 rate-limit failure (exact
    _RATE_LIMIT_MESSAGE, file mtime younger than POLL_SECONDS), return the
    remaining seconds instead, so that repeated collector restarts right
    after a 429 don't immediately re-hammer the endpoint. Pure / side-effect
    free so it's unit-testable without threads.
    """
    path = path or DATA_PATH
    try:
        mtime = os.path.getmtime(str(path))
    except OSError:
        return 0

    data = read_data_file(path)
    if data.get("error") != _RATE_LIMIT_MESSAGE:
        return 0

    age = time.time() - mtime
    if age >= POLL_SECONDS:
        return 0
    return max(0, int(POLL_SECONDS - age))


def _fmt_util(window: Optional[dict]) -> str:
    if not window or window.get("utilization") is None:
        return "n/a"
    return f"{window['utilization']}%"


def poller_loop(stop_event: threading.Event) -> None:
    delay = initial_poll_delay()
    if delay > 0:
        log.info(
            "recent rate-limit state found; first poll in %d minutes",
            max(1, delay // 60),
        )
        if stop_event.wait(delay):
            return

    while not stop_event.is_set():
        result = poll_once()
        delay = next_poll_delay(_last_poll_status, result)
        if stop_event.wait(delay):
            break


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class QuotaRequestHandler(BaseHTTPRequestHandler):
    server_version = "ClaudeQuotaCollector/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # Suppress default per-request access log noise.
        pass

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib method name
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        path = self.path.split("?", 1)[0]
        if path in ("/latest.json", "/"):
            data = read_data_file(DATA_PATH)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = json.dumps({"ok": False, "error": "not found"}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def _allow_reuse_address_for_platform(plat: str) -> bool:
    """SO_REUSEADDR (which http.server enables via allow_reuse_address = 1)
    means "allow a quick restart to rebind a socket still in TIME_WAIT" on
    POSIX. On Windows, SO_REUSEADDR instead permits two unrelated processes
    to bind the *same* address/port simultaneously, which silently defeats
    our single-instance port guard (two collectors both bind 127.0.0.1:8765
    and both poll the rate-limited API). So: keep the POSIX quick-restart
    behavior, but disable it on win32 so a second instance's bind() raises
    OSError like main() expects."""
    return plat != "win32"


class QuotaServer(ThreadingHTTPServer):
    # Evaluated once at class definition time -- see
    # _allow_reuse_address_for_platform for why this differs on Windows.
    allow_reuse_address = _allow_reuse_address_for_platform(sys.platform)

    def server_bind(self) -> None:
        # Belt-and-braces on top of allow_reuse_address = False: explicitly
        # request exclusive binding on Windows, where SO_REUSEADDR semantics
        # otherwise allow two processes to double-bind the same port.
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port: int = None) -> QuotaServer:
    port = PORT if port is None else port
    return QuotaServer(("127.0.0.1", port), QuotaRequestHandler)


# --------------------------------------------------------------------------
# Diagnostics (--diag)
# --------------------------------------------------------------------------


def _diag_credential_manager() -> dict:
    """Scan Windows Credential Manager for generic credentials whose target
    name contains "claude" (case-insensitive). Collects TARGET NAMES ONLY --
    never reads/decrypts a credential blob. Returns {"available": False,
    "claude_targets": []} on any failure or on non-Windows platforms."""
    result: dict = {"available": False, "claude_targets": []}
    if sys.platform != "win32":
        return result

    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        CRED_TYPE_GENERIC = 1

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        PCREDENTIAL = ctypes.POINTER(CREDENTIAL)

        advapi32.CredEnumerateW.restype = wintypes.BOOL
        advapi32.CredEnumerateW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.POINTER(PCREDENTIAL)),
        ]
        advapi32.CredFree.restype = None
        advapi32.CredFree.argtypes = [ctypes.c_void_p]

        count = wintypes.DWORD()
        creds_ptr_ptr = ctypes.POINTER(PCREDENTIAL)()

        ok = advapi32.CredEnumerateW(
            None, 0, ctypes.byref(count), ctypes.byref(creds_ptr_ptr)
        )
        if not ok:
            # e.g. ERROR_NOT_FOUND when there are no stored credentials at
            # all -- treat as a successful (empty) scan, not an error.
            last_err = ctypes.get_last_error()
            if last_err == 1168:  # ERROR_NOT_FOUND
                result["available"] = True
            return result

        targets = []
        try:
            for i in range(count.value):
                cred = creds_ptr_ptr[i].contents
                if cred.Type != CRED_TYPE_GENERIC:
                    continue
                name = cred.TargetName
                if name and "claude" in name.lower():
                    targets.append(name)
        finally:
            advapi32.CredFree(creds_ptr_ptr)

        result["available"] = True
        result["claude_targets"] = targets
        return result
    except Exception:  # noqa: BLE001 - diagnostics must never crash
        return {"available": False, "claude_targets": []}


def _parse_schtasks_list_output(output: str) -> dict:
    """Parse the stdout of `schtasks /Query /TN ... /V /FO LIST` into the
    fields the "scheduled_task" diag.json section needs.

    Field labels are matched case-insensitively against the text before the
    first colon on each line; the rest of the line (after that first colon)
    is kept verbatim as the value, since values themselves may contain
    colons (e.g. a drive letter in "Task To Run: C:\\path\\..."). Field
    labels are localized on non-English Windows -- if a line's label isn't
    one of the ones we recognize, it's simply skipped and the corresponding
    field stays None; "raw_first_lines" is filled regardless so a human can
    still read the localized output."""
    fields: dict = {
        "status": None,
        "task_to_run": None,
        "last_run_time": None,
        "last_result": None,
    }
    label_map = {
        "status": "status",
        "task to run": "task_to_run",
        "last run time": "last_run_time",
        "last result": "last_result",
    }

    non_empty_lines = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        non_empty_lines.append(line)
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = label_map.get(label.strip().lower())
        if key is not None:
            fields[key] = value.strip()

    fields["raw_first_lines"] = [ln[:120] for ln in non_empty_lines[:12]]
    return fields


def _diag_scheduled_task() -> dict:
    """Query Windows Task Scheduler for the "ClaudeQuotaCollector" at-logon
    task via `schtasks /Query`, so the task's presence/status can be
    verified without terminal access. available:False on non-Windows or if
    the schtasks invocation itself fails for any reason (missing binary,
    timeout, permissions, ...). exists reflects whether schtasks found the
    task (returncode == 0); status/task_to_run/last_run_time/last_result are
    best-effort parses of the LIST output and stay None if unparseable
    (e.g. localized field labels) even though exists is still True."""
    result: dict = {
        "available": False,
        "exists": False,
        "status": None,
        "task_to_run": None,
        "last_run_time": None,
        "last_result": None,
        "raw_first_lines": [],
    }
    if sys.platform != "win32":
        return result

    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", "ClaudeQuotaCollector", "/V", "/FO", "LIST"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - diagnostics must never crash
        return result

    result["available"] = True
    result["exists"] = proc.returncode == 0
    if not result["exists"]:
        return result

    result.update(_parse_schtasks_list_output(proc.stdout or ""))
    return result


def _diag_run_key() -> dict:
    """Query the HKCU Run key for the "ClaudeQuotaCollector" no-admin
    autostart fallback registered by install_startup.bat when Task
    Scheduler needs admin rights (schtasks /Create fails with "Access is
    denied"), via `reg query`. available:False on non-Windows or if the reg
    invocation itself fails for any reason (missing binary, timeout, ...).
    exists reflects whether reg found the value (returncode == 0); value is
    the REG_SZ command line verbatim (quoted interpreter path + quoted
    script path -- file paths only, safe to include) and stays None if the
    output can't be parsed even though exists is still True."""
    result: dict = {"available": False, "exists": False, "value": None}
    if sys.platform != "win32":
        return result

    try:
        proc = subprocess.run(
            [
                "reg", "query",
                "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                "/v", "ClaudeQuotaCollector",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - diagnostics must never crash
        return result

    result["available"] = True
    result["exists"] = proc.returncode == 0
    if not result["exists"]:
        return result

    for line in (proc.stdout or "").splitlines():
        if "ClaudeQuotaCollector" in line and "REG_SZ" in line:
            _, _, value = line.partition("REG_SZ")
            result["value"] = value.strip()
            break

    return result


def _diag_collector_process() -> dict:
    """Check whether a collector process (`python.../claude_quota.py`, no
    server mode) is currently running, via a PowerShell Win32_Process query.
    Excludes any matching process whose command line also contains
    "--diag" so a concurrent `--diag` invocation never reports itself as
    the running collector. available/running False on any failure or on
    non-Windows."""
    result: dict = {"available": False, "running": False, "pids": []}
    if sys.platform != "win32":
        return result

    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                "Where-Object {$_.CommandLine -like '*claude_quota.py*' -and "
                "$_.CommandLine -notlike '*--diag*'} | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - diagnostics must never crash
        return result

    result["available"] = True
    if proc.returncode != 0:
        return result

    pids = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue

    result["pids"] = pids
    result["running"] = len(pids) > 0
    return result


def _diag_credentials_file(creds_path: Path) -> dict:
    """Build the sanitized "credentials_file" section of diag.json. Never
    reads/includes any token text, prefix, or fragment."""
    info: dict = {
        "path": str(creds_path),
        "exists": False,
        "mtime": None,
        "top_level_keys": [],
        "oauth_keys": [],
        "access_token_present": False,
        "access_token_length": 0,
        "refresh_token_present": False,
        "refresh_token_length": 0,
        "expires_at_iso": None,
        "expired": None,
        "scopes": None,
        "subscription_type": None,
    }

    try:
        exists = creds_path.exists()
    except OSError:
        exists = False
    info["exists"] = exists
    if not exists:
        return info

    try:
        stat = creds_path.stat()
        info["mtime"] = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        info["mtime"] = None

    try:
        data = read_credentials(creds_path)
    except FetchError:
        return info

    if isinstance(data, dict):
        info["top_level_keys"] = sorted(data.keys())
        oauth = data.get("claudeAiOauth")
        if isinstance(oauth, dict):
            info["oauth_keys"] = sorted(oauth.keys())
            if "scopes" in oauth:
                info["scopes"] = oauth.get("scopes")
            if "subscriptionType" in oauth:
                info["subscription_type"] = oauth.get("subscriptionType")
        info.update(_credential_state_fields(data))

    return info


def build_diag(creds_path: Path = None) -> dict:
    """Build the full sanitized --diag payload. Does not touch the network
    (no usage-endpoint call) and never includes any token text."""
    creds_path = creds_path if creds_path is not None else CREDENTIALS_PATH
    return {
        "generated_at": _utc_now_iso(),
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "credentials_file": _diag_credentials_file(creds_path),
        "credential_manager": _diag_credential_manager(),
        "scheduled_task": _diag_scheduled_task(),
        "run_key": _diag_run_key(),
        "collector_process": _diag_collector_process(),
    }


def run_diag() -> None:
    """Entry point for `python claude_quota.py --diag`: writes DIAG_PATH and
    returns (caller exits 0). Never starts the server or calls the usage
    endpoint."""
    diag = build_diag()
    _atomic_write_json(DIAG_PATH, diag)
    log.info("diag written to %s", DIAG_PATH)
    print(f"diag written to {DIAG_PATH}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

_MAX_LOG_BYTES = 1_000_000  # 1 MB


def _configure_file_logging() -> None:
    """Attach a FileHandler (in addition to the console handler already set
    up by logging.basicConfig at import time) writing to LOG_PATH. If the
    log file already exceeds 1 MB, truncate it first so it doesn't grow
    without bound."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > _MAX_LOG_BYTES:
            with open(LOG_PATH, "w", encoding="utf-8"):
                pass
        handler = logging.FileHandler(str(LOG_PATH), encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
            )
        )
        logging.getLogger().addHandler(handler)
    except OSError as exc:
        log.warning("could not attach log file handler at %s (%s)", LOG_PATH, exc.__class__.__name__)


def main() -> None:
    if "--diag" in sys.argv[1:]:
        # Diagnostic mode: must not start the server and must not call the
        # (rate-limited) usage endpoint.
        _configure_file_logging()
        run_diag()
        return

    _configure_file_logging()

    log.info(
        "starting: port=%d poll_seconds=%d creds=%s data=%s log=%s",
        PORT,
        POLL_SECONDS,
        CREDENTIALS_PATH,
        DATA_PATH,
        LOG_PATH,
    )

    stop_event = threading.Event()
    poller = threading.Thread(target=poller_loop, args=(stop_event,), daemon=True)
    poller.start()

    try:
        httpd = make_server()
    except OSError:
        log.error(
            "another collector instance is already serving on port %d; exiting", PORT
        )
        stop_event.set()
        sys.exit(1)

    def _shutdown(signum, frame):  # noqa: ANN001 - signal handler signature
        log.info("shutting down (signal %s)", signum)
        stop_event.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        pass  # SIGTERM not available on this platform

    log.info("serving on http://127.0.0.1:%d/latest.json", PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        httpd.server_close()
        log.info("stopped")


if __name__ == "__main__":
    main()
