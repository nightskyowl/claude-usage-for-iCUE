#!/usr/bin/env python3
"""Unit tests for claude_quota.py. Stdlib unittest only, no network access."""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


@contextlib.contextmanager
def _env(name, value):
    """Temporarily set an environment variable, restoring the previous state
    (including absence) on exit."""
    sentinel = object()
    previous = os.environ.get(name, sentinel)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_quota as cq

# --------------------------------------------------------------------------
# Real-collector.log safety net
# --------------------------------------------------------------------------
#
# claude_quota.log ("claude_quota" logger) propagates to the root logger,
# which logging.basicConfig() (run at import time by claude_quota) attaches a
# StreamHandler to. Nothing here writes to a *file* unless something calls
# claude_quota._configure_file_logging() (directly, or indirectly via
# claude_quota.main()) -- that attaches a FileHandler pointed at
# claude_quota.LOG_PATH to the root logger. If any test does that without
# first repointing LOG_PATH at a temp file, every log line emitted by *any*
# later test in the run (fixture warnings, "token refresh succeeded" from
# monkeypatched refresh flows, "poll ok"/"poll failed" from poll-cycle tests,
# etc.) leaks into the real collector.log used by the live collector process.
#
# Individual test classes that exercise file logging (LogFileTruncationTests,
# DiagCliInvocationTests) patch LOG_PATH locally and detach any handler they
# attach. setUpModule/tearDownModule below is a belt-and-suspenders guard on
# top of that: it repoints LOG_PATH at a per-run temp file for the *entire*
# test session, and asserts no FileHandler targeting the real collector.log
# is ever attached to the root logger.

_REAL_LOG_PATH = cq.LOG_PATH.resolve()
_module_tmp_log_dir = None


def _real_log_file_handler_attached():
    """Return the first root-logger FileHandler whose target file resolves
    to the real collector.log, or None."""
    for handler in cq.logging.getLogger().handlers:
        if isinstance(handler, cq.logging.FileHandler):
            try:
                base = Path(handler.baseFilename).resolve()
            except OSError:
                continue
            if base == _REAL_LOG_PATH:
                return handler
    return None


def _assert_real_log_untouched():
    leaked = _real_log_file_handler_attached()
    assert leaked is None, (
        f"a FileHandler targeting the real collector.log ({_REAL_LOG_PATH}) "
        f"is attached to the root logger: {leaked!r} -- this would leak "
        f"test log lines into the live collector's log file"
    )


def setUpModule():
    global _module_tmp_log_dir
    _assert_real_log_untouched()
    _module_tmp_log_dir = tempfile.TemporaryDirectory(prefix="claude-quota-test-log-")
    cq.LOG_PATH = Path(_module_tmp_log_dir.name) / "collector.log"


def tearDownModule():
    cq.LOG_PATH = _REAL_LOG_PATH
    _assert_real_log_untouched()
    if _module_tmp_log_dir is not None:
        _module_tmp_log_dir.cleanup()


class NormalizeUtilizationTests(unittest.TestCase):
    """Rule under test (see cq.normalize_utilization docstring): raw values are
    treated as percents (0-100) by default -- there is no fraction heuristic,
    since the Anthropic /api/oauth/usage endpoint is observed to report
    utilization as a percent. A raw 0.42 means "0.42 percent", not 42
    percent."""

    def setUp(self):
        # Guard against test order / env leakage: force the flag off for this
        # class unless a test explicitly overrides it.
        self._orig_flag = cq.CLAUDE_QUOTA_ASSUME_FRACTION
        cq.CLAUDE_QUOTA_ASSUME_FRACTION = False
        self.addCleanup(setattr, cq, "CLAUDE_QUOTA_ASSUME_FRACTION", self._orig_flag)

    def test_raw_42_is_a_percent(self):
        self.assertEqual(cq.normalize_utilization(42), 42.0)

    def test_raw_0_42_is_0_4_percent_not_42_percent(self):
        self.assertEqual(cq.normalize_utilization(0.42), 0.4)

    def test_boundary_1_0_is_1_percent_not_100_percent(self):
        self.assertEqual(cq.normalize_utilization(1.0), 1.0)

    def test_raw_1_5_is_already_a_percent(self):
        self.assertEqual(cq.normalize_utilization(1.5), 1.5)

    def test_clamp_above_100(self):
        self.assertEqual(cq.normalize_utilization(150), 100.0)

    def test_clamp_negative(self):
        self.assertEqual(cq.normalize_utilization(-5), 0.0)

    def test_rounds_to_one_decimal(self):
        self.assertEqual(cq.normalize_utilization(61.53), 61.5)
        self.assertEqual(cq.normalize_utilization(0.615), 0.6)

    def test_none_returns_none(self):
        self.assertIsNone(cq.normalize_utilization(None))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(cq.normalize_utilization("42"))
        self.assertIsNone(cq.normalize_utilization([]))

    def test_bool_rejected(self):
        # bool is a subclass of int in Python; must not be silently accepted.
        self.assertIsNone(cq.normalize_utilization(True))


class AssumeFractionFlagTests(unittest.TestCase):
    """CLAUDE_QUOTA_ASSUME_FRACTION is an escape hatch: when on, raw values are
    multiplied by 100 before clamping (i.e. the old fraction heuristic, but
    unconditional rather than value-dependent)."""

    def setUp(self):
        self._orig_flag = cq.CLAUDE_QUOTA_ASSUME_FRACTION
        self.addCleanup(setattr, cq, "CLAUDE_QUOTA_ASSUME_FRACTION", self._orig_flag)

    def test_flag_off_treats_raw_as_percent(self):
        cq.CLAUDE_QUOTA_ASSUME_FRACTION = False
        self.assertEqual(cq.normalize_utilization(0.42), 0.4)
        self.assertEqual(cq.normalize_utilization(42), 42.0)

    def test_flag_on_treats_raw_as_fraction(self):
        cq.CLAUDE_QUOTA_ASSUME_FRACTION = True
        self.assertEqual(cq.normalize_utilization(0.42), 42.0)
        self.assertEqual(cq.normalize_utilization(1.0), 100.0)

    def test_flag_on_still_clamps_after_scaling(self):
        cq.CLAUDE_QUOTA_ASSUME_FRACTION = True
        self.assertEqual(cq.normalize_utilization(2), 100.0)


class FractionalUtilizationWarningTests(unittest.TestCase):
    """normalize_usage_payload should warn (once per process) when both
    windows' raw utilizations look fractional (<=1.0) while
    CLAUDE_QUOTA_ASSUME_FRACTION is off, without changing any values."""

    def setUp(self):
        self._orig_flag = cq.CLAUDE_QUOTA_ASSUME_FRACTION
        cq.CLAUDE_QUOTA_ASSUME_FRACTION = False
        self.addCleanup(setattr, cq, "CLAUDE_QUOTA_ASSUME_FRACTION", self._orig_flag)

        self._orig_warned = cq._fractional_warning_logged
        cq._fractional_warning_logged = False
        self.addCleanup(setattr, cq, "_fractional_warning_logged", self._orig_warned)

    @staticmethod
    def _fractional_payload():
        return {
            "five_hour": {"utilization": 0.2, "resets_at": "2026-08-12T12:00:00Z"},
            "seven_day": {"utilization": 0.5, "resets_at": "2026-08-15T00:00:00Z"},
        }

    def test_warns_once_when_both_windows_look_fractional(self):
        with self.assertLogs(cq.log, level="WARNING") as ctx:
            result = cq.normalize_usage_payload(self._fractional_payload())
        self.assertTrue(any("fractional" in message for message in ctx.output))
        # values themselves must be unchanged (treated as percent, not scaled)
        self.assertEqual(result["five_hour"]["utilization"], 0.2)
        self.assertEqual(result["seven_day"]["utilization"], 0.5)

    def test_does_not_warn_again_on_second_call(self):
        cq.normalize_usage_payload(self._fractional_payload())
        self.assertTrue(cq._fractional_warning_logged)
        with self.assertRaises(AssertionError):
            with self.assertLogs(cq.log, level="WARNING"):
                cq.normalize_usage_payload(self._fractional_payload())

    def test_does_not_warn_when_only_one_window_looks_fractional(self):
        payload = {
            "five_hour": {"utilization": 0.2, "resets_at": "x"},
            "seven_day": {"utilization": 42, "resets_at": "y"},
        }
        with self.assertRaises(AssertionError):
            with self.assertLogs(cq.log, level="WARNING"):
                cq.normalize_usage_payload(payload)

    def test_does_not_warn_when_assume_fraction_flag_is_on(self):
        cq.CLAUDE_QUOTA_ASSUME_FRACTION = True
        with self.assertRaises(AssertionError):
            with self.assertLogs(cq.log, level="WARNING"):
                cq.normalize_usage_payload(self._fractional_payload())


class NormalizeUsagePayloadTests(unittest.TestCase):
    def test_sample_payload_matches_contract(self):
        payload = {
            "five_hour": {"utilization": 23, "resets_at": "2026-08-12T12:00:00Z"},
            "seven_day": {"utilization": 0.615, "resets_at": "2026-08-15T00:00:00Z"},
            "seven_day_opus": {"utilization": 5},
        }
        result = cq.normalize_usage_payload(payload)
        self.assertTrue(result["ok"])
        self.assertFalse(result["stale"])
        self.assertIn("fetched_at", result)
        self.assertEqual(
            result["five_hour"],
            {"utilization": 23.0, "resets_at": "2026-08-12T12:00:00Z"},
        )
        self.assertEqual(
            result["seven_day"],
            {"utilization": 0.6, "resets_at": "2026-08-15T00:00:00Z"},
        )
        # extra key must be ignored, not present in output
        self.assertNotIn("seven_day_opus", result)

    def test_missing_resets_at_becomes_null(self):
        payload = {"five_hour": {"utilization": 10}}
        result = cq.normalize_usage_payload(payload)
        self.assertEqual(result["five_hour"], {"utilization": 10.0, "resets_at": None})
        self.assertIsNone(result["seven_day"])

    def test_list_payload_fails_gracefully(self):
        with self.assertRaises(ValueError):
            cq.normalize_usage_payload([1, 2, 3])

    def test_empty_payload_fails_gracefully(self):
        with self.assertRaises(ValueError):
            cq.normalize_usage_payload({"unexpected": {"foo": "bar"}})

    def test_tolerates_missing_seven_day(self):
        payload = {"five_hour": {"utilization": 50, "resets_at": "x"}}
        result = cq.normalize_usage_payload(payload)
        self.assertIsNone(result["seven_day"])
        self.assertEqual(result["five_hour"]["utilization"], 50.0)


class TokenReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def _write_creds(self, content):
        path = Path(self.tmpdir.name) / ".credentials.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_reads_token_from_valid_file(self):
        path = self._write_creds(
            json.dumps({"claudeAiOauth": {"accessToken": "fake-token-abc"}})
        )
        token = cq.read_access_token(path)
        self.assertEqual(token, "fake-token-abc")

    def test_missing_file_raises_fetch_error(self):
        path = Path(self.tmpdir.name) / "does_not_exist.json"
        with self.assertRaises(cq.FetchError) as ctx:
            cq.read_access_token(path)
        self.assertNotIn("fake-token-abc", str(ctx.exception))

    def test_invalid_json_raises_fetch_error(self):
        path = self._write_creds("{not valid json")
        with self.assertRaises(cq.FetchError):
            cq.read_access_token(path)

    def test_missing_key_raises_fetch_error(self):
        path = self._write_creds(json.dumps({"somethingElse": True}))
        with self.assertRaises(cq.FetchError):
            cq.read_access_token(path)

    def test_error_message_never_contains_token(self):
        # Even on the failure paths, ensure no accidental leakage: build a
        # file with a token-like value in a wrong location and confirm the
        # error text does not include it.
        path = self._write_creds(json.dumps({"claudeAiOauth": {"wrongKey": "sk-ant-secret-999"}}))
        with self.assertRaises(cq.FetchError) as ctx:
            cq.read_access_token(path)
        self.assertNotIn("sk-ant-secret-999", str(ctx.exception))


class PollCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_path = Path(self.tmpdir.name) / "latest.json"

        # Patch credentials path so fetch_and_normalize's read_access_token call
        # (invoked inside poll_once) succeeds without touching the real file.
        self.creds_path = Path(self.tmpdir.name) / ".credentials.json"
        self.creds_path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "fake-token-xyz"}}),
            encoding="utf-8",
        )
        self._orig_creds = cq.CREDENTIALS_PATH
        cq.CREDENTIALS_PATH = self.creds_path
        self.addCleanup(setattr, cq, "CREDENTIALS_PATH", self._orig_creds)

        self._orig_fetch = cq.fetch_usage_payload
        self.addCleanup(setattr, cq, "fetch_usage_payload", self._orig_fetch)

        self._orig_post_refresh = cq.post_oauth_refresh
        self.addCleanup(setattr, cq, "post_oauth_refresh", self._orig_post_refresh)

        self._orig_no_refresh = cq.NO_REFRESH
        self.addCleanup(setattr, cq, "NO_REFRESH", self._orig_no_refresh)
        cq.NO_REFRESH = False

        # Auth-latch / backoff module state is global and must not leak
        # between tests (order-independence).
        self._orig_auth_latch = dict(cq._auth_latch)
        cq._auth_latch = {"active": False, "mtime": None}
        self.addCleanup(setattr, cq, "_auth_latch", self._orig_auth_latch)

        self._orig_consecutive_429s = cq._consecutive_429s
        cq._consecutive_429s = 0
        self.addCleanup(setattr, cq, "_consecutive_429s", self._orig_consecutive_429s)

    def _write_creds(self, oauth_extra=None, top_level_extra=None):
        """Overwrite self.creds_path with a claudeAiOauth block (accessToken
        plus any extra fields like refreshToken/expiresAt) and optional
        sibling top-level keys, for refresh-flow tests."""
        oauth = {"accessToken": "fake-token-xyz"}
        oauth.update(oauth_extra or {})
        content = {"claudeAiOauth": oauth}
        content.update(top_level_extra or {})
        self.creds_path.write_text(json.dumps(content), encoding="utf-8")
        return content

    def _read_data(self):
        with open(self.data_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_success_path_writes_contract_shape(self):
        sample = {
            "five_hour": {"utilization": 23, "resets_at": "2026-08-12T12:00:00Z"},
            "seven_day": {"utilization": 0.615, "resets_at": "2026-08-15T00:00:00Z"},
            "seven_day_opus": {"utilization": 5},
        }
        cq.fetch_usage_payload = lambda token: sample

        result = cq.poll_once(self.data_path)

        self.assertTrue(result["ok"])
        self.assertFalse(result["stale"])
        on_disk = self._read_data()
        self.assertEqual(on_disk, result)
        self.assertEqual(on_disk["five_hour"]["utilization"], 23.0)
        self.assertEqual(on_disk["seven_day"]["utilization"], 0.6)
        self.assertIn("fetched_at", on_disk)

    def test_failure_after_success_keeps_utilizations_marks_stale(self):
        sample = {
            "five_hour": {"utilization": 23, "resets_at": "2026-08-12T12:00:00Z"},
            "seven_day": {"utilization": 61.5, "resets_at": "2026-08-15T00:00:00Z"},
        }
        cq.fetch_usage_payload = lambda token: sample
        cq.poll_once(self.data_path)

        def boom(token):
            raise cq.FetchError("simulated network failure")

        cq.fetch_usage_payload = boom
        result = cq.poll_once(self.data_path)

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["error"], "simulated network failure")
        self.assertEqual(result["five_hour"]["utilization"], 23.0)
        self.assertEqual(result["seven_day"]["utilization"], 61.5)

        on_disk = self._read_data()
        self.assertEqual(on_disk, result)

    def test_failure_with_no_prior_data_writes_ok_false_skeleton(self):
        def boom(token):
            raise cq.FetchError("simulated failure, no prior data")

        cq.fetch_usage_payload = boom
        result = cq.poll_once(self.data_path)

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertIsNone(result["five_hour"])
        self.assertIsNone(result["seven_day"])
        self.assertIsNone(result["fetched_at"])
        self.assertEqual(result["error"], "simulated failure, no prior data")

    def test_error_message_never_leaks_token(self):
        def boom(token):
            raise cq.FetchError(f"HTTP 401")

        cq.fetch_usage_payload = boom
        result = cq.poll_once(self.data_path)
        self.assertNotIn("fake-token-xyz", json.dumps(result))

    def test_missing_credentials_file_treated_as_failure(self):
        cq.CREDENTIALS_PATH = Path(self.tmpdir.name) / "nope.json"
        cq.fetch_usage_payload = self._orig_fetch  # should never even be called
        result = cq.poll_once(self.data_path)
        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertIn("credentials", result["error"])

    # -- OAuth refresh integration (poll-cycle level) --------------------

    def test_proactive_refresh_when_expires_at_is_past(self):
        now_ms = int(cq.time.time() * 1000)
        self._write_creds({"refreshToken": "old-refresh", "expiresAt": now_ms - 1000})

        cq.post_oauth_refresh = lambda rt: {
            "access_token": "fresh-token-123",
            "refresh_token": "fresh-refresh-456",
            "expires_in": 3600,
        }

        tokens_used = []

        def fake_fetch(token):
            tokens_used.append(token)
            return {
                "five_hour": {"utilization": 10, "resets_at": "x"},
                "seven_day": {"utilization": 20, "resets_at": "y"},
            }

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)

        self.assertTrue(result["ok"])
        self.assertEqual(tokens_used, ["fresh-token-123"])

        on_disk_creds = json.loads(self.creds_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk_creds["claudeAiOauth"]["accessToken"], "fresh-token-123")

        backup_path = self.creds_path.with_name(self.creds_path.name + ".claude-quota.bak")
        self.assertTrue(backup_path.exists())

    def test_401_then_refresh_then_single_retry_succeeds(self):
        self._write_creds({"refreshToken": "old-refresh", "expiresAt": int(cq.time.time() * 1000) + 999999})

        cq.post_oauth_refresh = lambda rt: {"access_token": "refreshed-token"}

        calls = []

        def fake_fetch(token):
            calls.append(token)
            if len(calls) == 1:
                raise cq.FetchError("HTTP 401", status_code=401)
            return {
                "five_hour": {"utilization": 30, "resets_at": "x"},
                "seven_day": {"utilization": 40, "resets_at": "y"},
            }

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)

        self.assertTrue(result["ok"])
        self.assertFalse(result["stale"])
        self.assertEqual(calls, ["fake-token-xyz", "refreshed-token"])
        self.assertEqual(result["five_hour"]["utilization"], 30.0)

    def test_401_then_refresh_fails_no_retry_loop(self):
        self._write_creds({"refreshToken": "old-refresh", "expiresAt": int(cq.time.time() * 1000) + 999999})

        def boom_refresh(rt):
            raise cq.FetchError(
                "token refresh failed (HTTP 400) — open Claude Code and run /login"
            )

        cq.post_oauth_refresh = boom_refresh

        calls = []

        def fake_fetch(token):
            calls.append(token)
            raise cq.FetchError("HTTP 401", status_code=401)

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertIn("token refresh failed", result["error"])
        self.assertIn("/login", result["error"])
        # exactly one usage call: the initial 401, no retry after a failed refresh
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fake-token-xyz", json.dumps(result))
        self.assertNotIn("old-refresh", json.dumps(result))

    def test_no_refresh_env_disables_refresh_on_401(self):
        self._write_creds({"refreshToken": "old-refresh", "expiresAt": int(cq.time.time() * 1000) + 999999})
        cq.NO_REFRESH = True

        refresh_called = []
        cq.post_oauth_refresh = lambda rt: refresh_called.append(rt) or {"access_token": "should-not-be-used"}

        calls = []

        def fake_fetch(token):
            calls.append(token)
            raise cq.FetchError("HTTP 401", status_code=401)

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)

        self.assertFalse(result["ok"])
        self.assertEqual(refresh_called, [])
        self.assertEqual(len(calls), 1)
        self.assertIn("401", result["error"])
        self.assertIn("CLAUDE_QUOTA_NO_REFRESH", result["error"])

    def test_refresh_never_leaks_token_text_in_error(self):
        self._write_creds({"refreshToken": "super-secret-refresh-token", "expiresAt": int(cq.time.time() * 1000) + 999999})

        def boom_refresh(rt):
            # simulate the flow function itself failing after seeing the token
            raise cq.FetchError("token refresh failed (HTTP 400) — open Claude Code and run /login")

        cq.post_oauth_refresh = boom_refresh

        def fake_fetch(token):
            raise cq.FetchError("HTTP 401", status_code=401)

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)
        dumped = json.dumps(result)
        self.assertNotIn("super-secret-refresh-token", dumped)
        self.assertNotIn("fake-token-xyz", dumped)


