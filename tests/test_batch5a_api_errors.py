"""Batch 5A: API configuration and error classification. Fully offline - urllib.request.urlopen is mocked."""
import io
import json
import socket
import urllib.error
from unittest import mock

from dba_agent import cli
from dba_agent.config import config_from_dict
from dba_agent.errors import (ConfigError, InvalidResponseError, LLMError, MalformedResponseError, ProviderError)
from dba_agent.llm import LLMClient, redact
from dba_agent.state import load_session
from tests.test_stage5a_revision import Base, ScriptedLLM, add_weight, response

KEY = "sk-SECRET-1234567890"
GAP = {"provider": "openai_compatible", "model": "gap-model-x", "base_url": "https://api.gapgpt.app/v1/",
       "api_key_env": "GAPGPT_API_KEY", "retry_delay": 0, "max_attempts": 1}
GEM = {"provider": "openai_compatible", "model": "gem-model-y", "api_key_env": "GEMINI_API_KEY", "json_mode": True,
       "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "retry_delay": 0, "max_attempts": 1}


def client(raw=GAP, **env):
    return LLMClient(config_from_dict(dict(raw), env=env or {"GAPGPT_API_KEY": KEY, "GEMINI_API_KEY": KEY}))


def http_error(code, body="", headers=None):
    return urllib.error.HTTPError("https://x/chat/completions", code, "err", headers or {}, io.BytesIO(body.encode()))


def fail(c, exc):
    with mock.patch("urllib.request.urlopen", side_effect=exc):
        try:
            c._chat("s", "u")
        except LLMError as e:
            return e
    raise AssertionError("no error raised")


class HttpStatusTests(Base):
    def test_401_is_authentication_and_names_env_var_not_key(self):
        e = fail(client(), http_error(401, '{"error":{"message":"bad key %s"}}' % KEY))
        self.assertIsInstance(e, ProviderError)
        self.assertEqual((e.kind, e.status, e.retryable), ("auth", 401, False))
        self.assertIn("HTTP 401", str(e)); self.assertIn("GAPGPT_API_KEY", str(e)); self.assertNotIn(KEY, str(e))

    def test_403_is_authorization_not_quota_without_evidence(self):
        e = fail(client(), http_error(403, '{"error":{"message":"Forbidden"}}', {"x-request-id": "req-42"}))
        self.assertEqual((e.kind, e.status, e.request_id), ("forbidden", 403, "req-42"))
        self.assertIn("HTTP 403", str(e)); self.assertIn("req-42", str(e)); self.assertNotIn("quota", str(e).lower())
        self.assertIn("not a design-revision result", str(e))

    def test_403_reports_quota_only_when_provider_says_so(self):
        e = fail(client(), http_error(403, '{"error":{"message":"You exceeded your current quota","code":"insufficient_quota"}}'))
        self.assertEqual(e.kind, "quota"); self.assertIn("quota", str(e).lower())

    def test_429_rate_limit_versus_quota(self):
        e = fail(client(), http_error(429, '{"error":{"message":"Too many requests"}}'))
        self.assertEqual((e.kind, e.retryable), ("rate_limit", True)); self.assertIn("rate limit", str(e))
        e = fail(client(), http_error(429, '{"error":{"message":"You exceeded your current quota, check billing","code":"insufficient_quota"}}'))
        self.assertEqual((e.kind, e.retryable), ("quota", False))
        # a bare quota mention (e.g. a per-minute limit) is not proof of permanent exhaustion
        e = fail(client(), http_error(429, json.dumps([{"error": {"message": "Quota exceeded for metric per minute", "status": "RESOURCE_EXHAUSTED"}}])))
        self.assertEqual((e.kind, e.retryable), ("rate_limit", True))
        e = fail(client(), http_error(429, '{"error":{"message":"slow down"}}', {"Retry-After": "5"}))
        self.assertEqual((e.kind, e.retryable), ("rate_limit", True))

    def test_exhausted_quota_is_not_retried_but_rate_limit_and_503_are(self):
        raw = dict(GAP, max_attempts=3)
        for body, status, calls in (('{"error":{"code":"insufficient_quota","message":"x"}}', 429, 1),
                                    ('{"error":{"message":"Too many requests"}}', 429, 3), ("", 503, 3)):
            c = LLMClient(config_from_dict(dict(raw), env={"GAPGPT_API_KEY": KEY}))
            with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(status, body))) as m:
                with self.assertRaises(ProviderError):
                    c.complete_json("x", "s", "u")
            self.assertEqual(m.call_count, calls)

    def test_invalid_api_key_types_give_config_error(self):
        for bad in (5, True, False, 0, ["k"], {"a": 1}, 1.5):
            for field in ("api_key", "api_key_env"):
                with self.assertRaises(ConfigError, msg=repr((field, bad))) as cm:
                    config_from_dict({"provider": "openai_compatible", "model": "m", "base_url": "http://h", field: bad}, env={"X": "k"})
                self.assertIn(field, str(cm.exception))

    def test_503_temporary_unavailable(self):
        e = fail(client(), http_error(503, "<html>down</html>"))
        self.assertEqual((e.kind, e.status, e.retryable), ("unavailable", 503, True)); self.assertIn("temporarily unavailable", str(e))

    def test_timeout_and_network_failures(self):
        for exc in (socket.timeout("t"), urllib.error.URLError(socket.timeout("t"))):
            e = fail(client(), exc)
            self.assertEqual(e.kind, "timeout"); self.assertIn("timed out", str(e))
        for exc in (urllib.error.URLError(OSError("name resolution")), ConnectionResetError("reset")):
            e = fail(client(), exc)
            self.assertEqual(e.kind, "network"); self.assertIsInstance(e, ProviderError)

    def test_secrets_are_redacted(self):
        self.assertNotIn(KEY, redact("Authorization: Bearer %s and %s" % (KEY, KEY), KEY))
        e = fail(client(), http_error(500, '{"error":{"message":"Authorization: Bearer %s"}}' % KEY))
        self.assertNotIn(KEY, str(e))

    def test_malformed_responses(self):
        c = client()
        for data in ({"choices": []}, {"nope": 1}, "str"):
            with mock.patch.object(c, "_post", return_value=data):
                with self.assertRaises(MalformedResponseError):
                    c._chat("s", "u")
        with mock.patch.object(c, "_post", return_value={"choices": [{"message": {"content": "no json here"}}]}):
            with self.assertRaises(MalformedResponseError):
                c.complete_json("x", "s", "u")

    def test_exhausted_retries_keep_error_class(self):
        c = client()
        with mock.patch.object(c, "_post", side_effect=ProviderError("HTTP 503", kind="unavailable", status=503)):
            with self.assertRaises(ProviderError) as cm:
                c.complete_json("x", "s", "u")
        self.assertEqual(cm.exception.status, 503); self.assertIn("failed after 1 attempt", str(cm.exception))
        with mock.patch.object(c, "_chat", return_value='{"a": 1}'):
            with self.assertRaises(InvalidResponseError) as cm:
                c.complete_json("x", "s", "u", validator=lambda d: (_ for _ in ()).throw(InvalidResponseError("bad shape")))
        self.assertNotIsInstance(cm.exception, MalformedResponseError)


