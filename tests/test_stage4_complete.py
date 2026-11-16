"""Stage 4 (4A + 4B) end to end: four documents, consistency checks, recovery, completion, Stage 5 hand-over.
Uses two different design scenarios (clinic / PostgreSQL and university / MySQL)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import cli, design_docs as dd, design_model as dm, workflow
from dba_agent.design_render import check_mermaid_syntax
from dba_agent.errors import InvalidResponseError, LLMError
from dba_agent.prompts import PromptLoader
from dba_agent.stages import design
from dba_agent.state import load_session, new_session
from tests.design_fixtures import fresh
from tests.design_fixtures2 import fresh2

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")
DBMS_NAMES = {"postgresql": "PostgreSQL", "mysql_mariadb": "MySQL/MariaDB", "sqlite": "SQLite"}
SCENARIOS = {"clinic": (fresh, "postgresql"), "university": (fresh2, "mysql_mariadb")}


def make_session(tmp, dbms="postgresql", open_issues=None):
    s = new_session(tmp, {"provider": "fake", "model": "m"})
    s.data["intake"] = {"project_name": "Demo"}
    s.data["requirements"] = {"summary": "s", "categories": [], "open_issues": open_issues or [], "unresolved": {}}
    s.data["dbms_choice"] = {"selected_id": dbms, "selected": DBMS_NAMES[dbms], "design_implications": ["x"], "caveats": []}
    for a, b in (("intake", "interview"), ("interview", "dbms"), ("dbms", "design")):
        s.complete_stage(a, b)
    s.save()
    return s


class FakeLLM:
    def __init__(self, scenario="clinic", **overrides):
        make, _ = SCENARIOS[scenario]
        c, l = make()
        self.script = {"design:conceptual": [overrides.get("conceptual", c)], "design:logical": [overrides.get("logical", l)]}
        self.calls = []

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append(stage)
        item = copy.deepcopy(self.script[stage].pop(0))
        if isinstance(item, Exception):
            raise item
        return validator(item) if validator else item


class NoCallsLLM:
    calls = []

    def complete_json(self, *a, **k):
        raise AssertionError("an LLM call was made but the saved design should have been reused")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def run_stage(self, scenario="clinic", **kw):
        s = make_session(self.tmp.name, SCENARIOS[scenario][1], kw.pop("open_issues", None))
        design.run(s, FakeLLM(scenario, **kw), self.prompts)
        return s

    def docs(self, s):
        return {n: (s.output_dir / n).read_text(encoding="utf-8") for n in dd.DOC_FILES}


class DocumentGenerationTests(Base):
    def test_all_four_documents_and_design_json_written_for_both_scenarios(self):
        for name in SCENARIOS:
            with self.subTest(name):
                s = self.run_stage(name)
                for f in dd.DOC_FILES + ["design.json"]:
                    self.assertTrue((s.output_dir / f).is_file(), f)
                    self.assertGreater(len((s.output_dir / f).read_text(encoding="utf-8")), 200, f)
                self.assertEqual(json.loads((s.output_dir / "design.json").read_text(encoding="utf-8")), s.data["design"])
                self.assertEqual(dd.check_documents(s.data["design"], self.docs(s)), [])

    def test_every_physical_table_is_represented_everywhere(self):
        for name in SCENARIOS:
            with self.subTest(name):
                s = self.run_stage(name)
                docs = self.docs(s)
                for t in s.data["design"]["physical"]["tables"]:
                    self.assertIn(f"### `{t['name']}`", docs["logical-schema.md"])
                    self.assertIn(f"### `{t['name']}`", docs["normalization.md"])
                    self.assertIn(f"    {t['name']} {{", docs["logical-schema.md"])        # diagram box
                    for c in t["columns"]:
                        self.assertIn(f"| `{c['name']}` | `{c['data_type']}` |", docs["logical-schema.md"])

    def test_keys_constraints_indexes_and_relationships_documented(self):
        s = self.run_stage("clinic")
        md = self.docs(s)["logical-schema.md"]
        for needle in ("**Primary key:** `pk_owners` (`owner_id`)", "`uq_owners_email`", "`ck_pets_species`",
                       "species IN ('dog','cat','other')", "`ix_pets_owner`", "list pets of an owner",
                       "| `fk_pets_owners` | `owner_id` | `owners` (`owner_id`) | RESTRICT | CASCADE | owner_pet |",
                       "| `fk_vets_supervisor` | `supervisor_id` | `vets` (`vet_id`) | SET NULL |",
                       "| `pet_vaccination` | many-to-many | junction table `pet_vaccinations`",
                       "| `owner_pet` | one-to-many | foreign key `fk_pets_owners` on table `pets`",
                       "| `status_values` | schema | ck_appointments_status |",
                       "| `no_booking_for_deceased` | application |"):
            self.assertIn(needle, md)
        self.assertIn("**Primary key:** `pk_pet_vaccinations` (`pet_id`, `vaccine_id`, `administered_on`)", md)

    def test_second_scenario_specifics(self):
        s = self.run_stage("university")
        docs = self.docs(s)
        self.assertIn("MySQL/MariaDB", docs["logical-schema.md"])
        self.assertIn("`credits BETWEEN 1 AND 10`", docs["logical-schema.md"])
        self.assertIn("| `enrollments` | (student_id, course_id) | 1NF | violation, justified denormalization |",
                      docs["normalization.md"])

    def test_logical_mermaid_diagram_is_valid_and_complete(self):
        for name in SCENARIOS:
            with self.subTest(name):
                s = self.run_stage(name)
                diagram = dd._mermaid(self.docs(s)["logical-schema.md"])
                self.assertEqual(check_mermaid_syntax(diagram), [])
                tables = s.data["design"]["physical"]["tables"]
                self.assertEqual(sorted(dd._diagram_entities(diagram)), sorted(t["name"] for t in tables))
                n_fk = sum(len(t["foreign_keys"]) for t in tables)
                self.assertEqual(sum(1 for ln in diagram.splitlines() if ' : "fk_' in ln), n_fk)
        self.assertIn("DECIMAL(8_2) fee", diagram)                    # comma in the type would break Mermaid

    def test_conceptual_diagram_still_valid(self):
        s = self.run_stage("clinic")
        self.assertEqual(check_mermaid_syntax(dd._mermaid(self.docs(s)["conceptual-model.md"])), [])

    def test_mermaid_checker_rejects_broken_diagrams(self):
        good = dd.mermaid_relational(self.run_stage("clinic").data["design"])
        self.assertTrue(check_mermaid_syntax(good.replace("erDiagram", "graph TD", 1)))
        self.assertTrue(check_mermaid_syntax(good.replace("    }\n", "", 1)))
        self.assertTrue(check_mermaid_syntax(good + "\n    ghost ||--o{ owners : \"x\""))

    def test_special_characters_in_descriptions_and_expressions_do_not_break_the_checker(self):
        c, l = fresh()
        l["tables"][0]["description"] = "Owners | with `ticks`\nand a newline"
        l["tables"][0]["columns"][1]["description"] = "Name with | pipe and `tick`"
        l["tables"][1]["check_constraints"][0]["expression"] = "species IN ('dog','cat','other') OR name LIKE 'a|b'"
        s = self.run_stage("clinic", conceptual=c, logical=l)
        self.assertEqual(dd.check_documents(s.data["design"], self.docs(s)), [])

    def test_documents_are_deterministic(self):
        s = self.run_stage("clinic")
        self.assertEqual(dd.render_all(s.data["design"]), self.docs(s))


class NormalizationAndAssumptionTests(Base):
    def test_findings_and_justified_denormalization_documented(self):
        s = self.run_stage("university")
        md = self.docs(s)["normalization.md"]
        self.assertIn("- **2NF:** VIOLATED", md)
        self.assertIn("Partial dependency: non-prime column course_title depends on (course_id)", md)
        self.assertIn("Transcripts must keep the title", md)
        self.assertIn("## Limitations", md)
        self.assertIn("cannot be detected", md)
        self.assertEqual(s.data["design_docs"]["warnings"], [])

    def test_unjustified_violation_becomes_a_warning_everywhere(self):
        s = self.run_stage("university", logical=fresh2(justified=False)[1])
        self.assertIn("VIOLATION, not justified", self.docs(s)["normalization.md"])
        self.assertIn("has no justification", self.docs(s)["normalization.md"])
        self.assertIn("has no justification", self.docs(s)["assumptions.md"])
        self.assertTrue(s.data["design_docs"]["warnings"])
        self.assertIn("design", s.data["completed_stages"])           # a warning does not block the stage

    def test_clean_design_has_no_violations(self):
        md = self.docs(self.run_stage("clinic"))["normalization.md"]
        self.assertNotIn("VIOLATED", md)
        self.assertIn("_None. The schema is not deliberately denormalized._", md)

    def test_assumptions_open_questions_and_unresolved_requirements(self):
        s = self.run_stage("clinic", open_issues=["Opening hours unknown"])
        md = self.docs(s)["assumptions.md"]
        self.assertIn("**A1:** A pet has exactly one owner.", md)
        self.assertIn("**T1:** Surrogate integer keys", md)                  # assumption added by the logical step
        self.assertIn("**Q1:** Are pets ever co-owned?", md)
        self.assertIn("- Opening hours unknown", md)
        self.assertIn("**no_booking_for_deceased** (application)", md)       # rule the schema cannot enforce
        self.assertIn("## Unresolved Requirements from the Interview", md)


class ConsistencyCheckerTests(Base):
    def setUp(self):
        super().setUp()
        self.s = self.run_stage("clinic")
        self.design = self.s.data["design"]
        self.docs0 = self.docs(self.s)

    def problems(self, name, old, new, count=1):
        docs = dict(self.docs0)
        self.assertIn(old, docs[name], f"test bug: {old!r} not in {name}")
        docs[name] = docs[name].replace(old, new, count)
        return dd.check_documents(self.design, docs)

    def test_pristine_documents_pass(self):
        self.assertEqual(dd.check_documents(self.design, self.docs0), [])

    def test_missing_or_empty_file(self):
        for name in dd.DOC_FILES:
            docs = dict(self.docs0)
            del docs[name]
            self.assertTrue(any(name in p and "missing" in p for p in dd.check_documents(self.design, docs)), name)
            docs[name] = "   \n"
            self.assertTrue(dd.check_documents(self.design, docs), name)

    def test_altered_column_facts_detected(self):
        for old, new in (("`VARCHAR(120)`", "`VARCHAR(20)`"),                       # data type
                         ("| `phone` | `VARCHAR(30)` | NULL |", "| `phone` | `VARCHAR(30)` | NOT NULL |"),   # nullability
                         ("| `owner_id` | `INTEGER` | NOT NULL | IDENTITY | PK |", "| `owner_id` | `INTEGER` | NOT NULL | - | PK |")):
            with self.subTest(old):
                self.assertTrue(self.problems("logical-schema.md", old, new))

    def test_altered_keys_and_constraints_detected(self):
        for old, new in (("**Primary key:** `pk_owners` (`owner_id`)", "**Primary key:** `pk_owners` (`email`)"),
                         ("| RESTRICT | CASCADE | owner_pet |", "| CASCADE | CASCADE | owner_pet |"),         # ON DELETE
                         ("species IN ('dog','cat','other')", "species IN ('dog','cat')"),                     # CHECK
                         ("| `uq_owners_email` | `email` |", "| `uq_owners_email` | `phone` |"),               # UNIQUE
                         ("| `ix_pets_owner` | `owner_id` | no |", "| `ix_pets_owner` | `owner_id` | yes |")):   # index
            with self.subTest(old):
                self.assertTrue(self.problems("logical-schema.md", old, new))

    def test_removed_and_invented_rows_detected(self):
        line = next(ln for ln in self.docs0["logical-schema.md"].splitlines() if ln.startswith("| `fk_pets_owners`"))
        self.assertTrue(self.problems("logical-schema.md", line + "\n", ""))
        extra = "| `ghost` | `INTEGER` | NULL | - | - | - |\n"
        old = "| `phone` | `VARCHAR(30)` | NULL | - | - | - |\n"
        self.assertIn(old, self.docs0["logical-schema.md"])
        self.assertTrue(self.problems("logical-schema.md", old, old + extra))

    def test_relationship_and_rule_changes_detected(self):
        self.assertTrue(self.problems("logical-schema.md", "| `owner_pet` | one-to-many | foreign key `fk_pets_owners` on table `pets`",
                                      "| `owner_pet` | one-to-many | foreign key `fk_pets_owners` on table `owners`"))
        self.assertTrue(self.problems("logical-schema.md", "| `status_values` | schema |", "| `status_values` | application |"))
        self.assertTrue(self.problems("conceptual-model.md", "### Pet (`pet`", "### Pet (`animal`"))

    def test_diagram_changes_detected(self):
        self.assertTrue(self.problems("logical-schema.md", "        VARCHAR(30) phone \"nullable\"\n", ""))
        self.assertTrue(self.problems("logical-schema.md", "    owners ||..o{ pets : \"fk_pets_owners\"\n", ""))
        self.assertTrue(self.problems("conceptual-model.md", "```mermaid\nerDiagram", "```mermaid\ngraph TD"))

    def test_semantically_altered_normalization_wording_detected(self):
        for old, new in (("| `owners` | (owner_id); (email) | BCNF | OK |", "| `owners` | (owner_id); (email) | 2NF | OK |"),
                         ("| `owners` | (owner_id); (email) |", "| `owners` | (owner_id) |"),            # candidate key dropped
                         ("- **2NF:** satisfied", "- **2NF:** VIOLATED"),
                         ("- **BCNF:** satisfied", "- **BCNF:** fine"),
                         ("| BCNF | OK |", "| BCNF | declared denormalization |"),
                         ("- `owner_id` -> `email`, `full_name`, `phone`", "- `owner_id` -> `email`, `full_name`"),
                         ("**Candidate keys:** (owner_id); (email)", "**Candidate keys:** (owner_id)")):
            with self.subTest(old):
                self.assertTrue(self.problems("normalization.md", old, new))

    def test_wording_change_not_covered_by_a_specific_check_is_still_caught(self):
        probs = self.problems("normalization.md", "assumed satisfied", "proven satisfied")
        self.assertTrue(any("differs from the rendering" in p for p in probs))

    def test_assumption_changes_detected(self):
        self.assertTrue(self.problems("assumptions.md", "A pet has exactly one owner.", "A pet has two owners."))
        self.assertTrue(self.problems("assumptions.md", "**Q1:** Are pets ever co-owned?", "**Q9:** Are pets ever co-owned?"))
        self.assertTrue(self.problems("assumptions.md", "_None._", "_None._\n- invented gap", 1))

    def test_document_for_a_table_missing_from_design_detected(self):
        design = copy.deepcopy(self.design)
        design["physical"]["tables"][1]["columns"][1]["data_type"] = "VARCHAR(81)"         # design changed, docs stale
        self.assertTrue(dd.check_documents(design, self.docs0))


class ReuseRecoveryAndCompletionTests(Base):
    def test_existing_valid_design_is_reused_without_llm_calls(self):
        s = make_session(self.tmp.name)
        llm = FakeLLM()
        design.run(s, llm, self.prompts)
        self.assertEqual(llm.calls, ["design:conceptual", "design:logical"])
        s2 = load_session(self.tmp.name, s.session_id)
        s2.complete_stage("design", "revision")
        s2.data["current_stage"] = "design"                      # run Stage 4 again on a saved design
        design.run(s2, NoCallsLLM(), self.prompts)
        self.assertEqual(s2.current_stage, "revision")

    def test_recovery_after_llm_failure_in_logical_call(self):
        s = make_session(self.tmp.name)
        with self.assertRaises(LLMError):
            design.run(s, FakeLLM(logical=LLMError("boom", retryable=False)), self.prompts)
        s = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s.current_stage, "design")
        self.assertNotIn("design", s.data["completed_stages"])
        self.assertNotIn("design", s.data)
        llm = FakeLLM()
        design.run(s, llm, self.prompts)
        self.assertEqual(llm.calls, ["design:logical"])               # conceptual result was kept
        self.assertEqual(s.current_stage, "revision")

    def test_recovery_after_interruption_while_writing_documents(self):
        s = make_session(self.tmp.name)
        real, written = s.write_output, []

        def flaky(name, text):
            if len(written) == 3:
                raise KeyboardInterrupt()
            written.append(name)
            return real(name, text)
        with mock.patch.object(s, "write_output", flaky):
            with self.assertRaises(KeyboardInterrupt):
                design.run(s, FakeLLM(), self.prompts)
        s = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s.current_stage, "design")
        self.assertNotIn("design", s.data["completed_stages"])
        self.assertIn("design", s.data)                                # design itself was already saved
        design.run(s, NoCallsLLM(), self.prompts)                      # finishes with no LLM call
        self.assertEqual(s.current_stage, "revision")
        self.assertEqual(dd.check_documents(s.data["design"], self.docs(s)), [])

    def test_rerun_repairs_corrupted_document_on_disk(self):
        s = make_session(self.tmp.name)
        design.run(s, FakeLLM(), self.prompts)
        (s.output_dir / "logical-schema.md").write_text("garbage", encoding="utf-8")
        design.run(s, NoCallsLLM(), self.prompts)
        self.assertEqual(dd.check_documents(s.data["design"], self.docs(s)), [])

    def test_stage_completes_only_after_successful_validation(self):
        s = make_session(self.tmp.name)
        with mock.patch.object(design, "check_documents", return_value=["normalization.md: broken"]):
            with self.assertRaises(InvalidResponseError) as cm:
                design.run(s, FakeLLM(), self.prompts)
        self.assertIn("not consistent", str(cm.exception))
        self.assertIn("normalization.md: broken", str(cm.exception))
        s = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s.current_stage, "design")
        self.assertNotIn("design", s.data["completed_stages"])
        self.assertNotIn("design_docs", s.data)
        design.run(s, NoCallsLLM(), self.prompts)                      # validation now succeeds
        s = load_session(self.tmp.name, s.session_id)
        self.assertEqual(s.data["completed_stages"], ["intake", "interview", "dbms", "design"])
        self.assertEqual(s.current_stage, "revision")
        self.assertIn("validated_at", s.data["design_docs"])
        self.assertEqual(s.data["design_docs"]["files"], design.STAGE_OUTPUTS)

    def test_invalid_saved_design_is_not_silently_used(self):
        s = make_session(self.tmp.name)
        design.run(s, FakeLLM(), self.prompts)
        s.data["current_stage"] = "design"
        s.data["completed_stages"].remove("design")
        s.data["design"]["physical"]["tables"].pop(0)
        with self.assertRaises(InvalidResponseError) as cm:
            design.run(s, NoCallsLLM(), self.prompts)
        self.assertIn("design-draft", str(cm.exception))
        self.assertEqual(s.current_stage, "design")

    def test_stages_1_to_3_are_untouched(self):
        s = make_session(self.tmp.name)
        for name in ("intake-summary.md", "requirements.md", "dbms-choice.md"):
            s.write_output(name, f"original {name}")
        s.save()
        before = {k: copy.deepcopy(s.data[k]) for k in ("intake", "requirements", "dbms_choice", "description")
                  if k in s.data}
        design.run(s, FakeLLM(), self.prompts)
        for k, v in before.items():
            self.assertEqual(s.data[k], v, k)
        for name in ("intake-summary.md", "requirements.md", "dbms-choice.md"):
            self.assertEqual((s.output_dir / name).read_text(encoding="utf-8"), f"original {name}")
        self.assertEqual(s.data["completed_stages"][:3], ["intake", "interview", "dbms"])


class WorkflowAndCliTests(Base):
    def test_design_registered_and_next_stage_is_revision(self):
        self.assertTrue(workflow.is_implemented("design"))
        self.assertFalse(workflow.is_implemented("revision"))
        self.assertFalse(workflow.is_implemented("sql"))              # Stage 5 / 6 are not part of this delivery
        s = make_session(self.tmp.name)
        self.assertTrue(workflow.should_confirm("design", s))         # asks before starting
        workflow.run_stage("design", s, FakeLLM(), self.prompts)
        self.assertEqual(s.current_stage, "revision")
        s2 = make_session(self.tmp.name)
        s2.data["design_progress"] = {"conceptual": {}}
        self.assertFalse(workflow.should_confirm("design", s2))       # already begun: resume without asking

    def _cli(self, tmp, session, inputs, scenario="clinic"):
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"provider": "ollama", "model": "m", "base_url": "http://x"}), encoding="utf-8")
        answers, out = iter(inputs), []

        class Client:
            def __init__(self, config, logger=None):
                self.inner = FakeLLM(scenario)

            def complete_json(self, *a, **k):
                return self.inner.complete_json(*a, **k)
        with mock.patch.object(cli, "LLMClient", Client):
            code = cli.main(["--config", str(cfg), "--sessions-dir", tmp, "resume", session.session_id],
                            input_fn=lambda: next(answers), out=out.append)
        return code, out

    def test_cli_resume_runs_stage4_and_advances_to_stage5(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario), tempfile.TemporaryDirectory() as tmp:
                s = make_session(tmp, SCENARIOS[scenario][1])
                code, out = self._cli(tmp, s, ["y"], scenario)
                self.assertEqual(code, 0, out)
                s2 = load_session(tmp, s.session_id)
                self.assertEqual(s2.current_stage, "revision")
                self.assertEqual(s2.data["completed_stages"][-1], "design")
                text = "\n".join(out)
                for f in dd.DOC_FILES:
                    self.assertIn(f, text)
                self.assertIn("Stages 5 and 6 are implemented but run through dedicated commands", text)

    def test_cli_resume_after_completion_does_not_rerun_stage4(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp, "mysql_mariadb")
            self.assertEqual(self._cli(tmp, s, ["y"], "university")[0], 0)
            before = (load_session(tmp, s.session_id).output_dir / "design.json").read_text(encoding="utf-8")
            code, out = self._cli(tmp, s, [], "university")            # no input needed, no LLM call possible
            self.assertEqual(code, 0)
            self.assertTrue(any("dedicated commands" in ln for ln in out))
            self.assertEqual((load_session(tmp, s.session_id).output_dir / "design.json").read_text(encoding="utf-8"), before)

    def test_cli_summary_lists_files_and_warnings(self):
        s = self.run_stage("university", logical=fresh2(justified=False)[1])
        out = []
        cli.print_design_summary(s, out.append)
        text = "\n".join(out)
        for f in dd.DOC_FILES:
            self.assertIn(f, text)
        self.assertIn("WARNING:", text)

    def test_design_draft_command_still_only_runs_4a(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = make_session(tmp)
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({"provider": "ollama", "model": "m", "base_url": "http://x"}), encoding="utf-8")

            class Client:
                def __init__(self, config, logger=None):
                    self.inner = FakeLLM()

                def complete_json(self, *a, **k):
                    return self.inner.complete_json(*a, **k)
            out = []
            with mock.patch.object(cli, "LLMClient", Client):
                self.assertEqual(cli.main(["--config", str(cfg), "--sessions-dir", tmp, "design-draft", s.session_id],
                                          out=out.append), 0)
            s2 = load_session(tmp, s.session_id)
            self.assertEqual(s2.current_stage, "design")
            self.assertFalse((s2.output_dir / "normalization.md").exists())


if __name__ == "__main__":
    unittest.main()
