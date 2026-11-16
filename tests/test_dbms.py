"""Stage 3 tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from dba_agent.errors import InvalidResponseError, LLMError
from dba_agent.prompts import PromptLoader
from dba_agent.stages import dbms
from dba_agent.state import load_session, new_session

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")
ALL = ("postgresql", "mysql_mariadb", "sqlite", "sql_server")


def choice(selected="postgresql"):
    return {
        "selected": selected, "summary": f"{selected} is the best fit.",
        "key_requirements": ["About 2 million rows | per year", "Many concurrent writers"],
        "justification": "Because of the stated volumes and rules.",
        "comparison": [{"criterion": c, "relevance": f"why {c} matters",
                        "ratings": {d: f"{d} on {c}" for d in ALL}} for c in dbms.CRITERIA_IDS],
        "rejected": [{"dbms": d, "reasons": [f"{d} is weaker here"]} for d in ALL if d != selected],
        "caveats": ["Needs tuning"], "design_implications": ["Use native enums"],
    }


class FakeLLM:
    def __init__(self, items):
        self.items, self.calls = list(items), []

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append((stage, system, user))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return validator(copy.deepcopy(item)) if validator else item


def session_with_requirements(tmp, text="Needs 300 bikes", unresolved=None):
    s = new_session(tmp)
    s.data["intake"] = {"project_name": "Demo"}
    s.data["requirements"] = {"summary": text, "categories": [], "open_issues": [],
                              "unresolved": unresolved or {}}
    s.complete_stage("intake", "interview")
    s.complete_stage("interview", "dbms")
    s.save()
    return s


class ValidatorTests(unittest.TestCase):
    def test_valid_choice_and_aliases(self):
        for alias, expected in (("PostgreSQL", "postgresql"), ("postgres", "postgresql"),
                                ("MySQL/MariaDB", "mysql_mariadb"), ("MariaDB", "mysql_mariadb"),
                                ("SQL Server", "sql_server"), ("MSSQL", "sql_server"), ("SQLite3", "sqlite")):
            self.assertEqual(dbms.dbms_id(alias), expected)
        out = dbms.validate_choice(choice("sqlite"))
        self.assertEqual((out["selected_id"], out["selected"]), ("sqlite", "SQLite"))
        self.assertEqual([r["dbms_id"] for r in out["rejected"]], ["postgresql", "mysql_mariadb", "sql_server"])
        self.assertEqual([r["criterion"] for r in out["comparison"]], dbms.CRITERIA_IDS)

    def test_unknown_dbms_rejected(self):
        with self.assertRaises(InvalidResponseError):
            dbms.validate_choice(dict(choice(), selected="Oracle"))

    def test_missing_rating_criterion_or_rejection(self):
        bad = choice(); del bad["comparison"][0]["ratings"]["sqlite"]
        with self.assertRaises(InvalidResponseError):
            dbms.validate_choice(bad)
        bad = choice(); bad["comparison"].pop()
        with self.assertRaises(InvalidResponseError):
            dbms.validate_choice(bad)
        bad = choice(); bad["rejected"].pop()
        with self.assertRaises(InvalidResponseError):
            dbms.validate_choice(bad)
        bad = choice(); bad["rejected"][0]["dbms"] = "postgresql"     # selected listed as rejected
        with self.assertRaises(InvalidResponseError):
            dbms.validate_choice(bad)
        bad = choice(); bad["key_requirements"] = []
        with self.assertRaises(InvalidResponseError):
            dbms.validate_choice(bad)

    def test_extra_criteria_allowed(self):
        c = choice()
        c["comparison"].append({"criterion": "Licensing", "relevance": "r", "ratings": {d: "x" for d in ALL}})
        out = dbms.validate_choice(c)
        self.assertEqual(out["comparison"][-1]["title"], "Licensing")


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def test_prompts_render_and_contain_no_hardcoded_scenario(self):
        sysp = self.prompts.render("dbms_system", dbms_list="L", criteria="C")
        usr = self.prompts.render("dbms_user", project_name="P", requirements="R")
        self.assertNotIn("{{", sysp + usr)
        for word in ("library", "hospital", "university", "bookstore", "student"):
            self.assertNotIn(word, (sysp + usr).lower())

    def test_run_saves_state_and_markdown(self):
        s = session_with_requirements(self.tmp.name, "Needs 300 bikes",
                                      {"skipped_questions": [{"round": 1, "category": "data_volume_growth",
                                                              "question": "Growth rate?"}]})
        c = choice("postgresql")
        c["comparison"][0]["ratings"]["sqlite"] = "fast | but single writer"
        llm = FakeLLM([c])
        dbms.run(s, llm, self.prompts)
        self.assertEqual(s.current_stage, "design")
        self.assertEqual(s.data["completed_stages"], ["intake", "interview", "dbms"])
        self.assertEqual(s.data["dbms_choice"]["selected_id"], "postgresql")
        self.assertIn("Needs 300 bikes", llm.calls[0][2])             # real requirements reach the prompt
        md = (s.output_dir / "dbms-choice.md").read_text(encoding="utf-8")
        self.assertIn("## Selected DBMS: PostgreSQL", md)
        header = [l for l in md.splitlines() if l.startswith("| Criterion")][0]
        for name in ("PostgreSQL (selected)", "MySQL/MariaDB", "SQLite", "SQL Server"):
            self.assertIn(name, header)
        table = [l for l in md.splitlines() if l.startswith("|")]
        self.assertEqual(len(table), 2 + len(dbms.CRITERIA))           # header + separator + one row each
        self.assertTrue(all(l.count("|") - l.count("\\|") == 7 for l in table))   # pipes in cells are escaped
        self.assertIn("fast \\| but single writer", md)
        for needle in ("## Why the Other Options Were Rejected", "### SQLite", "### SQL Server",
                       "### MySQL/MariaDB", "Growth rate?", "Needs tuning", "Use native enums"):
            self.assertIn(needle, md)
        reloaded = load_session(self.tmp.name, s.session_id)
        self.assertEqual(reloaded.data["dbms_choice"]["justification"], "Because of the stated volumes and rules.")

    def test_selection_follows_the_model_not_a_default(self):
        for sel in ("sqlite", "sql_server", "mysql_mariadb"):
            with tempfile.TemporaryDirectory() as tmp:
                s = session_with_requirements(tmp)
                dbms.run(s, FakeLLM([choice(sel)]), self.prompts)
                self.assertEqual(s.data["dbms_choice"]["selected_id"], sel)
                self.assertEqual(len(s.data["dbms_choice"]["rejected"]), 3)

    def test_requires_requirements(self):
        s = new_session(self.tmp.name)
        with self.assertRaises(InvalidResponseError):
            dbms.run(s, FakeLLM([]), self.prompts)

    def test_failure_keeps_stage_pending_and_retry_succeeds(self):
        s = session_with_requirements(self.tmp.name)
        with self.assertRaises(LLMError):
            dbms.run(s, FakeLLM([LLMError("down")]), self.prompts)
        s2 = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s2.current_stage, "dbms")
        self.assertNotIn("dbms_choice", s2.data)
        dbms.run(s2, FakeLLM([choice()]), self.prompts)
        self.assertEqual(s2.current_stage, "design")


if __name__ == "__main__":
    unittest.main()