class TokenRefreshWriteBackTests(unittest.TestCase):
    """Direct tests of refresh_token_flow's credentials-file write-back
    behavior: backup creation, field preservation, partial responses."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.creds_path = Path(self.tmpdir.name) / ".credentials.json"

        self._orig_post_refresh = cq.post_oauth_refresh
        self.addCleanup(setattr, cq, "post_oauth_refresh", self._orig_post_refresh)

    def _write_creds(self, oauth, extra_top_level=None):
        content = {"claudeAiOauth": oauth}
        content.update(extra_top_level or {})
        self.creds_path.write_text(json.dumps(content), encoding="utf-8")

    def test_backup_created_and_fields_updated_others_preserved(self):
        self._write_creds(
            oauth={
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": 1000,
                "scopes": ["user:inference"],
                "subscriptionType": "pro",
            },
            extra_top_level={"otherTopLevelKey": {"nested": True}},
        )

        cq.post_oauth_refresh = lambda rt: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        }

        before_ms = int(cq.time.time() * 1000)
        new_token = cq.refresh_token_flow(self.creds_path)
        after_ms = int(cq.time.time() * 1000)

        self.assertEqual(new_token, "new-access")

        backup_path = self.creds_path.with_name(self.creds_path.name + ".claude-quota.bak")
        self.assertTrue(backup_path.exists())
        backup_data = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertEqual(backup_data["claudeAiOauth"]["accessToken"], "old-access")

        updated = json.loads(self.creds_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["claudeAiOauth"]["accessToken"], "new-access")
        self.assertEqual(updated["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertGreaterEqual(updated["claudeAiOauth"]["expiresAt"], before_ms + 7200 * 1000)
        self.assertLessEqual(updated["claudeAiOauth"]["expiresAt"], after_ms + 7200 * 1000)

        # unrelated keys preserved byte-for-byte-equivalent
        self.assertEqual(updated["claudeAiOauth"]["scopes"], ["user:inference"])
        self.assertEqual(updated["claudeAiOauth"]["subscriptionType"], "pro")
        self.assertEqual(updated["otherTopLevelKey"], {"nested": True})

    def test_backup_overwritten_each_call(self):
        self._write_creds(oauth={"accessToken": "v1", "refreshToken": "r1"})
        cq.post_oauth_refresh = lambda rt: {"access_token": "v2", "refresh_token": "r2"}
        cq.refresh_token_flow(self.creds_path)

        cq.post_oauth_refresh = lambda rt: {"access_token": "v3", "refresh_token": "r3"}
        cq.refresh_token_flow(self.creds_path)

        backup_path = self.creds_path.with_name(self.creds_path.name + ".claude-quota.bak")
        backup_data = json.loads(backup_path.read_text(encoding="utf-8"))
        # the backup reflects the state right before the *second* refresh,
        # i.e. the (v2, r2) creds, not the original (v1, r1)
        self.assertEqual(backup_data["claudeAiOauth"]["accessToken"], "v2")

    def test_response_without_new_refresh_token_or_expires_in_keeps_old_values(self):
        self._write_creds(
            oauth={"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": 12345}
        )
        cq.post_oauth_refresh = lambda rt: {"access_token": "new-access-only"}

        new_token = cq.refresh_token_flow(self.creds_path)

        self.assertEqual(new_token, "new-access-only")
        updated = json.loads(self.creds_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["claudeAiOauth"]["accessToken"], "new-access-only")
        self.assertEqual(updated["claudeAiOauth"]["refreshToken"], "old-refresh")
        self.assertEqual(updated["claudeAiOauth"]["expiresAt"], 12345)

    def test_missing_refresh_token_raises_actionable_error_no_network_call(self):
        self._write_creds(oauth={"accessToken": "old-access"})  # no refreshToken

        called = []
        cq.post_oauth_refresh = lambda rt: called.append(rt) or {"access_token": "x"}

        with self.assertRaises(cq.FetchError) as ctx:
            cq.refresh_token_flow(self.creds_path)

        self.assertEqual(called, [])
        self.assertIn("/login", str(ctx.exception))

    def test_refresh_request_failure_is_actionable_and_token_free(self):
        self._write_creds(oauth={"accessToken": "old-access", "refreshToken": "top-secret-rt"})

        def boom(rt):
            raise cq.FetchError("token refresh failed (HTTP 400) — open Claude Code and run /login")

        cq.post_oauth_refresh = boom

        with self.assertRaises(cq.FetchError) as ctx:
            cq.refresh_token_flow(self.creds_path)

        message = str(ctx.exception)
        self.assertIn("/login", message)
        self.assertNotIn("top-secret-rt", message)

        # credentials file must be untouched on failure (no backup, no write)
        backup_path = self.creds_path.with_name(self.creds_path.name + ".claude-quota.bak")
        self.assertFalse(backup_path.exists())
        unchanged = json.loads(self.creds_path.read_text(encoding="utf-8"))
        self.assertEqual(unchanged["claudeAiOauth"]["accessToken"], "old-access")

    def test_malformed_response_missing_access_token_raises_actionable_error(self):
        self._write_creds(oauth={"accessToken": "old-access", "refreshToken": "rt"})
        cq.post_oauth_refresh = lambda rt: {"refresh_token": "new-refresh"}  # no access_token

        with self.assertRaises(cq.FetchError) as ctx:
            cq.refresh_token_flow(self.creds_path)
        self.assertIn("/login", str(ctx.exception))


def _headers_lower(request):
    """Normalize a urllib.request.Request's headers to a lowercase-keyed
    dict, independent of urllib's internal capitalize() storage form."""
    return {k.lower(): v for k, v in request.header_items()}


class RefreshRequestBuildTests(unittest.TestCase):
    """_build_refresh_request is the seam that lets us assert on headers
    without touching the network. It must send the same User-Agent as the
    usage GET (see UsageRequestHeadersTests) so Cloudflare's WAF in front of
    console.anthropic.com doesn't block the request for looking like a bare
    urllib client."""

    def test_headers_include_user_agent_beta_accept_content_type(self):
        request = cq._build_refresh_request("some-refresh-token")
        headers = _headers_lower(request)
        self.assertEqual(headers["user-agent"], cq.USER_AGENT)
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["content-type"], "application/json")

    def test_request_targets_token_url_via_post(self):
        request = cq._build_refresh_request("some-refresh-token")
        self.assertEqual(request.get_full_url(), cq.TOKEN_URL)
        self.assertEqual(request.get_method(), "POST")

    def test_body_contains_refresh_token_and_client_id(self):
        request = cq._build_refresh_request("some-refresh-token")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["refresh_token"], "some-refresh-token")
        self.assertEqual(body["client_id"], cq.OAUTH_CLIENT_ID)


class UsageRequestHeadersTests(unittest.TestCase):
    """fetch_usage_payload's GET must use the same shared USER_AGENT
    constant as the refresh POST."""

    def setUp(self):
        self._orig_urlopen = cq.urllib.request.urlopen
        self.addCleanup(setattr, cq.urllib.request, "urlopen", self._orig_urlopen)

    def test_usage_request_uses_shared_user_agent_and_beta_header(self):
        captured = {}

        class FakeResponse:
            def __init__(self, body):
                self._body = body

            def getcode(self):
                return 200

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return FakeResponse(b'{"five_hour": {"utilization": 1}}')

        cq.urllib.request.urlopen = fake_urlopen

        cq.fetch_usage_payload("tok")

        headers = _headers_lower(captured["request"])
        self.assertEqual(headers["user-agent"], cq.USER_AGENT)
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["authorization"], "Bearer tok")


class RefreshFailureMessageMappingTests(unittest.TestCase):
    """_refresh_failure_message maps HTTP status codes from the token
    refresh endpoint to actionable, token-free messages."""

    def test_400_says_refresh_token_invalid(self):
        msg = cq._refresh_failure_message(400)
        self.assertIn("HTTP 400", msg)
        self.assertIn("refresh token is invalid", msg)
        self.assertIn("/login", msg)

    def test_401_says_refresh_token_invalid(self):
        msg = cq._refresh_failure_message(401)
        self.assertIn("HTTP 401", msg)
        self.assertIn("refresh token is invalid", msg)
        self.assertIn("/login", msg)

    def test_403_says_blocked_by_server_no_login_suggestion(self):
        msg = cq._refresh_failure_message(403)
        self.assertIn("HTTP 403", msg)
        self.assertIn("blocked", msg)
        self.assertIn("rejected by server", msg)
        # 403 means the server (e.g. WAF) rejected the request outright, not
        # that the refresh token itself is bad, so don't tell the user to
        # /login for this one.
        self.assertNotIn("/login", msg)

    def test_other_codes_keep_generic_message(self):
        msg = cq._refresh_failure_message(500)
        self.assertIn("HTTP 500", msg)
        self.assertIn("/login", msg)


class PostOauthRefreshHttpErrorTests(unittest.TestCase):
    """post_oauth_refresh must translate HTTPError status codes through
    _refresh_failure_message and always populate status_code."""

    def setUp(self):
        self._orig_urlopen = cq.urllib.request.urlopen
        self.addCleanup(setattr, cq.urllib.request, "urlopen", self._orig_urlopen)

    def _install_http_error(self, code):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(cq.TOKEN_URL, code, "err", {}, io.BytesIO(b""))

        cq.urllib.request.urlopen = fake_urlopen

    def test_403_raises_blocked_message_with_status_code(self):
        self._install_http_error(403)
        with self.assertRaises(cq.FetchError) as ctx:
            cq.post_oauth_refresh("some-refresh-token")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("blocked", str(ctx.exception))
        self.assertNotIn("some-refresh-token", str(ctx.exception))

    def test_401_raises_rejected_message_with_status_code(self):
        self._install_http_error(401)
        with self.assertRaises(cq.FetchError) as ctx:
            cq.post_oauth_refresh("some-refresh-token")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("rejected", str(ctx.exception))
        self.assertIn("/login", str(ctx.exception))
        self.assertNotIn("some-refresh-token", str(ctx.exception))

    def test_400_raises_rejected_message_with_status_code(self):
        self._install_http_error(400)
        with self.assertRaises(cq.FetchError) as ctx:
            cq.post_oauth_refresh("some-refresh-token")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("rejected", str(ctx.exception))
        self.assertIn("/login", str(ctx.exception))

    def test_500_raises_generic_message_with_status_code(self):
        self._install_http_error(500)
        with self.assertRaises(cq.FetchError) as ctx:
            cq.post_oauth_refresh("some-refresh-token")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertIn("/login", str(ctx.exception))


class HttpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_path = Path(self.tmpdir.name) / "latest.json"

        self._orig_data_path = cq.DATA_PATH
        cq.DATA_PATH = self.data_path
        self.addCleanup(setattr, cq, "DATA_PATH", self._orig_data_path)

        sample = {
            "ok": True,
            "stale": False,
            "fetched_at": "2026-08-12T08:00:00Z",
            "five_hour": {"utilization": 42.0, "resets_at": "2026-08-12T11:00:00Z"},
            "seven_day": {"utilization": 61.5, "resets_at": "2026-08-15T00:00:00Z"},
        }
        cq._atomic_write_json(self.data_path, sample)

        self.httpd = cq.make_server(port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.getcode(), dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_latest_json_returns_200_with_cors_and_valid_json(self):
        status, headers, body = self._get("/latest.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(headers.get("Content-Type"), "application/json")
        parsed = json.loads(body.decode("utf-8"))
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["five_hour"]["utilization"], 42.0)

    def test_root_also_serves_latest_json(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        parsed = json.loads(body.decode("utf-8"))
        self.assertTrue(parsed["ok"])

    def test_unknown_path_returns_404(self):
        status, headers, body = self._get("/other")
        self.assertEqual(status, 404)
        parsed = json.loads(body.decode("utf-8"))
        self.assertFalse(parsed["ok"])

    def test_missing_data_file_returns_ok_false_skeleton(self):
        os.remove(self.data_path)
        status, headers, body = self._get("/latest.json")
        self.assertEqual(status, 200)
        parsed = json.loads(body.decode("utf-8"))
        self.assertFalse(parsed["ok"])
        self.assertTrue(parsed["stale"])


class QuotaServerSingleInstanceGuardTests(unittest.TestCase):
    """Regression tests for the Windows double-bind bug: two collector
    processes both bound 127.0.0.1:8765 simultaneously because
    http.server.HTTPServer's allow_reuse_address = 1 maps to SO_REUSEADDR,
    which on Windows (unlike POSIX) permits two unrelated sockets to bind
    the exact same address/port. make_server() now returns a QuotaServer
    whose allow_reuse_address is platform-dependent (False on win32), so a
    second instance's bind() raises OSError and the existing port-in-use
    guard in main() fires."""

    def test_make_server_returns_quota_server(self):
        httpd = cq.make_server(port=0)
        try:
            self.assertIsInstance(httpd, cq.QuotaServer)
        finally:
            httpd.server_close()

    def test_allow_reuse_address_false_on_win32(self):
        self.assertFalse(cq._allow_reuse_address_for_platform("win32"))

    def test_allow_reuse_address_true_on_linux(self):
        self.assertTrue(cq._allow_reuse_address_for_platform("linux"))

    def test_allow_reuse_address_true_on_darwin(self):
        self.assertTrue(cq._allow_reuse_address_for_platform("darwin"))

    def test_quota_server_class_attribute_matches_running_platform(self):
        # Sanity check that the class attribute (evaluated once at class
        # definition time from sys.platform) is consistent with the helper.
        expected = cq._allow_reuse_address_for_platform(sys.platform)
        self.assertEqual(cq.QuotaServer.allow_reuse_address, expected)

    def test_second_instance_on_same_port_raises_oserror(self):
        # This is the behavior the port-in-use guard in main() depends on.
        # On POSIX (where this suite runs), allow_reuse_address is True,
        # which only permits rebinding a socket stuck in TIME_WAIT -- it
        # does not allow two simultaneously *live* listeners on the same
        # port, so this already proves the guard path works today. The
        # win32-specific behavior is covered by the allow_reuse_address
        # helper tests above, since we can't flip sys.platform at runtime
        # and have the QuotaServer class attribute (bound at class
        # definition time) pick it up.
        first = cq.make_server(port=0)
        self.addCleanup(first.server_close)
        port = first.server_address[1]

        with self.assertRaises(OSError):
            second = cq.make_server(port=port)
            second.server_close()


class LogFileTruncationTests(unittest.TestCase):
    """_configure_file_logging should truncate collector.log before attaching
    the handler if it's grown past 1 MB, and otherwise append."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.log_path = Path(self.tmpdir.name) / "collector.log"

        self._orig_log_path = cq.LOG_PATH
        cq.LOG_PATH = self.log_path
        self.addCleanup(setattr, cq, "LOG_PATH", self._orig_log_path)

        self._root_logger = cq.logging.getLogger()
        self._orig_handlers = list(self._root_logger.handlers)
        self.addCleanup(self._restore_handlers)

    def _restore_handlers(self):
        for handler in list(self._root_logger.handlers):
            if handler not in self._orig_handlers:
                self._root_logger.removeHandler(handler)
                handler.close()

    def test_oversized_log_is_truncated_before_attaching_handler(self):
        self.log_path.write_text("x" * (cq._MAX_LOG_BYTES + 1000), encoding="utf-8")
        cq._configure_file_logging()
        self.assertLess(self.log_path.stat().st_size, 1000)

    def test_small_log_is_not_truncated(self):
        self.log_path.write_text("existing log line\n", encoding="utf-8")
        cq._configure_file_logging()
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("existing log line", content)


class PollSecondsFloorTests(unittest.TestCase):
    def test_env_below_floor_is_clamped(self):
        os.environ["CLAUDE_QUOTA_POLL_SECONDS"] = "60"
        try:
            self.assertEqual(cq._resolve_poll_seconds(), cq._MIN_POLL_SECONDS)
        finally:
            del os.environ["CLAUDE_QUOTA_POLL_SECONDS"]

    def test_env_above_floor_is_respected(self):
        os.environ["CLAUDE_QUOTA_POLL_SECONDS"] = "1800"
        try:
            self.assertEqual(cq._resolve_poll_seconds(), 1800)
        finally:
            del os.environ["CLAUDE_QUOTA_POLL_SECONDS"]

    def test_non_integer_env_falls_back_to_floor(self):
        os.environ["CLAUDE_QUOTA_POLL_SECONDS"] = "not-a-number"
        try:
            self.assertEqual(cq._resolve_poll_seconds(), cq._MIN_POLL_SECONDS)
        finally:
            del os.environ["CLAUDE_QUOTA_POLL_SECONDS"]


class DescribeCredentialsTests(unittest.TestCase):
    """describe_credentials() must summarize credential state without ever
    including token text/prefix/fragment."""

    def test_present_access_absent_refresh_expired(self):
        token = "a" * 108
        expires_at_ms = int(cq.time.time() * 1000) - 5000  # already expired
        data = {
            "claudeAiOauth": {
                "accessToken": token,
                "expiresAt": expires_at_ms,
            }
        }
        summary = cq.describe_credentials(data)

        self.assertIn("access_token=present(len=108)", summary)
        self.assertIn("refresh_token=absent", summary)
        self.assertIn("(expired)", summary)
        self.assertNotIn(token, summary)

    def test_present_refresh_and_valid_expiry(self):
        access = "b" * 40
        refresh = "c" * 64
        expires_at_ms = int(cq.time.time() * 1000) + 3_600_000  # 1h in future
        data = {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires_at_ms,
            }
        }
        summary = cq.describe_credentials(data)

        self.assertIn("access_token=present(len=40)", summary)
        self.assertIn("refresh_token=present(len=64)", summary)
        self.assertIn("(valid)", summary)
        self.assertNotIn(access, summary)
        self.assertNotIn(refresh, summary)

    def test_no_tokens_no_expiry(self):
        summary = cq.describe_credentials({"claudeAiOauth": {}})
        self.assertIn("access_token=absent", summary)
        self.assertIn("refresh_token=absent", summary)
        self.assertIn("expires_at=unknown", summary)

    def test_missing_oauth_block_does_not_crash(self):
        summary = cq.describe_credentials({"somethingElse": True})
        self.assertIn("access_token=absent", summary)
        self.assertIn("refresh_token=absent", summary)

    def test_never_leaks_token_text_for_secret_like_values(self):
        secret = "sk-ant-oat01-super-secret-fragment-should-never-appear"
        data = {"claudeAiOauth": {"accessToken": secret, "refreshToken": secret}}
        summary = cq.describe_credentials(data)
        self.assertNotIn(secret, summary)
        self.assertNotIn(secret[:10], summary)


class DiagModeTests(unittest.TestCase):
    """Coverage for build_diag() / run_diag() (the --diag mode): structure,
    sanitization, and that it never touches the network."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.creds_path = Path(self.tmpdir.name) / ".credentials.json"
        self.diag_path = Path(self.tmpdir.name) / "diag.json"

        self._orig_creds = cq.CREDENTIALS_PATH
        self.addCleanup(setattr, cq, "CREDENTIALS_PATH", self._orig_creds)
        cq.CREDENTIALS_PATH = self.creds_path

        self._orig_diag = cq.DIAG_PATH
        self.addCleanup(setattr, cq, "DIAG_PATH", self._orig_diag)
        cq.DIAG_PATH = self.diag_path

        # The usage endpoint must never be called in --diag mode: fail loudly
        # if anything tries.
        self._orig_fetch = cq.fetch_usage_payload
        self.addCleanup(setattr, cq, "fetch_usage_payload", self._orig_fetch)

        def _must_not_be_called(token):
            raise AssertionError("fetch_usage_payload must not be called in --diag mode")

        cq.fetch_usage_payload = _must_not_be_called

    def _write_creds(self, oauth):
        self.creds_path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")

    def test_diag_structure_with_fixture_creds(self):
        access = "z" * 108
        refresh = "y" * 55
        expires_at_ms = int(cq.time.time() * 1000) - 1000  # expired
        self._write_creds(
            {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires_at_ms,
                "scopes": ["user:inference"],
                "subscriptionType": "pro",
            }
        )

        diag = cq.build_diag()
        dumped = json.dumps(diag)

        # top-level shape
        for key in (
            "generated_at",
            "python_version",
            "platform",
            "credentials_file",
            "credential_manager",
        ):
            self.assertIn(key, diag)

        creds_section = diag["credentials_file"]
        self.assertEqual(creds_section["path"], str(self.creds_path))
        self.assertTrue(creds_section["exists"])
        self.assertIsNotNone(creds_section["mtime"])
        self.assertIn("claudeAiOauth", creds_section["top_level_keys"])
        self.assertIn("accessToken", creds_section["oauth_keys"])
        self.assertIn("refreshToken", creds_section["oauth_keys"])
        self.assertTrue(creds_section["access_token_present"])
        self.assertEqual(creds_section["access_token_length"], 108)
        self.assertTrue(creds_section["refresh_token_present"])
        self.assertEqual(creds_section["refresh_token_length"], 55)
        self.assertTrue(creds_section["expired"])
        self.assertIsNotNone(creds_section["expires_at_iso"])
        self.assertEqual(creds_section["scopes"], ["user:inference"])
        self.assertEqual(creds_section["subscription_type"], "pro")

        cred_mgr = diag["credential_manager"]
        self.assertIn("available", cred_mgr)
        self.assertIn("claude_targets", cred_mgr)
        if sys.platform != "win32":
            self.assertFalse(cred_mgr["available"])
            self.assertEqual(cred_mgr["claude_targets"], [])

        # sanitization: no token text anywhere in the serialized diag
        self.assertNotIn(access, dumped)
        self.assertNotIn(refresh, dumped)

    def test_diag_handles_missing_credentials_file(self):
        # self.creds_path deliberately not written
        diag = cq.build_diag()
        creds_section = diag["credentials_file"]
        self.assertFalse(creds_section["exists"])
        self.assertIsNone(creds_section["mtime"])
        self.assertEqual(creds_section["top_level_keys"], [])
        self.assertFalse(creds_section["access_token_present"])
        self.assertFalse(creds_section["refresh_token_present"])

    def test_diag_handles_missing_refresh_token(self):
        self._write_creds({"accessToken": "a" * 20})
        diag = cq.build_diag()
        creds_section = diag["credentials_file"]
        self.assertTrue(creds_section["access_token_present"])
        self.assertFalse(creds_section["refresh_token_present"])
        self.assertEqual(creds_section["refresh_token_length"], 0)

    def test_run_diag_writes_file_and_never_calls_usage_endpoint(self):
        self._write_creds({"accessToken": "a" * 20, "refreshToken": "b" * 20})
        cq.run_diag()

        self.assertTrue(self.diag_path.exists())
        on_disk = json.loads(self.diag_path.read_text(encoding="utf-8"))
        self.assertIn("credentials_file", on_disk)
        self.assertIn("credential_manager", on_disk)
        self.assertNotIn("a" * 20, json.dumps(on_disk))

    def test_credential_manager_available_false_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("this assertion targets non-Windows behavior")
        result = cq._diag_credential_manager()
        self.assertEqual(result, {"available": False, "claude_targets": []})

    def test_diag_includes_scheduled_task_and_collector_process_sections(self):
        # build_diag() must never touch the network and (on this platform)
        # never actually shells out in a way that breaks the test -- both
        # new sections are exercised in isolation elsewhere; here we just
        # confirm build_diag() wires them into the top-level payload.
        diag = cq.build_diag()
        self.assertIn("scheduled_task", diag)
        self.assertIn("collector_process", diag)
        for key in ("available", "exists", "status", "task_to_run",
                    "last_run_time", "last_result", "raw_first_lines"):
            self.assertIn(key, diag["scheduled_task"])
        for key in ("available", "running", "pids"):
            self.assertIn(key, diag["collector_process"])

    def test_diag_includes_run_key_section(self):
        # Same idea as the scheduled_task/collector_process check above, for
        # the no-admin HKCU Run key autostart fallback.
        diag = cq.build_diag()
        self.assertIn("run_key", diag)
        for key in ("available", "exists", "value"):
            self.assertIn(key, diag["run_key"])


class DiagCliInvocationTests(unittest.TestCase):
    """--diag via sys.argv must not start the server/poller and must exit
    cleanly after writing DIAG_PATH."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.creds_path = Path(self.tmpdir.name) / ".credentials.json"
        self.creds_path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "a" * 20}}), encoding="utf-8"
        )
        self.diag_path = Path(self.tmpdir.name) / "diag.json"

        self._orig_creds = cq.CREDENTIALS_PATH
        self.addCleanup(setattr, cq, "CREDENTIALS_PATH", self._orig_creds)
        cq.CREDENTIALS_PATH = self.creds_path

        self._orig_diag = cq.DIAG_PATH
        self.addCleanup(setattr, cq, "DIAG_PATH", self._orig_diag)
        cq.DIAG_PATH = self.diag_path

        self._orig_argv = sys.argv
        self.addCleanup(setattr, sys, "argv", self._orig_argv)

        self._orig_fetch = cq.fetch_usage_payload
        self.addCleanup(setattr, cq, "fetch_usage_payload", self._orig_fetch)

        def _must_not_be_called(token):
            raise AssertionError("--diag must never call the usage endpoint")

        cq.fetch_usage_payload = _must_not_be_called

        self._orig_make_server = cq.make_server
        self.addCleanup(setattr, cq, "make_server", self._orig_make_server)

        def _must_not_be_called_server(port=None):
            raise AssertionError("--diag must never start the HTTP server")

        cq.make_server = _must_not_be_called_server

        # cq.main() calls _configure_file_logging() unconditionally (even in
        # --diag mode), which attaches a FileHandler to the root logger
        # pointed at cq.LOG_PATH. Point it at a temp file and detach
        # whatever handler(s) get attached afterward, so this test can never
        # leak log lines into the real collector.log for the rest of the run.
        self._orig_log_path = cq.LOG_PATH
        self.log_path = Path(self.tmpdir.name) / "collector.log"
        cq.LOG_PATH = self.log_path
        self.addCleanup(setattr, cq, "LOG_PATH", self._orig_log_path)

        self._root_logger = cq.logging.getLogger()
        self._orig_handlers = list(self._root_logger.handlers)
        self.addCleanup(self._detach_new_handlers)

    def _detach_new_handlers(self):
        leaked = _real_log_file_handler_attached()
        for handler in list(self._root_logger.handlers):
            if handler not in self._orig_handlers:
                self._root_logger.removeHandler(handler)
                handler.close()
        assert leaked is None, (
            f"test_main_with_diag_flag_writes_file_and_returns attached a "
            f"FileHandler targeting the real collector.log: {leaked!r}"
        )

    def test_main_with_diag_flag_writes_file_and_returns(self):
        sys.argv = ["claude_quota.py", "--diag"]
        cq.main()  # must return normally (i.e. exit 0), not start server/poller
        self.assertTrue(self.diag_path.exists())


class ScheduledTaskAndCollectorProcessDiagTests(unittest.TestCase):
    """Coverage for the "scheduled_task" and "collector_process" diag.json
    sections: the schtasks LIST parser (fixture-driven, no subprocess), and
    the non-Windows availability:False short-circuit for both."""

    # -- _parse_schtasks_list_output(): English labels ---------------------

    _ENGLISH_LIST_OUTPUT = (
        "\n"
        "Folder: \\\n"
        "HostName:                            DESKTOP-TEST\n"
        "TaskName:                            \\ClaudeQuotaCollector\n"
        "Next Run Time:                       8/13/2026 9:00:00 AM\n"
        "Status:                               Ready\n"
        "Logon Mode:                          Interactive/Background\n"
        "Last Run Time:                       8/12/2026 9:00:00 AM\n"
        "Last Result:                          0\n"
        "Author:                              DESKTOP-TEST\\user\n"
        "Task To Run:                         C:\\Users\\user\\AppData\\Local\\"
        "Programs\\Python\\Python311\\pythonw.exe C:\\path\\to\\claude_quota.py\n"
        "Start In:                            C:\\path\\to\n"
        "Comment:                             N/A\n"
        "Scheduled Task State:                Enabled\n"
    )

    def test_parser_extracts_known_english_fields(self):
        parsed = cq._parse_schtasks_list_output(self._ENGLISH_LIST_OUTPUT)
        self.assertEqual(parsed["status"], "Ready")
        self.assertEqual(parsed["last_run_time"], "8/12/2026 9:00:00 AM")
        self.assertEqual(parsed["last_result"], "0")
        self.assertIn("pythonw.exe", parsed["task_to_run"])
        self.assertIn("claude_quota.py", parsed["task_to_run"])

    def test_parser_raw_first_lines_capped_at_12_and_120_chars(self):
        long_line = "Comment:                             " + ("x" * 300)
        output = self._ENGLISH_LIST_OUTPUT + long_line + "\n" + "Extra: y\n" * 5
        parsed = cq._parse_schtasks_list_output(output)
        self.assertLessEqual(len(parsed["raw_first_lines"]), 12)
        for line in parsed["raw_first_lines"]:
            self.assertLessEqual(len(line), 120)

    def test_parser_ignores_blank_lines_for_raw_first_lines(self):
        output = "\n\n" + self._ENGLISH_LIST_OUTPUT
        parsed = cq._parse_schtasks_list_output(output)
        self.assertTrue(all(line.strip() for line in parsed["raw_first_lines"]))

    # -- _parse_schtasks_list_output(): localized / unrecognized labels ----

    _LOCALIZED_LIST_OUTPUT = (
        "\u6587\u4ef6\u5939: \\\n"
        "\u4e3b\u673a\u540d:                            DESKTOP-TEST\n"
        "\u4efb\u52a1\u540d:                            \\ClaudeQuotaCollector\n"
        "\u4e0b\u6b21\u8fd0\u884c\u65f6\u95f4:                8/13/2026 9:00:00 AM\n"
        "\u72b6\u6001:                               \u5c31\u7eea\n"
        "\u4e0a\u6b21\u8fd0\u884c\u65f6\u95f4:                8/12/2026 9:00:00 AM\n"
        "\u4e0a\u6b21\u7ed3\u679c:                       0\n"
    )

    def test_parser_leaves_fields_none_when_labels_unrecognized(self):
        parsed = cq._parse_schtasks_list_output(self._LOCALIZED_LIST_OUTPUT)
        self.assertIsNone(parsed["status"])
        self.assertIsNone(parsed["task_to_run"])
        self.assertIsNone(parsed["last_run_time"])
        self.assertIsNone(parsed["last_result"])
        # raw_first_lines still gives a human something to read
        self.assertTrue(len(parsed["raw_first_lines"]) > 0)
        self.assertIn("DESKTOP-TEST", " ".join(parsed["raw_first_lines"]))

    # -- non-Windows availability:False short-circuits ----------------------

    def test_scheduled_task_available_false_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("this assertion targets non-Windows behavior")
        result = cq._diag_scheduled_task()
        self.assertEqual(
            result,
            {
                "available": False,
                "exists": False,
                "status": None,
                "task_to_run": None,
                "last_run_time": None,
                "last_result": None,
                "raw_first_lines": [],
            },
        )

    def test_collector_process_available_false_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("this assertion targets non-Windows behavior")
        result = cq._diag_collector_process()
        self.assertEqual(
            result, {"available": False, "running": False, "pids": []}
        )

    def test_diag_scheduled_task_never_raises_when_schtasks_missing(self):
        # Simulate "schtasks invocation fails" (e.g. binary missing) by
        # monkeypatching subprocess.run to raise, regardless of platform --
        # available must come back False, never propagate the exception.
        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _boom(*args, **kwargs):
            raise FileNotFoundError("schtasks not found")

        cq.subprocess.run = _boom
        cq.sys.platform = "win32"
        try:
            result = cq._diag_scheduled_task()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertFalse(result["available"])
        self.assertFalse(result["exists"])

    def test_diag_collector_process_never_raises_when_powershell_missing(self):
        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _boom(*args, **kwargs):
            raise FileNotFoundError("powershell not found")

        cq.subprocess.run = _boom
        cq.sys.platform = "win32"
        try:
            result = cq._diag_collector_process()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertFalse(result["available"])
        self.assertFalse(result["running"])
        self.assertEqual(result["pids"], [])

    def test_diag_scheduled_task_parses_mocked_subprocess_output(self):
        # Full path (Windows-forced): subprocess.run mocked to return a
        # canned CompletedProcess, confirms _diag_scheduled_task() wires the
        # parser output into the result dict correctly.
        import types

        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _fake_run(*args, **kwargs):
            return types.SimpleNamespace(
                returncode=0, stdout=self._ENGLISH_LIST_OUTPUT, stderr=""
            )

        cq.subprocess.run = _fake_run
        cq.sys.platform = "win32"
        try:
            result = cq._diag_scheduled_task()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertTrue(result["available"])
        self.assertTrue(result["exists"])
        self.assertEqual(result["status"], "Ready")
        self.assertEqual(result["last_result"], "0")

    def test_diag_scheduled_task_not_found_sets_exists_false(self):
        import types

        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _fake_run(*args, **kwargs):
            return types.SimpleNamespace(
                returncode=1, stdout="ERROR: The system cannot find the file specified.\n", stderr=""
            )

        cq.subprocess.run = _fake_run
        cq.sys.platform = "win32"
        try:
            result = cq._diag_scheduled_task()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertTrue(result["available"])
        self.assertFalse(result["exists"])
        self.assertIsNone(result["status"])

    def test_diag_collector_process_parses_mocked_pids(self):
        import types

        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _fake_run(*args, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="1234\n5678\n", stderr="")

        cq.subprocess.run = _fake_run
        cq.sys.platform = "win32"
        try:
            result = cq._diag_collector_process()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertTrue(result["available"])
        self.assertTrue(result["running"])
        self.assertEqual(result["pids"], [1234, 5678])

    def test_diag_collector_process_no_matches_not_running(self):
        import types

        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _fake_run(*args, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        cq.subprocess.run = _fake_run
        cq.sys.platform = "win32"
        try:
            result = cq._diag_collector_process()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertTrue(result["available"])
        self.assertFalse(result["running"])
        self.assertEqual(result["pids"], [])

    # -- _diag_run_key(): HKCU Run key no-admin autostart fallback ---------

    _REG_QUERY_OUTPUT_FOUND = (
        "\n"
        "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\n"
        "    ClaudeQuotaCollector    REG_SZ    "
        "\"C:\\Users\\user\\AppData\\Local\\Programs\\Python\\Python311\\"
        "pythonw.exe\" \"C:\\path\\to\\claude_quota.py\"\n"
        "\n"
    )

    def test_run_key_available_false_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("this assertion targets non-Windows behavior")
        result = cq._diag_run_key()
        self.assertEqual(
            result, {"available": False, "exists": False, "value": None}
        )

    def test_diag_run_key_never_raises_when_reg_missing(self):
        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _boom(*args, **kwargs):
            raise FileNotFoundError("reg not found")

        cq.subprocess.run = _boom
        cq.sys.platform = "win32"
        try:
            result = cq._diag_run_key()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertFalse(result["available"])
        self.assertFalse(result["exists"])
        self.assertIsNone(result["value"])

    def test_diag_run_key_parses_mocked_reg_query_output(self):
        import types

        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _fake_run(*args, **kwargs):
            return types.SimpleNamespace(
                returncode=0, stdout=self._REG_QUERY_OUTPUT_FOUND, stderr=""
            )

        cq.subprocess.run = _fake_run
        cq.sys.platform = "win32"
        try:
            result = cq._diag_run_key()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertTrue(result["available"])
        self.assertTrue(result["exists"])
        self.assertIn("pythonw.exe", result["value"])
        self.assertIn("claude_quota.py", result["value"])

    def test_diag_run_key_not_found_sets_exists_false(self):
        import types

        orig_run = cq.subprocess.run
        orig_platform = cq.sys.platform

        def _fake_run(*args, **kwargs):
            return types.SimpleNamespace(
                returncode=1,
                stdout="ERROR: The system was unable to find the specified "
                "registry key or value.\n",
                stderr="",
            )

        cq.subprocess.run = _fake_run
        cq.sys.platform = "win32"
        try:
            result = cq._diag_run_key()
        finally:
            cq.subprocess.run = orig_run
            cq.sys.platform = orig_platform

        self.assertTrue(result["available"])
        self.assertFalse(result["exists"])
        self.assertIsNone(result["value"])


class RateLimit429Tests(unittest.TestCase):
    """On HTTP 429 from the usage endpoint: no refresh attempted, no retry
    this cycle, and the specific friendly error message is used."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_path = Path(self.tmpdir.name) / "latest.json"
        self.creds_path = Path(self.tmpdir.name) / ".credentials.json"
        self.creds_path.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "fake-token-xyz",
                        "refreshToken": "old-refresh",
                    }
                }
            ),
            encoding="utf-8",
        )

        self._orig_creds = cq.CREDENTIALS_PATH
        cq.CREDENTIALS_PATH = self.creds_path
        self.addCleanup(setattr, cq, "CREDENTIALS_PATH", self._orig_creds)

        self._orig_fetch = cq.fetch_usage_payload
        self.addCleanup(setattr, cq, "fetch_usage_payload", self._orig_fetch)

        self._orig_post_refresh = cq.post_oauth_refresh
        self.addCleanup(setattr, cq, "post_oauth_refresh", self._orig_post_refresh)

        self._orig_no_refresh = cq.NO_REFRESH
        cq.NO_REFRESH = False
        self.addCleanup(setattr, cq, "NO_REFRESH", self._orig_no_refresh)

        self._orig_auth_latch = dict(cq._auth_latch)
        cq._auth_latch = {"active": False, "mtime": None}
        self.addCleanup(setattr, cq, "_auth_latch", self._orig_auth_latch)

        self._orig_consecutive_429s = cq._consecutive_429s
        cq._consecutive_429s = 0
        self.addCleanup(setattr, cq, "_consecutive_429s", self._orig_consecutive_429s)

    def test_429_no_refresh_no_retry_exact_message(self):
        refresh_called = []
        cq.post_oauth_refresh = lambda rt: refresh_called.append(rt) or {
            "access_token": "should-not-be-used"
        }

        calls = []

        def fake_fetch(token):
            calls.append(token)
            raise cq.FetchError("HTTP 429", status_code=429)

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(
            result["error"],
            "HTTP 429 — rate limited by Anthropic; will retry next cycle "
            "(avoid restarting repeatedly)",
        )
        # exactly one usage call: no retry this cycle
        self.assertEqual(len(calls), 1)
        # no refresh attempted: 429 is not an auth failure
        self.assertEqual(refresh_called, [])

    def test_429_does_not_trigger_auth_failure_creds_log_line(self):
        cq.post_oauth_refresh = lambda rt: {"access_token": "should-not-be-used"}

        def fake_fetch(token):
            raise cq.FetchError("HTTP 429", status_code=429)

        cq.fetch_usage_payload = fake_fetch

        with self.assertLogs(cq.log, level="INFO") as ctx:
            cq.poll_once(self.data_path)

        self.assertFalse(any("creds state" in line for line in ctx.output))

    def test_401_still_logs_sanitized_creds_state_line(self):
        cq.NO_REFRESH = True

        def fake_fetch(token):
            raise cq.FetchError("HTTP 401", status_code=401)

        cq.fetch_usage_payload = fake_fetch

        with self.assertLogs(cq.log, level="INFO") as ctx:
            result = cq.poll_once(self.data_path)

        self.assertFalse(result["ok"])
        self.assertTrue(any("creds state:" in line for line in ctx.output))
        self.assertTrue(any("access_token=present" in line for line in ctx.output))
        self.assertNotIn("fake-token-xyz", "\n".join(ctx.output))
        self.assertNotIn("old-refresh", "\n".join(ctx.output))


