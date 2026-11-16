"""Stage 2 fix: resolved issues leave the active unresolved list; 'not applicable' != missing."""
import tempfile
import unittest
from pathlib import Path

from dba_agent.prompts import PromptLoader
from dba_agent.stages import dbms
from dba_agent.stages import interview as iv
from dba_agent.state import load_session
from tests.test_stage2_fixes import ENOUGH, FOLLOW, QUESTIONS, REQS, FakeLLM, make, ui_for

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")


def rnd(number, answers, resolves=None, review=None):
    """Hand-built round: answers = list of (category, answer)."""
    return {"number": number, "kind": "initial" if number == 1 else "follow_up", "review": review,
            "questions": [{"id": f"r{number}q{i}", "category": c, "question": f"Q{number}.{i}", "why": "",
                           "answer": a, "resolves": (resolves or {}).get(i, [])}
                          for i, (c, a) in enumerate(answers, 1)]}


class AnswerKindTests(unittest.TestCase):
    def test_kinds(self):
        for text in ("N/A", "n/a", "NA", "Not applicable", "n/a - we have no online payments",
                     "Not applicable, single-user tool", "does not apply", "Doesn't apply here", "irrelevant"):
            self.assertEqual(iv.answer_kind(text), "not_applicable", text)
        for text in ("", "  ", iv.SKIPPED, "skip", "idk", "?", "unknown", "I don't know", None):
            self.assertEqual(iv.answer_kind(text), "missing", text)
        for text in ("about 300 users", "none", "no", "Nothing special, just admins", "national holidays"):
            self.assertEqual(iv.answer_kind(text), "answered", text)

    def test_is_answered_unchanged(self):
        self.assertFalse(iv.is_answered("n/a"))
        self.assertTrue(iv.is_resolved("n/a"))
        self.assertFalse(iv.is_resolved("idk"))

    def test_na_covers_a_category_except_entities(self):
        full = [(c, "real") for c in iv.CATEGORY_IDS]
        r = [rnd(1, [(c, "n/a" if c == "security_roles_privacy" else "real") for c in iv.CATEGORY_IDS])]
        self.assertEqual(iv.uncovered_categories(r), [])
        r = [rnd(1, [(c, "n/a" if c == "entities_attributes" else "real") for c in iv.CATEGORY_IDS])]
        self.assertEqual(iv.uncovered_categories(r), ["entities_attributes"])
        r = [rnd(1, [(c, "idk" if c == "security_roles_privacy" else "real") for c in iv.CATEGORY_IDS])]
        self.assertEqual(iv.uncovered_categories(r), ["security_roles_privacy"])
        self.assertEqual(iv.uncovered_categories([rnd(1, full)]), [])


