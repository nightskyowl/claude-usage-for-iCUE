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
import signal
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

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
HTTP_TIMEOUT_SECONDS = 30

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
    """Raised for any failure in reading credentials or calling the API.

    The message must never contain the token or any fragment of it.
    """


def read_access_token(path: Path) -> str:
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

    try:
        token = data["claudeAiOauth"]["accessToken"]
    except (KeyError, TypeError):
        raise FetchError("credentials file missing claudeAiOauth.accessToken")

    if not isinstance(token, str) or not token:
        raise FetchError("credentials file has an empty/invalid access token")

    return token


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
            "User-Agent": "claude-quota-widget/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        raise FetchError(f"network error ({exc.reason.__class__.__name__})")
    except TimeoutError:
        raise FetchError("request timed out")

    if status != 200:
        raise FetchError(f"HTTP {status}")

    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FetchError("response body was not valid JSON")


def fetch_and_normalize() -> dict:
    """Full fetch cycle: read token, call API, normalize. Raises FetchError."""
    token = read_access_token(CREDENTIALS_PATH)
    payload = fetch_usage_payload(token)
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


def poll_once(path: Path = None) -> dict:
    """Run a single poll cycle: fetch, normalize, write, log. Returns the
    data that was written."""
    path = path or DATA_PATH
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
        return result
    except Exception as exc:  # noqa: BLE001 - never crash the poller
        result = write_failure(path, f"unexpected error ({exc.__class__.__name__})")
        log.exception("poll failed with unexpected error")
        return result
    else:
        write_success(path, normalized)
        log.info(
            "poll ok: 5h=%s 7d=%s",
            _fmt_util(normalized.get("five_hour")),
            _fmt_util(normalized.get("seven_day")),
        )
        return normalized


def _fmt_util(window: Optional[dict]) -> str:
    if not window or window.get("utilization") is None:
        return "n/a"
    return f"{window['utilization']}%"


def poller_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        poll_once()
        stop_event.wait(POLL_SECONDS)


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


def make_server(port: int = None) -> ThreadingHTTPServer:
    port = PORT if port is None else port
    return ThreadingHTTPServer(("127.0.0.1", port), QuotaRequestHandler)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    log.info(
        "starting: port=%d poll_seconds=%d creds=%s data=%s",
        PORT,
        POLL_SECONDS,
        CREDENTIALS_PATH,
        DATA_PATH,
    )

    stop_event = threading.Event()
    poller = threading.Thread(target=poller_loop, args=(stop_event,), daemon=True)
    poller.start()

    httpd = make_server()

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