class AuthLatchTests(unittest.TestCase):
    """Auth-failure latch (keyed on credentials file mtime): after a
    401-class outcome, subsequent cycles must skip all network activity as
    long as the credentials file's mtime is unchanged, and resume the
    instant it changes (a fresh login)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_path = Path(self.tmpdir.name) / "latest.json"
        self.creds_path = Path(self.tmpdir.name) / ".credentials.json"
        self.creds_path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "dead-token"}}),
            encoding="utf-8",
        )

        self._orig_creds = cq.CREDENTIALS_PATH
        cq.CREDENTIALS_PATH = self.creds_path
        self.addCleanup(setattr, cq, "CREDENTIALS_PATH", self._orig_creds)

        self._orig_fetch = cq.fetch_usage_payload
        self.addCleanup(setattr, cq, "fetch_usage_payload", self._orig_fetch)

        self._orig_post_refresh = cq.post_oauth_refresh
        self.addCleanup(setattr, cq, "post_oauth_refresh", self._orig_post_refresh)

        self._orig_no_refresh = cq.NO_REFRESH
        # Force a direct 401 (no refresh token in creds anyway) so these
        # tests exercise the latch itself, not the refresh flow.
        cq.NO_REFRESH = True
        self.addCleanup(setattr, cq, "NO_REFRESH", self._orig_no_refresh)

        self._orig_auth_latch = dict(cq._auth_latch)
        cq._auth_latch = {"active": False, "mtime": None}
        self.addCleanup(setattr, cq, "_auth_latch", self._orig_auth_latch)

        self._orig_consecutive_429s = cq._consecutive_429s
        cq._consecutive_429s = 0
        self.addCleanup(setattr, cq, "_consecutive_429s", self._orig_consecutive_429s)

    def _fail_401(self):
        def fake_fetch(token):
            raise cq.FetchError("HTTP 401", status_code=401)

        cq.fetch_usage_payload = fake_fetch

    def _fail_429(self):
        def fake_fetch(token):
            raise cq.FetchError("HTTP 429", status_code=429)

        cq.fetch_usage_payload = fake_fetch

    def _succeed(self):
        def fake_fetch(token):
            return {
                "five_hour": {"utilization": 1, "resets_at": "x"},
                "seven_day": {"utilization": 2, "resets_at": "y"},
            }

        cq.fetch_usage_payload = fake_fetch

    def test_latch_set_on_401_outcome(self):
        self._fail_401()
        cq.poll_once(self.data_path)
        self.assertTrue(cq._auth_latch["active"])
        self.assertEqual(cq._auth_latch["mtime"], os.path.getmtime(str(self.creds_path)))
        self.assertEqual(cq._last_poll_status, "auth")

    def test_second_cycle_same_mtime_skips_network_and_writes_exact_error(self):
        self._fail_401()
        cq.poll_once(self.data_path)

        fetch_calls = []

        def fetch_must_not_be_called(token):
            fetch_calls.append(token)
            raise AssertionError("fetch_usage_payload must not be called while latch active")

        cq.fetch_usage_payload = fetch_must_not_be_called

        refresh_calls = []

        def refresh_must_not_be_called(rt):
            refresh_calls.append(rt)
            raise AssertionError("post_oauth_refresh must not be called while latch active")

        cq.post_oauth_refresh = refresh_must_not_be_called

        result = cq.poll_once(self.data_path)

        self.assertEqual(fetch_calls, [])
        self.assertEqual(refresh_calls, [])
        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["error"], cq._AUTH_LATCH_ERROR)
        self.assertEqual(
            result["error"],
            "authentication failed — complete a fresh login: open a "
            "terminal, run claude, then /login (collector retries "
            "automatically once the credentials file changes)",
        )

    def test_second_cycle_logs_exactly_one_skip_line(self):
        self._fail_401()
        cq.poll_once(self.data_path)

        def fetch_must_not_be_called(token):
            raise AssertionError("must not be called")

        cq.fetch_usage_payload = fetch_must_not_be_called

        with self.assertLogs(cq.log, level="INFO") as ctx:
            cq.poll_once(self.data_path)

        skip_lines = [line for line in ctx.output if "skipping poll" in line]
        self.assertEqual(len(skip_lines), 1)

    def test_changed_mtime_clears_latch_and_calls_network_again(self):
        self._fail_401()
        cq.poll_once(self.data_path)
        self.assertTrue(cq._auth_latch["active"])

        # Simulate a fresh login rewriting the credentials file with a
        # distinctly different mtime.
        old_mtime = os.path.getmtime(str(self.creds_path))
        self.creds_path.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "new-token"}}),
            encoding="utf-8",
        )
        new_mtime = old_mtime + 120.0
        os.utime(str(self.creds_path), (new_mtime, new_mtime))

        calls = []

        def fake_fetch(token):
            calls.append(token)
            return {
                "five_hour": {"utilization": 1, "resets_at": "x"},
                "seven_day": {"utilization": 2, "resets_at": "y"},
            }

        cq.fetch_usage_payload = fake_fetch

        result = cq.poll_once(self.data_path)

        self.assertEqual(len(calls), 1)
        self.assertTrue(result["ok"])
        self.assertFalse(cq._auth_latch["active"])

    def test_success_clears_latch(self):
        self._fail_401()
        cq.poll_once(self.data_path)
        self.assertTrue(cq._auth_latch["active"])

        # A fresh login (mtime change) is required to get past the latch
        # check and reach a real network cycle; that cycle then succeeds.
        old_mtime = os.path.getmtime(str(self.creds_path))
        new_mtime = old_mtime + 60.0
        os.utime(str(self.creds_path), (new_mtime, new_mtime))

        self._succeed()
        result = cq.poll_once(self.data_path)

        self.assertTrue(result["ok"])
        self.assertFalse(cq._auth_latch["active"])
        self.assertIsNone(cq._auth_latch["mtime"])
        self.assertEqual(cq._last_poll_status, "ok")

    def test_429_does_not_set_latch(self):
        self._fail_429()
        cq.poll_once(self.data_path)
        self.assertFalse(cq._auth_latch["active"])
        self.assertIsNone(cq._auth_latch["mtime"])
        self.assertEqual(cq._last_poll_status, "429")


class NextPollDelayTests(unittest.TestCase):
    """next_poll_delay(): exponential backoff on consecutive 429 outcomes,
    reset by any other outcome."""

    def setUp(self):
        self._orig_poll_seconds = cq.POLL_SECONDS
        cq.POLL_SECONDS = 900
        self.addCleanup(setattr, cq, "POLL_SECONDS", self._orig_poll_seconds)

        self._orig_consecutive_429s = cq._consecutive_429s
        cq._consecutive_429s = 0
        self.addCleanup(setattr, cq, "_consecutive_429s", self._orig_consecutive_429s)

    def test_backoff_sequence(self):
        delays = [cq.next_poll_delay("429") for _ in range(4)]
        self.assertEqual(delays, [1800, 3600, 7200, 7200])

    def test_reset_after_success(self):
        cq.next_poll_delay("429")
        cq.next_poll_delay("429")
        self.assertEqual(cq.next_poll_delay("ok"), 900)
        self.assertEqual(cq.next_poll_delay("429"), 1800)  # sequence restarts

    def test_reset_after_non_429_failure_kinds(self):
        cq.next_poll_delay("429")
        self.assertEqual(cq.next_poll_delay("auth"), 900)
        self.assertEqual(cq.next_poll_delay("429"), 1800)

        cq.next_poll_delay("429")
        self.assertEqual(cq.next_poll_delay("other"), 900)
        self.assertEqual(cq.next_poll_delay("429"), 1800)

    def test_logs_when_delay_exceeds_poll_seconds(self):
        with self.assertLogs(cq.log, level="INFO") as ctx:
            cq.next_poll_delay("429")
        self.assertTrue(any("backing off" in line for line in ctx.output))

    def test_does_not_log_for_normal_interval(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs(cq.log, level="INFO"):
                cq.next_poll_delay("ok")


_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _payload(**windows):
    """Build a minimal normalized payload carrying only resets_at values."""
    return {
        key: {"utilization": 50.0, "resets_at": value}
        for key, value in windows.items()
    }


class ParseResetAtTests(unittest.TestCase):
    """_parse_reset_at(): tolerant ISO-8601 parsing, always aware UTC."""

    def test_parses_offset_form_the_api_actually_returns(self):
        parsed = cq._parse_reset_at("2026-08-12T11:39:59.948947+00:00")
        self.assertEqual(parsed, datetime(2026, 8, 12, 11, 39, 59, 948947, tzinfo=timezone.utc))

    def test_parses_trailing_z(self):
        # fromisoformat only accepts 'Z' natively on 3.11+; we normalize it.
        self.assertEqual(
            cq._parse_reset_at("2026-08-12T11:39:59Z"),
            datetime(2026, 8, 12, 11, 39, 59, tzinfo=timezone.utc),
        )

    def test_naive_timestamp_is_assumed_utc(self):
        self.assertEqual(
            cq._parse_reset_at("2026-08-12T11:39:59"),
            datetime(2026, 8, 12, 11, 39, 59, tzinfo=timezone.utc),
        )

    def test_non_utc_offset_is_converted(self):
        self.assertEqual(
            cq._parse_reset_at("2026-08-12T18:39:59+07:00"),
            datetime(2026, 8, 12, 11, 39, 59, tzinfo=timezone.utc),
        )

    def test_unparseable_and_missing_values_return_none(self):
        for value in (None, "", "   ", "not-a-date", 12345, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(cq._parse_reset_at(value))


class SecondsUntilNextResetTests(unittest.TestCase):
    """seconds_until_next_reset(): soonest *future* reset across windows."""

    def test_picks_soonest_future_window(self):
        data = _payload(
            five_hour=(_NOW + timedelta(minutes=10)).isoformat(),
            seven_day=(_NOW + timedelta(days=3)).isoformat(),
        )
        self.assertEqual(cq.seconds_until_next_reset(data, _NOW), 600)

    def test_seven_day_wins_when_it_is_sooner(self):
        data = _payload(
            five_hour=(_NOW + timedelta(hours=4)).isoformat(),
            seven_day=(_NOW + timedelta(minutes=5)).isoformat(),
        )
        self.assertEqual(cq.seconds_until_next_reset(data, _NOW), 300)

    def test_past_resets_are_ignored(self):
        data = _payload(
            five_hour=(_NOW - timedelta(minutes=30)).isoformat(),
            seven_day=(_NOW + timedelta(minutes=20)).isoformat(),
        )
        self.assertEqual(cq.seconds_until_next_reset(data, _NOW), 1200)

    def test_all_past_returns_none(self):
        data = _payload(five_hour=(_NOW - timedelta(minutes=1)).isoformat())
        self.assertIsNone(cq.seconds_until_next_reset(data, _NOW))

    def test_missing_malformed_and_non_dict_inputs_return_none(self):
        for data in (None, {}, "nope", 42, {"five_hour": None}, {"five_hour": "x"}):
            with self.subTest(data=data):
                self.assertIsNone(cq.seconds_until_next_reset(data, _NOW))

    def test_failure_skeleton_without_resets_is_safe(self):
        data = {"ok": False, "five_hour": {"utilization": None, "resets_at": None}}
        self.assertIsNone(cq.seconds_until_next_reset(data, _NOW))

    def test_post_rollover_payload_falls_back_to_the_other_window(self):
        """Exact shape observed live at a 5h rollover (2026-08-12 18:40 +0700):
        a *successful* poll can carry utilization 0.0 with resets_at None until
        the window restarts. The null window must be skipped, not crash or
        schedule a tight loop -- the 7-day boundary carries the cadence."""
        data = {
            "ok": True,
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {
                "utilization": 12.0,
                "resets_at": (_NOW + timedelta(days=3)).isoformat(),
            },
        }
        self.assertEqual(cq.seconds_until_next_reset(data, _NOW), 3 * 24 * 3600)
        # ...and that distant boundary must leave the normal cadence alone.
        self.assertEqual(cq.next_poll_delay("ok", data, _NOW), cq.POLL_SECONDS)


class ResetAwarePollDelayTests(unittest.TestCase):
    """next_poll_delay() pulls the next poll forward to just after an imminent
    window reset -- but never pushes it out, never below the floor, and never
    while backing off or auth-latched."""

    def setUp(self):
        self._orig_poll_seconds = cq.POLL_SECONDS
        cq.POLL_SECONDS = 900
        self.addCleanup(setattr, cq, "POLL_SECONDS", self._orig_poll_seconds)

        self._orig_consecutive_429s = cq._consecutive_429s
        cq._consecutive_429s = 0
        self.addCleanup(setattr, cq, "_consecutive_429s", self._orig_consecutive_429s)

    def _delay(self, status, seconds_ahead):
        data = _payload(five_hour=(_NOW + timedelta(seconds=seconds_ahead)).isoformat())
        return cq.next_poll_delay(status, data, _NOW)

    def test_imminent_reset_pulls_poll_forward_with_grace(self):
        # 200s to reset -> poll at 200 + _RESET_GRACE_SECONDS, well under 900.
        self.assertEqual(self._delay("ok", 200), 200 + cq._RESET_GRACE_SECONDS)

    def test_distant_reset_leaves_normal_cadence_untouched(self):
        # A 5h window mid-cycle must never stretch the interval past POLL_SECONDS.
        self.assertEqual(self._delay("ok", 4 * 3600), 900)

    def test_reset_just_beyond_poll_interval_does_not_extend(self):
        self.assertEqual(self._delay("ok", 1000), 900)

    def test_very_near_reset_is_floored(self):
        # Without the floor this would schedule a poll ~5s out.
        self.assertEqual(self._delay("ok", 5), cq._MIN_RESET_POLL_SECONDS)

    def test_backoff_ignores_imminent_reset(self):
        # A rate-limited collector must not be dragged back in by a boundary.
        self.assertEqual(self._delay("429", 30), 1800)

    def test_auth_latch_ignores_imminent_reset(self):
        self.assertEqual(self._delay("auth", 30), 900)

    def test_other_failure_ignores_imminent_reset(self):
        self.assertEqual(self._delay("other", 30), 900)

    def test_no_data_falls_back_to_normal_interval(self):
        self.assertEqual(cq.next_poll_delay("ok"), 900)

    def test_logs_when_pulling_forward(self):
        with self.assertLogs(cq.log, level="INFO") as ctx:
            self._delay("ok", 200)
        self.assertTrue(any("pulling next poll forward" in line for line in ctx.output))

    def test_does_not_log_when_cadence_unchanged(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs(cq.log, level="INFO"):
                self._delay("ok", 4 * 3600)

    def test_rollover_settles_back_to_normal_cadence(self):
        """The post-reset payload advertises a fresh ~5h boundary, so the
        pulled-forward poll costs exactly one extra request, not a fast loop."""
        self.assertEqual(self._delay("ok", 120), 120 + cq._RESET_GRACE_SECONDS)
        after_reset = _NOW + timedelta(seconds=135)
        fresh = _payload(five_hour=(after_reset + timedelta(hours=5)).isoformat())
        self.assertEqual(cq.next_poll_delay("ok", fresh, after_reset), 900)


class InitialPollDelayTests(unittest.TestCase):
    """initial_poll_delay(): pure, thread-free helper that defers the
    startup poll when the on-disk data file already reflects a recent 429."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_path = Path(self.tmpdir.name) / "latest.json"

        self._orig_poll_seconds = cq.POLL_SECONDS
        cq.POLL_SECONDS = 900
        self.addCleanup(setattr, cq, "POLL_SECONDS", self._orig_poll_seconds)

    def _write(self, error, age_seconds=0.0):
        data = {
            "ok": False,
            "stale": True,
            "error": error,
            "fetched_at": None,
            "five_hour": None,
            "seven_day": None,
        }
        cq._atomic_write_json(self.data_path, data)
        if age_seconds:
            t = cq.time.time() - age_seconds
            os.utime(str(self.data_path), (t, t))

    def test_fresh_429_state_defers_first_poll(self):
        self._write(cq._RATE_LIMIT_MESSAGE, age_seconds=60)
        delay = cq.initial_poll_delay(self.data_path)
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 900)
        self.assertAlmostEqual(delay, 840, delta=5)

    def test_old_429_state_polls_immediately(self):
        self._write(cq._RATE_LIMIT_MESSAGE, age_seconds=1000)
        self.assertEqual(cq.initial_poll_delay(self.data_path), 0)

    def test_non_429_error_polls_immediately(self):
        self._write("some other error", age_seconds=1)
        self.assertEqual(cq.initial_poll_delay(self.data_path), 0)

    def test_ok_state_polls_immediately(self):
        data = {
            "ok": True,
            "stale": False,
            "error": None,
            "fetched_at": "2026-08-12T08:00:00Z",
            "five_hour": {"utilization": 1, "resets_at": "x"},
            "seven_day": {"utilization": 2, "resets_at": "y"},
        }
        cq._atomic_write_json(self.data_path, data)
        self.assertEqual(cq.initial_poll_delay(self.data_path), 0)

    def test_missing_file_polls_immediately(self):
        self.assertEqual(cq.initial_poll_delay(self.data_path), 0)


