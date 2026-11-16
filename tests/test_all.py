import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import cli
from dba_agent.config import config_from_dict, load_config
from dba_agent.errors import ConfigError, InvalidResponseError, LLMError, PromptError, SessionError
from dba_agent.llm import LLMClient, extract_json
from dba_agent.logger import LLMCallLogger
from dba_agent.prompts import PromptLoader
from dba_agent.stages import intake
from dba_agent.state import load_session, new_session

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")

GOOD = {
    "project_name": "Pet Boarding",
    "summary": "A system to manage pet boarding bookings.",
    "actors": [{"name": "Owner", "description": "books stays", "responsibilities": ["book"]}, "Staff"],
    "entities": [{"name": "Pet", "description": "animal", "key_attributes": ["name", "species"]},
                 {"name": "Booking"}],
    "processes": [{"name": "Make booking", "description": "owner books", "actors": ["Owner"]}],
    "requirements": [{"type": "business rule", "description": "No overlapping bookings per kennel"}, "Keep history"],
    "ambiguities": [{"description": "Max stay length?", "why_it_matters": "CHECK constraint"}],
}


def cfg(**kw):
    raw = {"provider": "ollama", "model": "m", "retry_delay": 0, "max_attempts": 3}
    raw.update(kw)
    return config_from_dict(raw, env={})


class FakeLLM:
    """Stands in for LLMClient with canned, offline responses."""
    def __init__(self, responses):
        self.responses = list(responses)

    def complete_json(self, stage, system, user, validator=None):
        data = self.responses.pop(0)
        return validator(data) if validator else data


class ConfigTests(unittest.TestCase):
    def test_defaults_and_env_override(self):
        c = config_from_dict({"provider": "ollama", "model": "a"}, env={"DBA_AGENT_MODEL": "b"})
        self.assertEqual(c.model, "b")
        self.assertEqual(c.base_url, "http://localhost:11434")

    def test_api_key_from_env_var_name(self):
        c = config_from_dict({"provider": "openai_compatible", "model": "x", "api_key_env": "MYKEY"},
                             env={"MYKEY": "secret"})
        self.assertEqual(c.api_key, "secret")

    def test_missing_key_and_bad_provider(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"provider": "openai_compatible", "model": "x"}, env={})
        with self.assertRaises(ConfigError):
            config_from_dict({"provider": "nope", "model": "x"}, env={})
        with self.assertRaises(ConfigError):
            config_from_dict({"model": "x"}, env={})
        with self.assertRaises(ConfigError):
            config_from_dict({"provider": "ollama"}, env={})

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/config.json")

    def test_example_configs_valid(self):
        root = Path(PROMPTS).parent
        load_config(str(root / "config.example.json"))
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "k"}):
            load_config(str(root / "config.openai-compatible.example.json"))


class PromptTests(unittest.TestCase):
    def test_render_and_no_reexpansion(self):
        p = PromptLoader(PROMPTS)
        text = p.render("intake_user", description="hello {{description}} {x}")
        self.assertIn("hello {{description}} {x}", text)

    def test_missing_prompt_and_var(self):
        with self.assertRaises(PromptError):
            PromptLoader(PROMPTS).load("nope")
        with self.assertRaises(PromptError):
            PromptLoader(PROMPTS).render("intake_user")


class JsonExtractTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(extract_json('{"a":1}'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a":1} done'), {"a": 1})

    def test_invalid(self):
        for bad in ("", "no json", "{broken", "[1,2]"):
            with self.assertRaises(InvalidResponseError):
                extract_json(bad)


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logger = LLMCallLogger(Path(self.tmp.name) / "logs" / "l.jsonl")

    def test_ollama_request_and_log(self):
        client = LLMClient(cfg(), self.logger)
        with mock.patch.object(client, "_post", return_value={"message": {"content": '{"k": 1}'}}) as post:
            self.assertEqual(client.complete_json("intake", "sys", "usr"), {"k": 1})
        self.assertTrue(post.call_args[0][0].endswith("/api/chat"))
        (entry,) = self.logger.read()
        self.assertEqual((entry["stage"], entry["attempt"], entry["error"]), ("intake", 1, None))
        self.assertEqual(entry["input"], "usr")

    def test_openai_request(self):
        c = cfg(provider="openai_compatible", base_url="http://x/v1", api_key="k")
        client = LLMClient(c, self.logger)
        reply = {"choices": [{"message": {"content": '{"k": 2}'}}]}
        with mock.patch.object(client, "_post", return_value=reply) as post:
            self.assertEqual(client.complete_json("intake", "s", "u"), {"k": 2})
        url, payload, headers = post.call_args[0]
        self.assertEqual(url, "http://x/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer k")

    def test_retry_on_invalid_then_success_logs_attempts(self):
        client = LLMClient(cfg(), self.logger)
        replies = [{"message": {"content": "garbage"}}, {"message": {"content": '{"ok": true}'}}]
        with mock.patch.object(client, "_post", side_effect=replies):
            self.assertEqual(client.complete_json("intake", "s", "u"), {"ok": True})
        e1, e2 = self.logger.read()
        self.assertEqual((e1["attempt"], e2["attempt"]), (1, 2))
        self.assertIsNotNone(e1["error"])
        self.assertIsNone(e2["error"])
        self.assertIn("rejected", e2["input"])

    def test_connection_error_exhausts_attempts(self):
        client = LLMClient(cfg(max_attempts=2), self.logger)
        with mock.patch.object(client, "_post", side_effect=LLMError("Cannot connect")):
            with self.assertRaises(LLMError) as ctx:
                client.complete_json("intake", "s", "u")
        self.assertIn("2 attempt", str(ctx.exception))
        self.assertEqual(len(self.logger.read()), 2)

    def test_non_retryable_stops_immediately(self):
        client = LLMClient(cfg(), self.logger)
        with mock.patch.object(client, "_post", side_effect=LLMError("HTTP 401", retryable=False)):
            with self.assertRaises(LLMError):
                client.complete_json("intake", "s", "u")
        self.assertEqual(len(self.logger.read()), 1)


class IntakeTests(unittest.TestCase):
    def test_validate_normalizes(self):
        out = intake.validate_intake(GOOD)
        self.assertEqual(out["actors"][1]["name"], "Staff")
        self.assertEqual(out["requirements"][1]["description"], "Keep history")

    def test_validate_rejects(self):
        for mutate in (lambda d: d.pop("summary"), lambda d: d.update(entities=[]),
                       lambda d: d.update(actors="x"), lambda d: d.update(entities=[{"x": 1}])):
            d = json.loads(json.dumps(GOOD))
            mutate(d)
            with self.assertRaises(InvalidResponseError):
                intake.validate_intake(d)

    def test_run_writes_state_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = new_session(tmp)
            s.data["description"] = "A pet boarding place.\nSecond line."
            intake.run(s, FakeLLM([GOOD]), PromptLoader(PROMPTS))
            self.assertEqual(s.current_stage, "interview")
            self.assertEqual(s.data["completed_stages"], ["intake"])
            md = (s.output_dir / "intake-summary.md").read_text(encoding="utf-8")
            for needle in ("Pet Boarding", "Owner", "Booking", "No overlapping", "Max stay length?", "> Second line."):
                self.assertIn(needle, md)
            again = load_session(tmp, s.session_id)
            self.assertEqual(again.data["intake"]["project_name"], "Pet Boarding")

    def test_empty_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = new_session(tmp)
            with self.assertRaises(InvalidResponseError):
                intake.run(s, FakeLLM([]), PromptLoader(PROMPTS))


class SessionTests(unittest.TestCase):
    def test_roundtrip_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = new_session(tmp, {"provider": "ollama", "model": "m"})
            s.data["description"] = "x"
            s.save()
            self.assertEqual(load_session(tmp, s.session_id).data["description"], "x")
            with self.assertRaises(SessionError):
                load_session(tmp, "nope")
            (Path(tmp) / "bad").mkdir()
            (Path(tmp) / "bad" / "state.json").write_text("{not json")
            with self.assertRaises(SessionError):
                load_session(tmp, "bad")


class CLITests(unittest.TestCase):
    """End-to-end: new -> close -> resume -> logs, with a mocked network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "config.json"
        self.cfg_path.write_text(json.dumps({
            "provider": "ollama", "model": "test-model", "retry_delay": 0,
            "sessions_dir": str(Path(self.tmp.name) / "sessions"),
        }))
        self.lines = []
        self.out = self.lines.append
        self.base = ["--config", str(self.cfg_path)]

    def run_cli(self, args, inputs=(), post=None):
        it = iter(inputs)
        def input_fn(): 
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        patcher = mock.patch.object(LLMClient, "_post", side_effect=post) if post else mock.MagicMock()
        with patcher:
            return cli.main(self.base + args, input_fn=input_fn, out=self.out)

    def test_new_fail_resume_and_logs(self):
        # 1) connection failure: session saved, description kept
        rc = self.run_cli(["new"], ["A pet boarding place.", "."], post=LLMError("Cannot connect"))
        self.assertEqual(rc, 2)
        sessions = list((Path(self.tmp.name) / "sessions").iterdir())
        self.assertEqual(len(sessions), 1)
        state = json.loads((sessions[0] / "state.json").read_text())
        self.assertEqual(state["current_stage"], "intake")
        self.assertEqual(state["description"], "A pet boarding place.")
        self.assertIn("Cannot connect", state["last_error"]["message"])
        # 2) resume later with a working model
        good = {"message": {"content": json.dumps(GOOD)}}
        rc = self.run_cli(["resume"], post=[good])
        self.assertEqual(rc, 0)
        state = json.loads((sessions[0] / "state.json").read_text())
        self.assertEqual(state["current_stage"], "interview")
        self.assertIsNone(state["last_error"])
        self.assertTrue((sessions[0] / "output" / "intake-summary.md").is_file())
        # 3) resume again: no LLM call, summary shown
        self.lines.clear()
        self.assertEqual(self.run_cli(["resume"]), 0)
        self.assertTrue(any("Pet Boarding" in l for l in self.lines))
        # 4) logs + list
        self.lines.clear()
        self.assertEqual(self.run_cli(["logs"]), 0)
        self.assertTrue(any("attempt=1" in l and "ERROR" in l for l in self.lines))
        self.assertEqual(len(LLMCallLogger(sessions[0] / "logs" / "llm_calls.jsonl").read()), 4)
        self.assertEqual(self.run_cli(["list"]), 0)

    def test_new_from_file_and_missing_config(self):
        f = Path(self.tmp.name) / "d.txt"
        f.write_text("Some system", encoding="utf-8")
        good = {"message": {"content": json.dumps(GOOD)}}
        self.assertEqual(self.run_cli(["new", "--file", str(f)], post=[good]), 0)
        rc = cli.main(["--config", "/no/such.json", "new"], out=self.out)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
