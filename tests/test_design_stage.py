"""Stage 4A orchestration: LLM calls, bounded retries, persistence, no premature completion."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import cli, workflow
from dba_agent.config import Config
from dba_agent.errors import InvalidResponseError, LLMError
from dba_agent.llm import LLMClient
from dba_agent.logger import LLMCallLogger
from dba_agent.prompts import PromptLoader
from dba_agent.stages import design
from dba_agent.state import load_session, new_session
from tests.design_fixtures import fresh

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")


def session_at_stage4(tmp, unresolved=None, open_issues=None, dbms="postgresql"):
    s = new_session(tmp, {"provider": "fake", "model": "m"})
    s.data["intake"] = {"project_name": "Clinic"}
    s.data["requirements"] = {"summary": "s", "categories": [], "open_issues": open_issues or [],
                              "unresolved": unresolved or {}}
    s.data["dbms_choice"] = {"selected_id": dbms, "selected": {"postgresql": "PostgreSQL", "sqlite": "SQLite"}.get(dbms, dbms), "design_implications": ["x"], "caveats": []}
    for a, b in (("intake", "interview"), ("interview", "dbms"), ("dbms", "design")):
        s.complete_stage(a, b)
    s.save()
    return s


class FakeLLM:
    def __init__(self, conceptual=None, logical=None):
        c, l = fresh()
        self.script = {"design:conceptual": [conceptual or c], "design:logical": [logical or l]}
        self.calls = []

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append((stage, system, user))
        item = copy.deepcopy(self.script[stage].pop(0))
        if isinstance(item, Exception):
            raise item
        return validator(item) if validator else item


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def test_generates_design_json_markdown_and_state(self):
        s = session_at_stage4(self.tmp.name)
        llm = FakeLLM()
        d = design.generate_design(s, llm, self.prompts)
        self.assertEqual([c[0] for c in llm.calls], ["design:conceptual", "design:logical"])
        on_disk = json.loads((s.output_dir / "design.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, d)
        self.assertEqual(s.data["design"], d)
        self.assertEqual(d["dbms"]["id"], "postgresql")
        md = (s.output_dir / "conceptual-model.md").read_text(encoding="utf-8")
        self.assertIn("```mermaid", md)
        self.assertIn("design.json", s.data["output_files"])

    def test_persistence_across_sessions(self):
        s = session_at_stage4(self.tmp.name)
        design.generate_design(s, FakeLLM(), self.prompts)
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s2.data["design"], s.data["design"])
        from dba_agent import design_model as dm
        self.assertEqual(dm.link_relationships(dm.validate_design(s2.data["design"])), s2.data["design"])
        llm = FakeLLM()
        design.generate_design(s2, llm, self.prompts)               # reuse: no LLM call at all
        self.assertEqual(llm.calls, [])

    def test_generate_design_alone_never_completes_stage4(self):
        """4A entry point (also used by `design-draft`): documents 4B and completion belong to design.run()."""
        s = session_at_stage4(self.tmp.name)
        design.generate_design(s, FakeLLM(), self.prompts)
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s2.current_stage, "design")
        self.assertNotIn("design", s2.data["completed_stages"])
        self.assertTrue(workflow.is_implemented("design"))
        for missing in ("logical-schema.md", "normalization.md", "assumptions.md"):
            self.assertFalse((s.output_dir / missing).exists())

    def test_requires_stage2_and_stage3(self):
        s = new_session(self.tmp.name)
        with self.assertRaises(InvalidResponseError):
            design.generate_design(s, FakeLLM(), self.prompts)
        s.data["requirements"] = {"summary": "x"}
        with self.assertRaises(InvalidResponseError) as cm:
            design.generate_design(s, FakeLLM(), self.prompts)
        self.assertIn("Stage 3", str(cm.exception))

    def test_prompts_carry_requirements_dbms_and_only_active_gaps(self):
        un = {"skipped_questions": [{"id": "r1q1", "question": "OLD RESOLVED QUESTION", "resolved": True}],
              "open_questions": [{"id": "r1q4", "question": "How many users?", "round": 1, "category": "data_volume_growth"}],
              "uncovered_categories": ["security_roles_privacy"], "remaining_gaps": [{"category": "x", "question": "Gap at limit"}]}
        s = session_at_stage4(self.tmp.name, un, ["Open issue from summary"], dbms="sqlite")
        llm = FakeLLM()
        design.generate_design(s, llm, self.prompts)
        user = llm.calls[0][2]
        for needle in ("How many users?", "Gap at limit", "Open issue from summary", "Security, user roles and privacy"):
            self.assertIn(needle, user)
        self.assertNotIn("OLD RESOLVED QUESTION", user)
        self.assertIn("sqlite", llm.calls[0][2].lower())
        self.assertIn("SQLite", llm.calls[1][1])                # dialect notes for the selected DBMS
        self.assertIn("INTEGER PRIMARY KEY", llm.calls[1][1])
        gaps = s.data["design"]["requirement_gaps"]
        self.assertIn("How many users?", gaps)
        self.assertNotIn("OLD RESOLVED QUESTION", gaps)

    def test_unresolved_requirements_must_become_assumptions_or_questions(self):
        c, _ = fresh()
        c["assumptions"], c["open_questions"] = [], []
        s = session_at_stage4(self.tmp.name, open_issues=["Opening hours unknown"])
        with self.assertRaises(InvalidResponseError) as cm:
            design.generate_design(s, FakeLLM(conceptual=c), self.prompts)
        self.assertIn("unresolved", str(cm.exception))
        s = session_at_stage4(self.tmp.name)                         # no gaps -> empty lists are fine
        design.generate_design(s, FakeLLM(conceptual=c), self.prompts)

    def test_conceptual_progress_survives_logical_failure(self):
        s = session_at_stage4(self.tmp.name)
        llm = FakeLLM(logical=LLMError("boom", retryable=False))
        with self.assertRaises(LLMError):
            design.generate_design(s, llm, self.prompts)
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertIn("conceptual", s2.data["design_progress"])
        self.assertNotIn("design", s2.data)
        self.assertFalse((s2.output_dir / "design.json").exists())
        llm2 = FakeLLM()
        design.generate_design(s2, llm2, self.prompts)
        self.assertEqual([c[0] for c in llm2.calls], ["design:logical"])      # conceptual call NOT repeated
        self.assertNotIn("design_progress", s2.data)

    def test_force_regenerates(self):
        s = session_at_stage4(self.tmp.name)
        design.generate_design(s, FakeLLM(), self.prompts)
        llm = FakeLLM()
        design.generate_design(s, llm, self.prompts, force=True)
        self.assertEqual(len(llm.calls), 2)

    def test_cross_validation_rejects_inconsistent_logical_output(self):
        s = session_at_stage4(self.tmp.name)
        _, l = fresh()
        l["tables"].pop(0)
        with self.assertRaises(InvalidResponseError):
            design.generate_design(s, FakeLLM(logical=l), self.prompts)


class DialectPromptTests(unittest.TestCase):
    def test_every_supported_dbms_has_a_nonempty_dialect_prompt(self):
        from dba_agent.stages.dbms import DBMS_IDS
        prompts = PromptLoader(PROMPTS)
        for dbms_id in DBMS_IDS:
            text = design._dialect_notes(prompts, dbms_id)
            self.assertGreater(len(text.strip()), 80, dbms_id)
            self.assertNotIn("Use standard SQL types valid", text, dbms_id)     # not the generic fallback


class ScriptedClient(LLMClient):
    """Real LLMClient (real retry loop + logging); only the transport is scripted."""
    def __init__(self, config, logger, replies):
        super().__init__(config, logger)
        self.replies, self.prompts_seen = list(replies), []

    def _chat(self, system, user):
        self.prompts_seen.append(user)
        return self.replies.pop(0)


class BoundedRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)
        self.cfg = Config(provider="ollama", model="m", base_url="http://x", max_attempts=3, retry_delay=0)

    def client(self, s, replies):
        return ScriptedClient(self.cfg, LLMCallLogger(s.log_path), replies)

    def test_invalid_then_valid_retries_with_error_feedback_and_logs_attempts(self):
        s = session_at_stage4(self.tmp.name)
        c, l = fresh()
        bad = copy.deepcopy(c)
        bad["relationships"][0]["participants"][0]["entity"] = "ghost"
        llm = self.client(s, [json.dumps(bad), json.dumps(c), json.dumps(l)])
        design.generate_design(s, llm, self.prompts)
        self.assertIn("ghost", llm.prompts_seen[1])                        # validation error fed back
        log = LLMCallLogger(s.log_path).read()
        conc = [e for e in log if e["stage"] == "design:conceptual"]
        self.assertEqual([e["attempt"] for e in conc], [1, 2])
        self.assertTrue(conc[0]["error"] and not conc[1]["error"])
        self.assertEqual([e["stage"] for e in log][-1], "design:logical")

    def test_gives_up_after_max_attempts_and_saves_nothing(self):
        s = session_at_stage4(self.tmp.name)
        llm = self.client(s, ["not json", "{}", '{"entities": []}'])
        with self.assertRaises(LLMError) as cm:
            design.generate_design(s, llm, self.prompts)
        self.assertIn("3 attempt", str(cm.exception))
        self.assertEqual(len(LLMCallLogger(s.log_path).read()), 3)
        self.assertNotIn("design", s.data)
        self.assertFalse((s.output_dir / "design.json").exists())

    def test_logical_stage_also_bounded(self):
        s = session_at_stage4(self.tmp.name)
        c, l = fresh()
        l["tables"] = []
        llm = self.client(s, [json.dumps(c)] + [json.dumps(l)] * 3)
        with self.assertRaises(LLMError):
            design.generate_design(s, llm, self.prompts)
        self.assertEqual([e["stage"] for e in LLMCallLogger(s.log_path).read()].count("design:logical"), 3)


class FakeClientForCli:
    def __init__(self, config, logger=None):
        self.inner = FakeLLM()

    def complete_json(self, *a, **k):
        return self.inner.complete_json(*a, **k)


class CliTests(unittest.TestCase):
    def test_design_draft_command_does_not_advance_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = session_at_stage4(tmp)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"provider": "ollama", "model": "m", "base_url": "http://x"}), encoding="utf-8")
            out = []
            with mock.patch.object(cli, "LLMClient", FakeClientForCli):
                code = cli.main(["--config", str(cfg), "--sessions-dir", tmp, "design-draft", s.session_id],
                                out=out.append)
            self.assertEqual(code, 0, out)
            s2 = load_session(tmp, s.session_id)
            self.assertEqual(s2.current_stage, "design")
            self.assertNotIn("design", s2.data["completed_stages"])
            self.assertTrue((s2.output_dir / "conceptual-model.md").is_file())
            self.assertTrue(any("NOT complete" in line for line in out))

    def test_resume_asks_before_starting_design_and_declining_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = session_at_stage4(tmp)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"provider": "ollama", "model": "m", "base_url": "http://x"}), encoding="utf-8")
            out = []
            code = cli.main(["--config", str(cfg), "--sessions-dir", tmp, "resume", s.session_id],
                            input_fn=lambda: "n", out=out.append)
            self.assertEqual(code, 0)
            self.assertTrue(any("Continue to stage 'design'" in line or "Stopped" in line for line in out), out)
            self.assertEqual(load_session(tmp, s.session_id).current_stage, "design")


if __name__ == "__main__":
    unittest.main()