class ConfigTests(Base):
    def test_gapgpt_and_gemini_endpoint_model_and_key(self):
        seen = []
        for raw, url in ((GAP, "https://api.gapgpt.app/v1/chat/completions"),
                         (GEM, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")):
            c = client(raw)
            with mock.patch.object(c, "_post", side_effect=lambda u, p, h: seen.append((u, p, h)) or {"choices": [{"message": {"content": "ok"}}]}):
                c._chat("s", "u")
        (u1, p1, h1), (u2, p2, h2) = seen
        self.assertEqual((u1, p1["model"], h1["Authorization"]), ("https://api.gapgpt.app/v1/chat/completions", "gap-model-x", "Bearer " + KEY))
        self.assertEqual((u2, p2["model"]), ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gem-model-y"))
        self.assertEqual(p2["response_format"], {"type": "json_object"}); self.assertNotIn("response_format", p1)

    def test_missing_or_empty_key_env_is_reported_clearly(self):
        with self.assertRaises(ConfigError) as cm:
            config_from_dict(dict(GEM), env={})
        self.assertIn("GEMINI_API_KEY is not set", str(cm.exception)); self.assertIn("gem-model-y", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            config_from_dict(dict(GAP), env={"GAPGPT_API_KEY": "   "})
        self.assertIn("GAPGPT_API_KEY is empty", str(cm.exception))

    def test_existing_json_configs_still_load_and_model_is_never_substituted(self):
        c = config_from_dict({"provider": "openai_compatible", "model": "m", "base_url": "http://h/v1", "api_key": "k"}, env={})
        self.assertEqual((c.model, c.base_url, c.api_key, c.api_key_env), ("m", "http://h/v1", "k", ""))
        c = config_from_dict(dict(GEM), env={"GEMINI_API_KEY": "k"})
        self.assertEqual((c.model, c.api_key_env), ("gem-model-y", "GEMINI_API_KEY"))
        self.assertEqual(config_from_dict({"provider": "ollama", "model": "q"}, env={}).model, "q")

    def test_switching_provider_leaves_session_untouched(self):
        before = (self.s.path.read_bytes() if hasattr(self.s, "path") else None, json.dumps(self.s.data, sort_keys=True))
        for raw in (GAP, GEM):
            cli.build_runtime(self.s, config_from_dict(dict(raw), env={"GAPGPT_API_KEY": "k", "GEMINI_API_KEY": "k"}))
        self.assertEqual(json.dumps(load_session(self.tmp.name, self.s.session_id).data, sort_keys=True), before[1])


class CliClassificationTests(Base):
    def run_cli(self, llm, argv=("--request", "track weight", "--yes")):
        out = []
        with mock.patch.object(cli, "build_runtime", lambda s, c: (llm, self.prompts)), \
                mock.patch.object(cli, "load_config", lambda p=None: config_from_dict({"provider": "ollama", "model": "m", "sessions_dir": self.tmp.name}, env={})):
            code = cli.main(["--sessions-dir", self.tmp.name, "revise", self.s.session_id] + list(argv), input_fn=iter(["change one", "change two", ""]).__next__, out=out.append)
        return code, "\n".join(map(str, out))

    def snapshot(self):
        return json.dumps(load_session(self.tmp.name, self.s.session_id).data, sort_keys=True), self.files()

    def test_provider_failures_are_never_called_revision_rejected(self):
        errs = [fail(client(), http_error(c, '{"error":{"message":"m"}}', {"x-request-id": "r1"})) for c in (401, 403, 429, 503)]
        errs += [fail(client(), socket.timeout("t")), fail(client(), urllib.error.URLError(OSError("dns")))]
        for e in errs:
            before = self.snapshot()
            code, text = self.run_cli(ScriptedLLM(self.s, e))
            self.assertEqual(code, 2)
            self.assertNotIn("REVISION REJECTED", text); self.assertIn("API ERROR", text)
            self.assertIn("no revision was evaluated", text)
            if e.status:
                self.assertIn(f"HTTP {e.status}", text); self.assertIn("r1", text)
            self.assertNotIn(KEY, text)
            self.assertEqual(self.snapshot(), before)                       # no session mutation, no records
        self.assertEqual(load_session(self.tmp.name, self.s.session_id).data.get("revisions", []), [])

    def test_malformed_model_response_message(self):
        before = self.snapshot()
        code, text = self.run_cli(ScriptedLLM(self.s, MalformedResponseError("Model response contains no JSON object.")))
        self.assertEqual(code, 2); self.assertIn("INVALID MODEL RESPONSE", text); self.assertNotIn("REVISION REJECTED", text)
        self.assertEqual(self.snapshot(), before)

    def test_real_local_rejection_still_reported_as_revision_rejected(self):
        # a well-formed reply that fails local validation (no change at all) is a genuine rejection
        llm = ScriptedLLM(self.s, lambda cur: response(cur, lambda d: None, ["add_attribute"]))
        code, text = self.run_cli(llm)
        self.assertEqual(code, 2); self.assertIn("REVISION REJECTED", text); self.assertNotIn("API ERROR", text)
        self.assertEqual(load_session(self.tmp.name, self.s.session_id).data.get("revisions", []), [])

    def test_auth_failure_stops_interactive_loop(self):
        e = fail(client(), http_error(401, ""))
        llm = ScriptedLLM(self.s, e, e)
        code, text = self.run_cli(llm, argv=())
        self.assertEqual(code, 2); self.assertEqual(len(llm.calls), 1)
