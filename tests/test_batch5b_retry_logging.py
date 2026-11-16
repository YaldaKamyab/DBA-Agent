"""Batch 5B: Retry-After handling, avoidable requests, and credential redaction. Offline; urlopen and sleep are mocked."""
import io
import json
import tempfile
import unittest
from pathlib import Path
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from dba_agent.config import config_from_dict
from dba_agent.errors import InvalidResponseError, ProviderError
from dba_agent.llm import MAX_RETRY_AFTER_S, LLMClient, classify_http_error, parse_retry_after, redact
from dba_agent.logger import LLMCallLogger

KEY = "sk-live-ABCDEF1234567890XYZ"
RAW = {"provider": "openai_compatible", "model": "m1", "base_url": "https://api.example.test/v1",
       "api_key_env": "K", "max_attempts": 3, "retry_delay": 2}
OK = json.dumps({"choices": [{"message": {"content": '{"a": 1}'}}]}).encode()


def make(logger=None, **kw):
    return LLMClient(config_from_dict(dict(RAW, **kw), env={"K": KEY}), logger=logger)


def herr(code, body="{}", headers=None):
    return urllib.error.HTTPError("https://api.example.test/v1/chat/completions", code, "e", headers or {}, io.BytesIO(body.encode()))


class Resp:
    def __init__(self, data): self.data = data
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): return False


def run(client, effects):
    """Run complete_json with scripted urlopen results; returns (result_or_exc, urlopen_calls, sleeps)."""
    sleeps = []
    with mock.patch("urllib.request.urlopen", side_effect=effects) as m, mock.patch("dba_agent.llm.time.sleep", sleeps.append):
        try:
            out = client.complete_json("stage", "sys", "user")
        except Exception as e:  # noqa: BLE001
            out = e
    return out, m.call_count, sleeps


class RetryAfterParsing(unittest.TestCase):
    def test_seconds_http_date_and_invalid(self):
        self.assertEqual(parse_retry_after("7"), 7.0)
        self.assertEqual(parse_retry_after(" 2.5 "), 2.5)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now=now), 30.0)
        self.assertEqual(parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now=now), 0.0)
        for bad in (None, "", "soon", "-3", "nan", "inf", "Wed, 99 Foo 2026"):
            self.assertIsNone(parse_retry_after(bad), bad)

    def test_header_lookup_is_case_insensitive_and_only_for_429_503(self):
        self.assertEqual(classify_http_error(429, "{}", {"Retry-After": "4"}).retry_after, 4.0)
        self.assertEqual(classify_http_error(503, "{}", {"retry-after": "9"}).retry_after, 9.0)
        self.assertIsNone(classify_http_error(500, "{}", {"Retry-After": "9"}).retry_after)
        self.assertIsNone(classify_http_error(429, "{}", {"Retry-After": "soon"}).retry_after)
        # permanent quota never carries a wait
        e = classify_http_error(429, '{"error":{"code":"insufficient_quota"}}', {"Retry-After": "4"})
        self.assertEqual((e.kind, e.retryable, e.retry_after), ("quota", False, None))


class RetryBehaviour(unittest.TestCase):
    def test_retry_after_seconds_is_used_instead_of_default_backoff(self):
        out, calls, sleeps = run(make(), [herr(429, '{"error":{"message":"slow"}}', {"Retry-After": "7"}), Resp(OK)])
        self.assertEqual(out, {"a": 1}); self.assertEqual((calls, sleeps), (2, [7.0]))

    def test_retry_after_http_date(self):
        when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
        out, calls, sleeps = run(make(), [herr(503, "", {"Retry-After": when}), Resp(OK)])
        self.assertEqual(out, {"a": 1}); self.assertEqual(calls, 2); self.assertTrue(10 <= sleeps[0] <= 20)

    def test_malformed_retry_after_falls_back_to_default_backoff(self):
        out, calls, sleeps = run(make(), [herr(429, "{}", {"Retry-After": "soon"}), herr(503, "", {}), Resp(OK)])
        self.assertEqual(out, {"a": 1}); self.assertEqual((calls, sleeps), (3, [2, 4]))   # retry_delay * attempt

    def test_excessive_retry_after_stops_without_sleeping(self):
        out, calls, sleeps = run(make(), [herr(429, '{"error":{"message":"slow"}}', {"Retry-After": "3600"})] * 3)
        self.assertIsInstance(out, ProviderError)
        self.assertEqual((calls, sleeps, out.kind, out.status, out.retryable), (1, [], "rate_limit", 429, False))
        self.assertIn("3600s", str(out)); self.assertIn(str(int(MAX_RETRY_AFTER_S)), str(out)); self.assertIn("HTTP 429", str(out))

    def test_permanent_errors_make_one_request(self):
        for status, body in ((401, "{}"), (403, "{}"), (404, "{}"), (400, "{}"),
                             (429, '{"error":{"code":"insufficient_quota"}}'), (403, '{"error":{"message":"billing problem"}}')):
            out, calls, sleeps = run(make(), [herr(status, body, {"Retry-After": "1"})] * 3)
            self.assertIsInstance(out, ProviderError); self.assertEqual((calls, sleeps), (1, []), status)

    def test_transient_429_and_503_retry_within_limit_and_exhaust_clearly(self):
        for status in (429, 503):
            out, calls, sleeps = run(make(), [herr(status, "{}", {"x-request-id": "rid-9"})] * 5)
            self.assertIsInstance(out, ProviderError)
            self.assertEqual((calls, len(sleeps), out.status, out.request_id), (3, 2, status, "rid-9"))   # no sleep after last try
            self.assertIn("failed after 3 attempt(s)", str(out)); self.assertIn(f"HTTP {status}", str(out)); self.assertIn("rid-9", str(out))

    def test_success_stops_retrying(self):
        out, calls, sleeps = run(make(), [herr(503, "", {}), Resp(OK), Resp(OK)])
        self.assertEqual((out, calls, len(sleeps)), ({"a": 1}, 2, 1))
        out, calls, sleeps = run(make(), [Resp(OK), Resp(OK)])
        self.assertEqual((out, calls, sleeps), ({"a": 1}, 1, []))

    def test_max_attempts_one_never_sleeps(self):
        out, calls, sleeps = run(make(max_attempts=1), [herr(503, "", {"Retry-After": "5"})] * 2)
        self.assertEqual((calls, sleeps), (1, []))

    def test_invalid_response_retry_unchanged(self):
        bad = json.dumps({"choices": [{"message": {"content": "not json"}}]}).encode()
        out, calls, sleeps = run(make(), [Resp(bad), Resp(OK)])
        self.assertEqual((out, calls, sleeps), ({"a": 1}, 2, []))


