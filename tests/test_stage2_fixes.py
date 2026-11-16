"""Regression tests: Stage 2 must not treat skipped/insufficient answers as complete."""
import json
import tempfile
import unittest
from pathlib import Path

from dba_agent.prompts import PromptLoader
from dba_agent.stages import interview as iv
from dba_agent.state import load_session, new_session
from dba_agent.ui import UI

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")
INTAKE = {"project_name": "Demo", "summary": "s", "actors": [{"name": "a", "description": ""}],
          "entities": [{"name": "e", "description": ""}], "processes": [], "requirements": [], "ambiguities": []}
QUESTIONS = {"questions": [{"category": c, "question": f"Q {c}?"} for c in iv.CATEGORY_IDS]}
FOLLOW = {"enough_information": False, "reasoning": "More detail needed.",
          "follow_up_questions": [{"category": "data_volume_growth", "question": "How many exactly?"},
                                  {"category": "business_rules_constraints", "question": "Which rules?"}]}
ENOUGH = {"enough_information": True, "reasoning": "Looks complete.", "follow_up_questions": []}
REQS = {"summary": "S", "categories": [{"id": c, "findings": ["f"]} for c in iv.CATEGORY_IDS],
        "open_issues": []}


class FakeLLM:
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append((stage, user))
        item = json.loads(json.dumps(self.script[stage].pop(0)))
        return validator(item) if validator else item


def ui_for(answers):
    it = iter(answers)
    def f():
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return UI(f, lambda *_: None)


def make(tmp):
    s = new_session(tmp)
    s.data["intake"] = INTAKE
    s.complete_stage("intake", "interview")
    s.save()
    return s


class HelperTests(unittest.TestCase):
    def test_is_answered(self):
        for bad in (None, "", "  ", iv.SKIPPED, "idk", "?", "N/A", "I don't know"):
            self.assertFalse(iv.is_answered(bad), bad)
        for good in ("about 300", "none", "no"):
            self.assertTrue(iv.is_answered(good), good)

    def test_final_review_may_report_gaps_without_questions_being_asked(self):
        out = iv.validate_review(dict(FOLLOW), first_round=False, final=True)
        self.assertFalse(out["enough_information"])
        self.assertEqual(len(out["follow_up_questions"]), 2)
        out = iv.validate_review({"enough_information": False, "reasoning": "x"}, first_round=False, final=True)
        self.assertEqual(out["follow_up_questions"], [])

    def test_guard_leaves_covered_interview_alone(self):
        rounds = [{"number": 1, "kind": "initial", "review": None, "questions": [
            {"category": c, "question": "q", "answer": "real answer"} for c in iv.CATEGORY_IDS]}]
        review = iv.validate_review(ENOUGH, first_round=False)
        self.assertIs(iv.apply_coverage_guard(review, rounds), review)


class StageRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def test_all_skipped_then_llm_says_enough_is_overridden_and_questions_reasked(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, ENOUGH, ENOUGH],
                       "interview:requirements": [REQS]})
        # round 1 + round 2 entirely skipped, round 3 (re-asked) answered for real
        answers = [""] * 7 + [""] * 2 + [f"real {i}" for i in range(7)]
        iv.run(s, llm, self.prompts, ui_for(answers))
        rounds = s.data["interview"]["rounds"]
        self.assertEqual(len(rounds), 3)
        self.assertTrue(rounds[1]["review"].get("overridden"))
        self.assertFalse(rounds[1]["review"]["enough_information"])
        self.assertTrue(rounds[2]["questions"][0]["question"].startswith("(Skipped earlier"))
        un = s.data["requirements"]["unresolved"]
        self.assertEqual(un["uncovered_categories"], [])
        self.assertFalse(un["insufficient"])
        self.assertEqual(len(un["skipped_questions"]), 9)       # skipped items remain recorded
        md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("## Unresolved Requirements", md)
        self.assertNotIn("WARNING", md)
        # the requirements prompt told the model about the gaps
        self.assertIn("unresolved", [u for st, u in llm.calls if st == "interview:requirements"][0])

    def test_nothing_answered_until_limit_is_flagged_incomplete_not_silently_complete(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS],
                       "interview:review": [FOLLOW, FOLLOW, FOLLOW, ENOUGH],   # model claims "enough" at the end
                       "interview:requirements": [REQS]})
        iv.run(s, llm, self.prompts, ui_for([""] * (7 + 2 * 3)))
        last = s.data["interview"]["rounds"][-1]["review"]
        self.assertTrue(last["forced"])
        un = s.data["requirements"]["unresolved"]
        self.assertTrue(un["insufficient"])
        self.assertTrue(un["limit_reached"])
        self.assertEqual(un["uncovered_categories"], iv.CATEGORY_IDS)
        md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("WARNING - INCOMPLETE REQUIREMENTS", md)
        self.assertIn("No usable answer for category", md)
        self.assertEqual(s.current_stage, "dbms")               # still completes, but flagged

    def test_limit_with_real_gaps_preserves_them(self):
        s = make(self.tmp.name)
        gaps = {"enough_information": False, "reasoning": "Still unclear.",
                "follow_up_questions": [{"category": "security_roles_privacy", "question": "Who may delete records?"}]}
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, FOLLOW, FOLLOW, gaps],
                       "interview:requirements": [REQS]})
        iv.run(s, llm, self.prompts, ui_for(["real"] * (7 + 2 * 3)))
        un = s.data["requirements"]["unresolved"]
        self.assertTrue(un["limit_reached"])
        self.assertFalse(un["insufficient"])
        self.assertEqual(un["remaining_gaps"][0]["question"], "Who may delete records?")
        md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("Who may delete records?", md)
        self.assertIn("round limit", md.lower())
        # the final review prompt carried the FINAL-round note; earlier ones did not
        reviews = [u for st, u in llm.calls if st == "interview:review"]
        self.assertIn("FINAL round", reviews[-1])
        self.assertNotIn("FINAL round", reviews[0])
        # persisted state is what later stages read
        self.assertEqual(load_session(self.tmp.name, s.session_id).data["requirements"]["unresolved"]["limit_reached"], True)

    def test_resume_after_override_keeps_persistence_behaviour(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, ENOUGH, ENOUGH],
                       "interview:requirements": [REQS]})
        with self.assertRaises(EOFError):          # quits right after the override created round 3
            iv.run(s, llm, self.prompts, ui_for([""] * 9))
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertEqual(len(s2.data["interview"]["rounds"]), 3)
        self.assertEqual(s2.current_stage, "interview")
        iv.run(s2, llm, self.prompts, ui_for(["real"] * 7))
        self.assertEqual(s2.current_stage, "dbms")
        self.assertEqual(sum(1 for st, _ in llm.calls if st == "interview:questions"), 1)

    def test_legacy_session_without_unresolved_field_still_works(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, ENOUGH],
                       "interview:requirements": [REQS]})
        iv.run(s, llm, self.prompts, ui_for(["real"] * 9))
        del s.data["interview"]["unresolved"]       # simulate state written by the previous version
        del s.data["requirements"]["unresolved"]
        s.data["current_stage"] = "interview"
        iv.run(s, llm, self.prompts, ui_for([]))
        self.assertIn("unresolved", s.data["interview"])


if __name__ == "__main__":
    unittest.main()
