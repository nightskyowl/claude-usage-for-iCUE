#!/usr/bin/env python3
"""Unit tests for claude_quota.py. Stdlib unittest only, no network access."""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_quota as cq


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

    def test_main_with_diag_flag_writes_file_and_returns(self):
        sys.argv = ["claude_quota.py", "--diag"]
        cq.main()  # must return normally (i.e. exit 0), not start server/poller
        self.assertTrue(self.diag_path.exists())


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


if __name__ == "__main__":
    unittest.main()