class SecretProtection(unittest.TestCase):
    def test_redact_patterns_keep_business_text(self):
        t = redact('Authorization: Bearer abc123def456 {"api_key": "zzzzzzzz", "max_tokens": 50} '
                   'https://h/x?key=AIzaSy1234567890123456789012345678901&a=1 sk-abcdefghijklmnop1234 '
                   'Users need a password and an api key policy; store token counts.')
        for leaked in ("abc123def456", "zzzzzzzz", "AIzaSy1234", "sk-abcdefghijklmnop1234"):
            self.assertNotIn(leaked, t)
        self.assertIn('"max_tokens": 50', t); self.assertIn("a=1", t)
        self.assertIn("Users need a password and an api key policy; store token counts.", t)

    def test_exact_configured_key_is_removed(self):
        self.assertEqual(redact(f"oops {KEY} oops", KEY), "oops [REDACTED] oops")

    def test_provider_body_secret_not_in_error_but_diagnostics_kept(self):
        body = json.dumps({"error": {"message": f"Invalid key {KEY} for Bearer {KEY}", "code": "invalid_api_key", "request_id": "req-77"}})
        out, calls, _ = run(make(), [herr(401, body, {"Authorization": f"Bearer {KEY}"})])
        s = str(out)
        self.assertNotIn(KEY, s); self.assertNotIn("ABCDEF1234567890", s)
        for kept in ("HTTP 401", "req-77", "invalid_api_key", "api.example.test", "m1"):
            self.assertIn(kept, s)

    def test_long_provider_message_cannot_leak_truncated_key(self):
        body = json.dumps({"error": {"message": "x" * 285 + KEY}})
        self.assertNotIn(KEY[:10], str(classify_http_error(400, body, None, "h", "m", "", (KEY,))))

    def test_logger_never_writes_key_headers_or_credential_fields(self):
        with tempfile.TemporaryDirectory() as d:
            lg = LLMCallLogger(f"{d}/llm.jsonl")
            body = json.dumps({"error": {"message": f"bad {KEY}"}})
            out, calls, _ = run(make(logger=lg, max_attempts=2), [herr(503, body, {"Authorization": f"Bearer {KEY}"}), Resp(OK)])
            lg.log(stage="s", attempt=1, provider="p", model="m", system="sys", input='my "api_key": "hunter2hunter2"; need users table',
                   output=f"echo {KEY}", error=None, duration_s=1)
            raw = Path(f"{d}/llm.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(KEY, raw); self.assertNotIn("hunter2", raw); self.assertNotIn("Authorization", raw)
            entries = lg.read()
            self.assertEqual(len(entries), 3)
            self.assertIn("HTTP 503", entries[0]["error"]); self.assertEqual(entries[0]["attempt"], 1)
            self.assertIn("need users table", entries[2]["input"])

    def test_input_and_output_in_log_redacted_by_client(self):
        with tempfile.TemporaryDirectory() as d:
            lg = LLMCallLogger(f"{d}/llm.jsonl")
            c = make(logger=lg)
            data = json.dumps({"choices": [{"message": {"content": '{"k": "%s"}' % KEY}}]}).encode()
            with mock.patch("urllib.request.urlopen", side_effect=[Resp(data)]):
                c.complete_json("s", "sys", f"requirement mentions {KEY}")
            self.assertNotIn(KEY, Path(f"{d}/llm.jsonl").read_text(encoding="utf-8"))

    def test_config_repr_hides_key(self):
        self.assertNotIn(KEY, repr(make().config))

    def test_malformed_shape_message_is_redacted(self):
        c = make(max_attempts=1)
        with mock.patch.object(c, "_post", return_value={"weird": KEY}):
            with self.assertRaises(InvalidResponseError) as cm:
                c.complete_json("s", "sys", "u")
        self.assertNotIn(KEY, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
