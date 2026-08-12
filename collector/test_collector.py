#!/usr/bin/env python3
"""Unit tests for claude_quota.py. Stdlib unittest only, no network access."""

import json
import os
import sys
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