class ComputeUnresolvedTests(unittest.TestCase):
    def test_na_is_not_unresolved_but_skipped_is(self):
        r = [rnd(1, [("entities_attributes", "real"), ("security_roles_privacy", "n/a - single user"),
                     ("data_volume_growth", ""), ("business_rules_constraints", "idk")],
                 review={"enough_information": True, "reasoning": "x"})]
        un = iv.compute_unresolved(r)
        self.assertEqual([q["id"] for q in un["not_applicable_questions"]], ["r1q2"])
        self.assertEqual([q["id"] for q in un["skipped_questions"]], ["r1q3", "r1q4"])
        self.assertEqual([q["id"] for q in un["open_questions"]], ["r1q3", "r1q4"])

    def test_later_answer_resolves_earlier_question_but_history_stays(self):
        r = [rnd(1, [("data_volume_growth", ""), ("security_roles_privacy", "admins only")]),
             rnd(2, [("data_volume_growth", "about 5000 rows/year")], resolves={1: ["r1q1"]})]
        r[0]["review"] = r[1]["review"] = {"enough_information": True, "reasoning": "x"}
        un = iv.compute_unresolved(r)
        self.assertEqual(len(un["skipped_questions"]), 1)                       # still recorded
        self.assertTrue(un["skipped_questions"][0]["resolved"])
        self.assertEqual(un["skipped_questions"][0]["resolved_by"], "r2q1")
        self.assertEqual(un["open_questions"], [])

    def test_not_resolved_if_resolving_question_was_also_skipped(self):
        r = [rnd(1, [("data_volume_growth", "")]), rnd(2, [("data_volume_growth", "idk")], resolves={1: ["r1q1"]})]
        r[1]["review"] = {"enough_information": True, "reasoning": "x"}
        un = iv.compute_unresolved(r)
        self.assertEqual({q["id"] for q in un["open_questions"]}, {"r1q1", "r2q1"})

    def test_na_answer_to_follow_up_resolves(self):
        r = [rnd(1, [("security_roles_privacy", "")]), rnd(2, [("security_roles_privacy", "N/A, no user accounts")],
                                                           resolves={1: ["r1q1"]})]
        r[1]["review"] = {"enough_information": True, "reasoning": "x"}
        self.assertEqual(iv.compute_unresolved(r)["open_questions"], [])

    def test_transitive_chain(self):
        r = [rnd(1, [("data_volume_growth", "")]), rnd(2, [("data_volume_growth", "")], resolves={1: ["r1q1"]}),
             rnd(3, [("data_volume_growth", "10k rows")], resolves={1: ["r2q1"]})]
        r[2]["review"] = {"enough_information": True, "reasoning": "x"}
        self.assertEqual(iv.compute_unresolved(r)["open_questions"], [])

    def test_unknown_or_self_links_ignored_and_unlinked_stay_open(self):
        r = [rnd(1, [("data_volume_growth", ""), ("business_rules_constraints", "")]),
             rnd(2, [("data_volume_growth", "ok")], resolves={1: ["nope", "r2q1"]})]
        r[1]["review"] = {"enough_information": True, "reasoning": "x"}
        self.assertEqual({q["id"] for q in iv.compute_unresolved(r)["open_questions"]}, {"r1q1", "r1q2"})

    def test_legacy_state_without_open_questions(self):
        legacy = {"skipped_questions": [{"question": "old"}]}
        self.assertEqual(iv.open_questions(legacy), [{"question": "old"}])
        self.assertEqual(iv.open_questions({"open_questions": [], "skipped_questions": [{"question": "x"}]}), [])
        self.assertEqual(iv.open_questions(None), [])


class ValidationTests(unittest.TestCase):
    def test_resolves_is_optional_and_parsed(self):
        out = iv.validate_review({"enough_information": False, "reasoning": "r", "follow_up_questions": [
            {"category": "data_volume_growth", "question": "q", "resolves": ["r1q3", " "]},
            {"category": "business_rules_constraints", "question": "q2"}]}, first_round=True)
        self.assertEqual(out["follow_up_questions"][0]["resolves"], ["r1q3"])
        self.assertNotIn("resolves", out["follow_up_questions"][1])
        out = iv.validate_review({"enough_information": False, "reasoning": "r", "follow_up_questions": [
            {"category": "data_volume_growth", "question": "q", "resolves": "r1q3"}]}, first_round=True)
        self.assertNotIn("resolves", out["follow_up_questions"][0])          # malformed -> ignored, not an error

    def test_new_round_filters_unknown_ids(self):
        r = iv._new_round(2, [{"category": "data_volume_growth", "question": "q", "resolves": ["r1q1", "zzz"]}], ["r1q1"])
        self.assertEqual(r["questions"][0]["resolves"], ["r1q1"])


class StageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def test_followup_that_answers_a_skipped_question_clears_it(self):
        s = make(self.tmp.name)
        follow = {"enough_information": False, "reasoning": "More.", "follow_up_questions": [
            {"category": "data_volume_growth", "question": "How many exactly?", "resolves": ["r1q3"]},
            {"category": "business_rules_constraints", "question": "Which rules?"}]}
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [follow, ENOUGH],
                       "interview:requirements": [REQS]})
        answers = ["real"] * 2 + [""] + ["real"] * 4 + ["about 5000 per year", "none"]
        iv.run(s, llm, self.prompts, ui_for(answers))
        un = s.data["requirements"]["unresolved"]
        self.assertEqual(un["open_questions"], [])
        self.assertEqual(len(un["skipped_questions"]), 1)                       # history kept
        self.assertTrue(un["skipped_questions"][0]["resolved"])
        md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("## Unresolved Requirements\n\n_None._", md)
        self.assertIn("(skipped)", md)                                          # transcript still shows it
        self.assertNotIn("interview_unresolved_note", md)
        req_prompt = [u for st, u in llm.calls if st == "interview:requirements"][0]
        self.assertNotIn("interview left the following gaps", req_prompt)       # nothing active -> no gap note
        # the reviewer was told which skipped questions exist (with ids) so it can link them
        review_prompt = [u for st, u in llm.calls if st == "interview:review"][0]
        self.assertIn("r1q3 [data_volume_growth]", review_prompt)
        self.assertEqual(load_session(self.tmp.name, s.session_id).data["requirements"]["unresolved"]["open_questions"], [])

    def test_coverage_guard_reask_resolves_the_quoted_question(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, ENOUGH, ENOUGH],
                       "interview:requirements": [REQS]})
        iv.run(s, llm, self.prompts, ui_for([""] * 9 + [f"real {i}" for i in range(7)]))
        un = s.data["requirements"]["unresolved"]
        self.assertEqual(len(un["skipped_questions"]), 9)                       # all history kept
        resolved = [q for q in un["skipped_questions"] if q["resolved"]]
        self.assertEqual(len(resolved), 7)
        self.assertTrue(all(q["resolved_by"].startswith("r3") for q in resolved))
        self.assertEqual({q["id"] for q in un["open_questions"]}, {"r2q1", "r2q2"})   # never re-asked -> still open
        md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
        self.assertEqual(md.count("- Unanswered (round"), 2)

    def test_not_applicable_answer_is_not_flagged_and_reaches_requirements_prompt(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, ENOUGH],
                       "interview:requirements": [REQS]})
        answers = ["real"] * 5 + ["n/a - nobody logs in"] + ["real"] + ["real", "real"]
        iv.run(s, llm, self.prompts, ui_for(answers))
        un = s.data["requirements"]["unresolved"]
        self.assertEqual(un["open_questions"], [])
        self.assertEqual(un["skipped_questions"], [])
        self.assertEqual(len(un["not_applicable_questions"]), 1)
        self.assertFalse(un["insufficient"])
        self.assertEqual(un["uncovered_categories"], [])
        req_prompt = [u for st, u in llm.calls if st == "interview:requirements"][0]
        self.assertIn("explicitly not applicable", req_prompt)

    def test_idk_everywhere_is_still_missing_not_na(self):
        s = make(self.tmp.name)
        llm = FakeLLM({"interview:questions": [QUESTIONS], "interview:review": [FOLLOW, FOLLOW, FOLLOW, ENOUGH],
                       "interview:requirements": [REQS]})
        iv.run(s, llm, self.prompts, ui_for(["idk"] * (7 + 2 * 3)))
        un = s.data["requirements"]["unresolved"]
        self.assertTrue(un["insufficient"])
        self.assertEqual(un["not_applicable_questions"], [])

    def test_stage3_prompt_and_markdown_use_only_active_gaps(self):
        un = {"skipped_questions": [{"round": 1, "category": "data_volume_growth", "question": "RESOLVED ONE", "resolved": True},
                                    {"round": 1, "category": "business_rules_constraints", "question": "STILL OPEN", "resolved": False}],
              "open_questions": [{"round": 1, "category": "business_rules_constraints", "question": "STILL OPEN"}],
              "uncovered_categories": [], "remaining_gaps": []}
        choice = dbms.validate_choice(__import__("tests.test_dbms", fromlist=["choice"]).choice("postgresql"))
        md = dbms.render_markdown(choice, {"unresolved": un}, "Demo")
        self.assertIn("STILL OPEN", md)
        self.assertNotIn("RESOLVED ONE", md)
        self.assertNotIn("RESOLVED ONE", str(iv.active_requirements({"unresolved": un})))


if __name__ == "__main__":
    unittest.main()
