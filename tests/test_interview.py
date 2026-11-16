"""Stage 2 tests: validators, stage logic, interruption/resume, Stage1->Stage2 integration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import cli
from dba_agent.config import config_from_dict
from dba_agent.errors import InvalidResponseError, LLMError
from dba_agent.llm import LLMClient
from dba_agent.logger import LLMCallLogger
from dba_agent.prompts import PromptLoader
from dba_agent.stages import interview as iv
from dba_agent.state import load_session, new_session
from dba_agent.ui import UI

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")

INTAKE = {
    "project_name": "Bike Rental", "summary": "Rent bikes to tourists.",
    "actors": [{"name": "Customer", "description": ""}], "entities": [{"name": "Bike", "description": ""}],
    "processes": [], "requirements": [], "ambiguities": [],
}


def questions_payload(per_category=1):
    return {"questions": [{"category": c, "question": f"Question about {c} #{i}?", "why": "w"}
                          for c in iv.CATEGORY_IDS for i in range(per_category)]}


FOLLOW = {"enough_information": False, "reasoning": "Volumes are vague.",
          "follow_up_questions": [
              {"category": "data_volume_growth", "question": "You said 'a few hundred bikes' - how many exactly?"},
              {"category": "business_rules_constraints", "question": "What happens on late returns?"}]}
ENOUGH = {"enough_information": True, "reasoning": "All categories are clear.", "follow_up_questions": []}
REQS = {"summary": "Bike rental system.",
        "categories": [{"id": c, "findings": [f"Finding for {c}"]} for c in iv.CATEGORY_IDS],
        "open_issues": ["Late fee amount"]}


_RATING = {d: "ok" for d in ("postgresql", "mysql_mariadb", "sqlite", "sql_server")}
CHOICE = {
    "selected": "postgresql", "summary": "PostgreSQL fits.", "key_requirements": ["300 bikes"],
    "justification": "Strong constraints.",
    "comparison": [{"criterion": c, "relevance": "r", "ratings": _RATING}
                   for c in ("data_volume", "concurrency", "transactions_integrity", "security",
                             "reporting_analytics", "deployment", "maintainability")],
    "rejected": [{"dbms": d, "reasons": ["not needed"]} for d in ("mysql_mariadb", "sqlite", "sql_server")],
}


class FakeLLM:
    """Offline stand-in for LLMClient.complete_json; scripted per stage label."""
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append((stage, user))
        item = self.script[stage].pop(0)
        if isinstance(item, Exception):
            raise item
        return validator(json.loads(json.dumps(item))) if validator else item

    def count(self, stage):
        return sum(1 for s, _ in self.calls if s == stage)


def scripted_ui(answers):
    it = iter(answers)
    def input_fn():
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return UI(input_fn, lambda *_: None)


def fresh_session(tmp):
    s = new_session(tmp)
    s.data["intake"] = INTAKE
    s.complete_stage("intake", "interview")
    s.save()
    return s


class ValidatorTests(unittest.TestCase):
    def test_questions_must_cover_all_seven_categories(self):
        self.assertEqual(len(iv.validate_questions(questions_payload())), 7)
        data = questions_payload()
        data["questions"] = [q for q in data["questions"] if q["category"] != "security_roles_privacy"]
        with self.assertRaises(InvalidResponseError) as ctx:
            iv.validate_questions(data)
        self.assertIn("security_roles_privacy", str(ctx.exception))

    def test_category_titles_accepted_and_unknown_rejected(self):
        out = iv.validate_questions({"questions": [
            {"category": t, "question": "q?"} for t in iv.CATEGORY_TITLES.values()]})
        self.assertEqual({q["category"] for q in out}, set(iv.CATEGORY_IDS))
        with self.assertRaises(InvalidResponseError):
            iv._questions({"questions": [{"category": "bogus", "question": "q?"}]})

    def test_first_round_review_forces_follow_up(self):
        with self.assertRaises(InvalidResponseError):
            iv.validate_review(ENOUGH, first_round=True)
        forced = dict(FOLLOW, enough_information=True)       # model says "enough" in round 1
        out = iv.validate_review(forced, first_round=True)
        self.assertFalse(out["enough_information"])
        self.assertEqual(len(out["follow_up_questions"]), 2)

    def test_later_review_rules(self):
        self.assertTrue(iv.validate_review(ENOUGH, first_round=False)["enough_information"])
        with self.assertRaises(InvalidResponseError):
            iv.validate_review(dict(ENOUGH, enough_information=False), first_round=False)
        with self.assertRaises(InvalidResponseError):
            iv.validate_review({"enough_information": "yes", "reasoning": "x"}, first_round=False)

    def test_requirements_need_every_category(self):
        self.assertEqual(len(iv.validate_requirements(REQS)["categories"]), 7)
        bad = json.loads(json.dumps(REQS))
        bad["categories"].pop()
        with self.assertRaises(InvalidResponseError):
            iv.validate_requirements(bad)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def test_prompts_render_with_all_variables(self):
        for name in ("questions_system", "questions_user", "review_system", "review_user",
                     "requirements_system", "requirements_user"):
            text = self.prompts.render(f"interview_{name}", intake="I", categories="C",
                                       transcript="T", round_number=1, max_rounds=4)
            self.assertNotIn("{{", text)

    def test_full_interview_with_follow_up(self):
        s = fresh_session(self.tmp.name)
        llm = FakeLLM({"interview:questions": [questions_payload()],
                       "interview:review": [FOLLOW, ENOUGH],
                       "interview:requirements": [REQS]})
        answers = [f"answer {i}" for i in range(7)] + ["about 300 bikes", ""]
        iv.run(s, llm, self.prompts, scripted_ui(answers))
        self.assertEqual(s.current_stage, "dbms")
        self.assertEqual(s.data["completed_stages"], ["intake", "interview"])
        rounds = s.data["interview"]["rounds"]
        self.assertEqual([r["kind"] for r in rounds], ["initial", "follow_up"])
        self.assertEqual(rounds[1]["questions"][1]["answer"], iv.SKIPPED)
        # the review prompt really contains the user's answers
        review_prompt = [u for st, u in llm.calls if st == "interview:review"][0]
        self.assertIn("answer 3", review_prompt)
        # round-2 review sees follow-up answers too
        self.assertIn("about 300 bikes", [u for st, u in llm.calls if st == "interview:review"][1])
        md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
        for needle in ("Bike Rental", "Data volume and growth", "Late fee amount",
                       "about 300 bikes", "Rounds conducted: 2", "All categories are clear."):
            self.assertIn(needle, md)
        self.assertEqual(load_session(self.tmp.name, s.session_id).data["requirements"]["summary"],
                         "Bike rental system.")

    def test_interruption_mid_round_resumes_without_regenerating(self):
        s = fresh_session(self.tmp.name)
        llm = FakeLLM({"interview:questions": [questions_payload()],
                       "interview:review": [FOLLOW, ENOUGH],
                       "interview:requirements": [REQS]})
        with self.assertRaises(EOFError):                      # user quits after 3 answers
            iv.run(s, llm, self.prompts, scripted_ui(["a0", "a1", "a2"]))
        # reload from disk, as a new program run would
        s2 = load_session(self.tmp.name, s.session_id)
        answered = [q["answer"] for q in s2.data["interview"]["rounds"][0]["questions"]]
        self.assertEqual(answered, ["a0", "a1", "a2", None, None, None, None])
        self.assertEqual(s2.current_stage, "interview")
        iv.run(s2, llm, self.prompts, scripted_ui(["a3", "a4", "a5", "a6", "f0", "f1"]))
        self.assertEqual(llm.count("interview:questions"), 1)   # not regenerated
        done = s2.data["interview"]["rounds"][0]["questions"]
        self.assertEqual([q["answer"] for q in done], ["a0", "a1", "a2", "a3", "a4", "a5", "a6"])
        self.assertEqual(s2.current_stage, "dbms")

    def test_interruption_during_follow_up_round(self):
        s = fresh_session(self.tmp.name)
        llm = FakeLLM({"interview:questions": [questions_payload()],
                       "interview:review": [FOLLOW, ENOUGH],
                       "interview:requirements": [REQS]})
        with self.assertRaises(EOFError):
            iv.run(s, llm, self.prompts, scripted_ui([f"a{i}" for i in range(7)] + ["f0"]))
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertEqual(len(s2.data["interview"]["rounds"]), 2)
        self.assertIsNotNone(s2.data["interview"]["rounds"][0]["review"])
        iv.run(s2, llm, self.prompts, scripted_ui(["f1"]))
        self.assertEqual(llm.count("interview:review"), 2)      # round-1 review not repeated
        self.assertEqual(llm.count("interview:questions"), 1)

    def test_requirements_failure_keeps_answers_and_retries_only_that_call(self):
        s = fresh_session(self.tmp.name)
        llm = FakeLLM({"interview:questions": [questions_payload()],
                       "interview:review": [FOLLOW, ENOUGH],
                       "interview:requirements": [LLMError("down"), REQS]})
        answers = [f"a{i}" for i in range(7)] + ["f0", "f1"]
        with self.assertRaises(LLMError):
            iv.run(s, llm, self.prompts, scripted_ui(answers))
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s2.data["interview"]["status"], "complete")
        iv.run(s2, llm, self.prompts, scripted_ui([]))          # no more questions asked
        self.assertEqual(llm.count("interview:questions"), 1)
        self.assertEqual(llm.count("interview:review"), 2)
        self.assertTrue((s2.output_dir / "requirements.md").is_file())

    def test_round_limit_closes_interview(self):
        # the final round is still reviewed (to record remaining gaps); it is not asked again
        s = fresh_session(self.tmp.name)
        llm = FakeLLM({"interview:questions": [questions_payload()],
                       "interview:review": [FOLLOW] * iv.MAX_ROUNDS,
                       "interview:requirements": [REQS]})
        n_answers = 7 + 2 * (iv.MAX_ROUNDS - 1)
        iv.run(s, llm, self.prompts, scripted_ui(["x"] * n_answers))
        self.assertEqual(len(s.data["interview"]["rounds"]), iv.MAX_ROUNDS)
        self.assertTrue(s.data["interview"]["rounds"][-1]["review"]["forced"])
        self.assertEqual(s.current_stage, "dbms")

    def test_requires_intake(self):
        s = new_session(self.tmp.name)
        with self.assertRaises(InvalidResponseError):
            iv.run(s, FakeLLM({}), self.prompts, scripted_ui([]))


def fake_post(counter):
    """Network-level fake: answers by looking at which prompt was sent."""
    def post(url, payload, headers):
        system = payload["messages"][0]["content"]
        user = payload["messages"][1]["content"]
        if "system intake" in system:
            body = {"project_name": "Bike Rental", "summary": "Rent bikes.",
                    "actors": ["Customer"], "entities": ["Bike", "Rental"],
                    "processes": [], "requirements": [], "ambiguities": []}
            key = "intake"
        elif "FIRST round" in system:
            body, key = questions_payload(), "questions"
        elif "reviewing the answers" in system:
            body, key = (FOLLOW if "round 1 of" in user else ENOUGH), "review"
        elif "requirements specification" in system:
            body, key = REQS, "requirements"
        elif "performing DBMS selection" in system:
            body, key = CHOICE, "dbms"
        else:
            raise AssertionError("unexpected prompt")
        counter[key] = counter.get(key, 0) + 1
        return {"message": {"content": json.dumps(body)}}
    return post


class IntegrationTests(unittest.TestCase):
    """Stage 1 -> Stage 2 through the real CLI controller, network mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = Path(self.tmp.name) / "sessions"
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"provider": "ollama", "model": "m", "retry_delay": 0,
                                   "sessions_dir": str(self.sessions)}))
        self.base = ["--config", str(cfg)]
        self.lines = []
        self.counter = {}

    def run_cli(self, args, inputs):
        it = iter(inputs)
        def input_fn():
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        with mock.patch.object(LLMClient, "_post", side_effect=fake_post(self.counter)):
            return cli.main(self.base + args, input_fn=input_fn, out=self.lines.append)

    def session_dir(self):
        (d,) = list(self.sessions.iterdir())
        return d

    def test_stage1_to_stage2_in_one_run(self):
        answers = [f"ans {i}" for i in range(7)] + ["f-ans 0", "f-ans 1"]
        rc = self.run_cli(["new"], ["Bike rental shop.", ".", "y"] + answers)
        self.assertEqual(rc, 0)
        d = self.session_dir()
        state = json.loads((d / "state.json").read_text())
        self.assertEqual(state["current_stage"], "dbms")
        self.assertEqual(state["completed_stages"], ["intake", "interview"])
        self.assertTrue((d / "output" / "intake-summary.md").is_file())
        self.assertIn("f-ans 1", (d / "output" / "requirements.md").read_text())
        stages = [e["stage"] for e in LLMCallLogger(d / "logs" / "llm_calls.jsonl").read()]
        self.assertEqual(stages, ["intake", "interview:questions", "interview:review",
                                  "interview:review", "interview:requirements"])
        # resuming asks before Stage 3; "y" runs it, then the CLI asks before starting Stage 4 (no more input -> paused, nothing run)
        self.lines.clear()
        self.assertEqual(self.run_cli(["resume"], ["y"]), 0)
        self.assertTrue((d / "output" / "dbms-choice.md").is_file())
        state = json.loads((d / "state.json").read_text())
        self.assertEqual(state["current_stage"], "design")
        self.assertEqual(state["dbms_choice"]["selected_id"], "postgresql")
        self.assertEqual(state["completed_stages"], ["intake", "interview", "dbms"])
        self.assertTrue(any("Continue to stage 'design'" in l for l in self.lines), self.lines)
        stages = [e["stage"] for e in LLMCallLogger(d / "logs" / "llm_calls.jsonl").read()]
        self.assertEqual(stages[-1], "dbms:selection")

    def test_declining_stage2_then_resume_asks_again_and_interruption(self):
        self.assertEqual(self.run_cli(["new"], ["Bike rental shop.", ".", "n"]), 0)
        d = self.session_dir()
        self.assertEqual(json.loads((d / "state.json").read_text())["current_stage"], "interview")
        self.assertEqual(self.counter.get("questions"), None)
        # resume: confirm, answer only 2 questions, then the "terminal closes"
        self.assertEqual(self.run_cli(["resume"], ["y", "ans 0", "ans 1"]), 0)
        self.assertTrue(any("Paused during stage 'interview'" in l for l in self.lines))
        state = json.loads((d / "state.json").read_text())
        self.assertEqual(state["current_stage"], "interview")
        saved = [q["answer"] for q in state["interview"]["rounds"][0]["questions"]]
        self.assertEqual(saved[:3], ["ans 0", "ans 1", None])
        # second interruption: resume must NOT ask for confirmation and must keep going
        self.assertEqual(self.run_cli(["resume"], ["ans 2", "ans 3", "ans 4", "ans 5", "ans 6", "f0"]), 0)
        state = json.loads((d / "state.json").read_text())
        self.assertEqual(len(state["interview"]["rounds"]), 2)
        self.assertEqual(state["current_stage"], "interview")
        # final resume finishes the interview
        self.assertEqual(self.run_cli(["resume"], ["f1"]), 0)
        state = json.loads((d / "state.json").read_text())
        self.assertEqual(state["current_stage"], "dbms")
        self.assertEqual(self.counter["questions"], 1)       # generated once across 3 runs
        self.assertEqual(self.counter["review"], 2)
        self.assertEqual(self.counter["requirements"], 1)
        self.assertTrue((d / "output" / "requirements.md").is_file())

    def test_llm_failure_in_stage2_is_reported_and_retryable(self):
        self.run_cli(["new"], ["Bike rental shop.", ".", "n"])
        with mock.patch.object(LLMClient, "_post", side_effect=LLMError("Cannot connect")):
            rc = cli.main(self.base + ["resume"], input_fn=iter(["y"]).__next__, out=self.lines.append)
        self.assertEqual(rc, 2)
        state = json.loads((self.session_dir() / "state.json").read_text())
        self.assertEqual(state["current_stage"], "interview")
        self.assertIn("Cannot connect", state["last_error"]["message"])


if __name__ == "__main__":
    unittest.main()