# --------------------------------------------------------------------------
# Activity-driven idle pausing
# --------------------------------------------------------------------------


class _ActivityTempTree(unittest.TestCase):
    """Base fixture: an isolated fake ~/.claude/projects tree plus restored
    module-level idle config. Never touches the user's real transcripts."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="claude-quota-test-tx-")
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

        for name, value in (
            ("NO_IDLE", False),
            ("IDLE_AFTER_SECONDS", 300),
            ("IDLE_MAX_WAIT_SECONDS", 1800),
            ("TRANSCRIPTS_DIR", self.root),
        ):
            self.addCleanup(setattr, cq, name, getattr(cq, name))
            setattr(cq, name, value)

    def _transcript(self, name="session.jsonl", age_seconds=0.0, subdir="proj-a"):
        """Create a .jsonl whose mtime is `age_seconds` in the past."""
        directory = self.root / subdir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("{}\n", encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path


class NewestActivityMtimeTests(_ActivityTempTree):
    """newest_activity_mtime(): max mtime over the transcript tree, best
    effort, None on anything unusable."""

    def test_finds_newest_across_nested_project_dirs(self):
        self._transcript("old.jsonl", age_seconds=5000, subdir="proj-a")
        newest = self._transcript("new.jsonl", age_seconds=10, subdir="proj-b/nested")
        self.assertAlmostEqual(
            cq.newest_activity_mtime(self.root),
            os.path.getmtime(newest),
            places=3,
        )

    def test_ignores_non_jsonl_files(self):
        (self.root / "notes.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(cq.newest_activity_mtime(self.root))

    def test_missing_directory_returns_none(self):
        self.assertIsNone(cq.newest_activity_mtime(self.root / "does-not-exist"))

    def test_empty_tree_returns_none(self):
        self.assertIsNone(cq.newest_activity_mtime(self.root))


class SecondsSinceActivityTests(_ActivityTempTree):
    def test_reports_age_of_newest_transcript(self):
        self._transcript(age_seconds=120)
        age = cq.seconds_since_activity(self.root)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 120, delta=5)

    def test_none_when_no_transcripts(self):
        self.assertIsNone(cq.seconds_since_activity(self.root))

    def test_clock_skew_never_yields_negative_age(self):
        """A transcript mtime in the future (clock change, NTP step) must not
        produce a negative age that could confuse the comparison."""
        self._transcript(age_seconds=-600)
        self.assertEqual(cq.seconds_since_activity(self.root), 0.0)


class IsClaudeActiveTests(_ActivityTempTree):
    """is_claude_active(): the fail-open predicate. Only positive evidence of
    a quiet period may ever report idle."""

    def test_recent_transcript_is_active(self):
        self._transcript(age_seconds=5)
        self.assertTrue(cq.is_claude_active(self.root))

    def test_old_transcript_is_idle(self):
        self._transcript(age_seconds=3000)
        self.assertFalse(cq.is_claude_active(self.root))

    def test_boundary_is_inclusive(self):
        self._transcript(age_seconds=0)
        self.assertTrue(cq.is_claude_active(self.root, now=time.time() + 300))
        self.assertFalse(cq.is_claude_active(self.root, now=time.time() + 301))

    def test_missing_tree_fails_open_to_active(self):
        """A user whose transcripts live elsewhere must keep the old cadence,
        never get silently paused."""
        self.assertTrue(cq.is_claude_active(self.root / "nope"))

    def test_empty_tree_fails_open_to_active(self):
        self.assertTrue(cq.is_claude_active(self.root))

    def test_no_idle_flag_forces_active(self):
        cq.NO_IDLE = True
        self._transcript(age_seconds=99999)
        self.assertTrue(cq.is_claude_active(self.root))


class IdleExtensionAllowedTests(_ActivityTempTree):
    """idle_extension_allowed(): which cycles may be held. Guards the
    Phase 1 reset-aware behavior against regression."""

    def setUp(self):
        super().setUp()
        self._orig_poll_seconds = cq.POLL_SECONDS
        cq.POLL_SECONDS = 900
        self.addCleanup(setattr, cq, "POLL_SECONDS", self._orig_poll_seconds)

    def _allowed(self, status, seconds_ahead=None):
        data = (
            None
            if seconds_ahead is None
            else _payload(
                five_hour=(_NOW + timedelta(seconds=seconds_ahead)).isoformat()
            )
        )
        return cq.idle_extension_allowed(status, data, _NOW)

    def test_ok_cycle_with_distant_reset_may_be_held(self):
        self.assertTrue(self._allowed("ok", 4 * 3600))

    def test_ok_cycle_with_no_reset_data_may_be_held(self):
        self.assertTrue(self._allowed("ok"))

    def test_imminent_reset_is_never_held(self):
        """The rollover is the one event that moves the numbers while idle."""
        self.assertFalse(self._allowed("ok", 200))

    def test_null_resets_at_may_be_held(self):
        """Observed live just after a rollover: utilization 0, resets_at null."""
        data = _payload(five_hour=None, seven_day=None)
        self.assertTrue(cq.idle_extension_allowed("ok", data, _NOW))

    def test_backoff_and_auth_cycles_are_never_held(self):
        for status in ("429", "auth", "other"):
            with self.subTest(status=status):
                self.assertFalse(self._allowed(status, 4 * 3600))

    def test_no_idle_flag_disables_holding(self):
        cq.NO_IDLE = True
        self.assertFalse(self._allowed("ok", 4 * 3600))


class WaitForNextPollTests(_ActivityTempTree):
    """wait_for_next_poll(): the safety-critical piece. The base delay is
    always observed in full, so idle pausing can only ever ADD delay."""

    def setUp(self):
        super().setUp()
        self.waits = []

    def _stop_event(self, stop_after=None):
        """A stop_event stub recording every wait() and optionally signalling
        a stop on the Nth call."""
        waits = self.waits

        class _Stub:
            def __init__(self):
                self.calls = 0

            def wait(self, timeout):
                self.calls += 1
                waits.append(timeout)
                return stop_after is not None and self.calls >= stop_after

            def is_set(self):
                return False

        return _Stub()

    def test_base_delay_always_waited_in_full(self):
        self._transcript(age_seconds=5)  # active
        cq.wait_for_next_poll(self._stop_event(), 900, allow_idle=True, root=self.root)
        self.assertEqual(self.waits[0], 900)

    def test_active_user_adds_no_extra_delay(self):
        self._transcript(age_seconds=5)
        cq.wait_for_next_poll(self._stop_event(), 900, allow_idle=True, root=self.root)
        self.assertEqual(self.waits, [900])

    def test_allow_idle_false_adds_no_extra_delay(self):
        self._transcript(age_seconds=99999)  # idle, but holding not permitted
        cq.wait_for_next_poll(self._stop_event(), 900, allow_idle=False, root=self.root)
        self.assertEqual(self.waits, [900])

    def test_idle_user_holds_in_slices_up_to_heartbeat(self):
        self._transcript(age_seconds=99999)
        cq.wait_for_next_poll(self._stop_event(), 900, allow_idle=True, root=self.root)
        # 900 base, then 15s slices until the 1800s heartbeat cap.
        self.assertEqual(self.waits[0], 900)
        self.assertEqual(sum(self.waits), cq.IDLE_MAX_WAIT_SECONDS)
        self.assertTrue(all(w <= 15 for w in self.waits[1:]))

    def test_heartbeat_is_a_hard_cap(self):
        self._transcript(age_seconds=99999)
        cq.IDLE_MAX_WAIT_SECONDS = 1200
        cq.wait_for_next_poll(self._stop_event(), 900, allow_idle=True, root=self.root)
        self.assertEqual(sum(self.waits), 1200)

    def test_heartbeat_below_base_delay_never_shortens_the_wait(self):
        """A misconfigured heartbeat under POLL_SECONDS must not reduce the
        base delay -- it just means no extension happens."""
        self._transcript(age_seconds=99999)
        cq.IDLE_MAX_WAIT_SECONDS = 60
        cq.wait_for_next_poll(self._stop_event(), 900, allow_idle=True, root=self.root)
        self.assertEqual(self.waits, [900])

    def test_resumed_activity_ends_the_hold_within_one_slice(self):
        transcript = self._transcript(age_seconds=99999)
        event = self._stop_event()
        original_wait = event.wait

        def wait_and_touch(timeout):
            # After the base delay and two idle slices, the user comes back.
            result = original_wait(timeout)
            if event.calls == 3:
                now = time.time()
                os.utime(transcript, (now, now))
            return result

        event.wait = wait_and_touch
        cq.wait_for_next_poll(event, 900, allow_idle=True, root=self.root)
        self.assertEqual(self.waits, [900, 15, 15])
        self.assertLess(sum(self.waits), cq.IDLE_MAX_WAIT_SECONDS)

    def test_stop_during_base_delay_reports_stopping(self):
        self._transcript(age_seconds=5)
        self.assertTrue(
            cq.wait_for_next_poll(
                self._stop_event(stop_after=1), 900, allow_idle=True, root=self.root
            )
        )

    def test_stop_during_idle_hold_reports_stopping(self):
        self._transcript(age_seconds=99999)
        self.assertTrue(
            cq.wait_for_next_poll(
                self._stop_event(stop_after=2), 900, allow_idle=True, root=self.root
            )
        )

    def test_logs_hold_and_resume(self):
        transcript = self._transcript(age_seconds=99999)
        event = self._stop_event()
        original_wait = event.wait

        def wait_and_touch(timeout):
            result = original_wait(timeout)
            if event.calls == 2:
                now = time.time()
                os.utime(transcript, (now, now))
            return result

        event.wait = wait_and_touch
        with self.assertLogs(cq.log, level="INFO") as ctx:
            cq.wait_for_next_poll(event, 900, allow_idle=True, root=self.root)
        self.assertTrue(any("holding polls" in line for line in ctx.output))
        self.assertTrue(any("activity detected" in line for line in ctx.output))

    def test_active_hold_is_silent(self):
        self._transcript(age_seconds=5)
        with self.assertRaises(AssertionError):
            with self.assertLogs(cq.log, level="INFO"):
                cq.wait_for_next_poll(
                    self._stop_event(), 900, allow_idle=True, root=self.root
                )


class IdleConfigEnvTests(unittest.TestCase):
    """_env_int(): tolerant parsing of the idle-tuning env overrides."""

    def test_absent_uses_default(self):
        os.environ.pop("CLAUDE_QUOTA_TEST_INT", None)
        self.assertEqual(cq._env_int("CLAUDE_QUOTA_TEST_INT", 300, 60), 300)

    def test_blank_uses_default(self):
        with _env("CLAUDE_QUOTA_TEST_INT", "   "):
            self.assertEqual(cq._env_int("CLAUDE_QUOTA_TEST_INT", 300, 60), 300)

    def test_garbage_uses_default_and_warns(self):
        with _env("CLAUDE_QUOTA_TEST_INT", "soon"):
            with self.assertLogs(cq.log, level="WARNING"):
                self.assertEqual(cq._env_int("CLAUDE_QUOTA_TEST_INT", 300, 60), 300)

    def test_below_minimum_clamps_and_warns(self):
        with _env("CLAUDE_QUOTA_TEST_INT", "5"):
            with self.assertLogs(cq.log, level="WARNING"):
                self.assertEqual(cq._env_int("CLAUDE_QUOTA_TEST_INT", 300, 60), 60)

    def test_valid_override_is_honoured(self):
        with _env("CLAUDE_QUOTA_TEST_INT", "120"):
            self.assertEqual(cq._env_int("CLAUDE_QUOTA_TEST_INT", 300, 60), 120)


class ActivityDiagTests(_ActivityTempTree):
    """_diag_activity(): observable state for diagnose.bat, no transcript
    contents."""

    def test_reports_active_state_and_config(self):
        self._transcript(age_seconds=10)
        section = cq._diag_activity()
        self.assertTrue(section["idle_pausing_enabled"])
        self.assertTrue(section["transcripts_dir_exists"])
        self.assertTrue(section["active_now"])
        self.assertLess(section["seconds_since_activity"], 60)
        self.assertEqual(section["idle_after_seconds"], 300)

    def test_reports_idle_state(self):
        self._transcript(age_seconds=3000)
        section = cq._diag_activity()
        self.assertFalse(section["active_now"])
        self.assertGreater(section["seconds_since_activity"], 300)

    def test_missing_tree_reports_unknown_age_but_active(self):
        cq.TRANSCRIPTS_DIR = self.root / "nope"
        section = cq._diag_activity()
        self.assertFalse(section["transcripts_dir_exists"])
        self.assertIsNone(section["seconds_since_activity"])
        self.assertTrue(section["active_now"])

    def test_never_leaks_transcript_contents(self):
        path = self._transcript(age_seconds=10)
        path.write_text('{"secret":"sk-ant-not-a-real-token"}\n', encoding="utf-8")
        blob = json.dumps(cq._diag_activity())
        self.assertNotIn("sk-ant", blob)
        self.assertNotIn("secret", blob)


if __name__ == "__main__":
    unittest.main()
