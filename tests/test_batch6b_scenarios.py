"""Batch 6B - two independent scenarios through the REAL Stage 1-5 code with deterministic mocked LLM answers.

What this proves: the workflow's own parsing, validation, state handling, traceability, normalization analysis and
approval logic behave correctly on two different small domains. It does NOT prove that a real LLM would understand an
arbitrary unfamiliar scenario or produce these designs - the mocked answers are written by hand."""
import copy
import json
import tempfile
import unittest
from unittest import mock

from dba_agent import approval, cli, design_docs as dd, design_model as dm, normalization_analysis as na, revision
from dba_agent.config import config_from_dict
from dba_agent.errors import InvalidResponseError
from dba_agent.prompts import PromptLoader
from dba_agent.stages import design as design_stage, dbms as dbms_stage, interview as iv
from dba_agent.state import load_session
from tests import scenarios_6b as sc
from tests.pipeline_6b import PROMPTS, ScriptedLLM, make_script, run_pipeline, scripted_ui
from tests.scenarios_6b import _c, _fd
from tests.test_stage5a_revision import ScriptedLLM as RevLLM, response

NAMES = ("booking", "ordering")


def tbl(d, name):
    return next(t for t in d["physical"]["tables"] if t["name"] == name)


def keys_of(t):
    ks = [tuple(t["primary_key"])] + [tuple(u["columns"]) for u in t["unique_constraints"]]
    return ks + [tuple(i["columns"]) for i in t["indexes"] if i["unique"] and not i.get("where")]


class ScenarioBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)

    def pipeline(self, name, **kw):
        return run_pipeline(self.tmp.name + f"/{name}{len(kw)}", name, **kw)


# ============================================================== Stages 1 and 2
class Stage12Tests(ScenarioBase):
    def test_intake_keeps_entities_requirements_and_ambiguities(self):
        for n in NAMES:
            with self.subTest(n):
                s, llm, s12 = self.pipeline(n)
                it = s.data["intake"]
                self.assertEqual([e["name"] for e in it["entities"]], [e["name"] for e in s12["intake"]["entities"]])
                self.assertEqual([r["description"] for r in it["requirements"]], [r["description"] for r in s12["intake"]["requirements"]])
                md = (s.output_dir / "intake-summary.md").read_text(encoding="utf-8")
                for x in it["requirements"] + it["ambiguities"]:
                    self.assertIn(x["description"], md)

    def test_every_category_is_asked_and_every_user_answer_is_preserved(self):
        for n in NAMES:
            with self.subTest(n):
                s, llm, s12 = self.pipeline(n)
                rounds = s.data["interview"]["rounds"]
                self.assertEqual({q["category"] for q in rounds[0]["questions"]}, set(iv.CATEGORY_IDS))
                stored = [q["answer"] for r in rounds for q in r["questions"]]
                given = [a for a in s12["answers_r1"] + s12["answers_r2"] if a != "(skipped)"]
                for a in given:
                    self.assertIn(a, stored)
                md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
                for a in given:
                    self.assertIn(a, md)            # the transcript in requirements.md keeps the user's words

    def test_skipped_category_is_not_silently_dropped_booking(self):
        s, llm, s12 = self.pipeline("booking")
        rounds = s.data["interview"]["rounds"]
        self.assertEqual(len(rounds), 3)                                  # coverage guard forced a third round
        self.assertTrue(rounds[1]["review"].get("overridden"))
        third = rounds[2]["questions"][0]
        self.assertEqual(third["category"], "security_roles_privacy")
        self.assertIn("Skipped earlier", third["question"])
        self.assertEqual(third["answer"], "security: admins only")
        self.assertEqual(s.data["interview"]["unresolved"]["uncovered_categories"], [])

    def test_vague_answer_is_an_open_question_not_a_business_rule(self):
        for n, needle in (("booking", "several services"), ("ordering", "several payments")):
            with self.subTest(n):
                s, llm, s12 = self.pipeline(n)
                open_qs = iv.open_questions(s.data["interview"]["unresolved"])
                self.assertTrue(any(needle in q["question"] for q in open_qs), open_qs)
                req_md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
                self.assertIn("## Unresolved Requirements", req_md)
                self.assertTrue(any(needle in g for g in s.data["design"]["requirement_gaps"]))
                d = s.data["design"]
                self.assertTrue(d["open_questions"] and any(needle in q["text"] for q in d["open_questions"]))
                self.assertTrue(any(needle in q["text"] for q in d["open_questions"]) or d["assumptions"])

    def test_design_without_recording_the_open_question_is_rejected(self):
        for n in NAMES:
            with self.subTest(n):
                c = sc.SCENARIOS[n]["conceptual"]()
                c["assumptions"], c["open_questions"] = [], []
                with self.assertRaises(InvalidResponseError):
                    run_pipeline(self.tmp.name + f"/neg{n}", n, conceptual=c)

    def test_vague_phrases_are_missing_information_but_real_content_is_an_answer(self):     # regression (Batch 6B)
        for text in ("I don't know yet", "not sure", "No idea at the moment.", "unknown", "TBD", "I can't say", "I do not know."):
            self.assertFalse(iv.is_answered(text), text)
            self.assertEqual(iv.answer_kind(text), "missing", text)
        for text in ("I don't know exactly, maybe 300", "about 300", "Not sure - probably yes", "no overlaps at all"):
            self.assertTrue(iv.is_answered(text), text)
        self.assertEqual(iv.answer_kind("n/a - single user"), "not_applicable")

    def test_intake_statements_are_carried_verbatim_to_later_stages(self):        # regression (Batch 6B)
        for n in NAMES:
            with self.subTest(n):
                s, llm, s12 = self.pipeline(n)
                reqs = s.data["requirements"]
                stated = [r["description"] for r in s12["intake"]["requirements"]]
                for text in stated:
                    self.assertTrue(any(text in x for x in reqs["intake_requirements"]))
                self.assertEqual(reqs["intake_ambiguities"], [a["description"] for a in s12["intake"]["ambiguities"]])
                md = (s.output_dir / "requirements.md").read_text(encoding="utf-8")
                for stage in ("dbms:selection", "design:conceptual", "design:logical"):
                    prompt = llm.users(stage)[0]
                    for text in stated + reqs["intake_ambiguities"]:
                        self.assertIn(text, prompt, f"{stage} prompt lost: {text}")
                for text in stated:
                    self.assertIn(text, md)

    def test_interrupted_interview_resumes_without_losing_answers_or_repeating_calls(self):
        script, s12 = make_script("ordering")
        llm = ScriptedLLM(script)
        s = run_pipeline(self.tmp.name + "/r", "ordering", until="intake")[0]
        from dba_agent import workflow
        with self.assertRaises(EOFError):                                   # the user pauses after 3 answers
            workflow.run_stage("interview", s, llm, self.prompts, scripted_ui(s12["answers_r1"][:3]))
        saved = load_session(self.tmp.name + "/r", s.session_id)
        first = saved.data["interview"]["rounds"][0]["questions"]
        self.assertEqual([q["answer"] for q in first[:3]], s12["answers_r1"][:3])
        self.assertTrue(all(q["answer"] is None for q in first[3:]))
        llm2 = ScriptedLLM(dict(script, **{"interview:questions": []}))     # a repeated question call would raise
        rest = s12["answers_r1"][3:] + s12["answers_r2"]
        workflow.run_stage("interview", saved, llm2, self.prompts, scripted_ui(rest))
        self.assertEqual(saved.current_stage, "dbms")
        self.assertEqual([q["answer"] for q in saved.data["interview"]["rounds"][0]["questions"]], s12["answers_r1"])


# ============================================================== Stage 3
TYPE_MAP = {"mysql_mariadb": {"TIMESTAMP": "DATETIME", "INTEGER": "INT"},
            "sql_server": {"TIMESTAMP": "DATETIME2", "INTEGER": "INT"},
            "sqlite": {"VARCHAR(120)": "TEXT", "VARCHAR(200)": "TEXT", "VARCHAR(40)": "TEXT", "VARCHAR(150)": "TEXT",
                       "VARCHAR(10)": "TEXT", "TIMESTAMP": "TEXT", "NUMERIC(10,2)": "NUMERIC"},
            "postgresql": {}}


def ordering_for(dbms):
    lg = sc.ordering_logical()
    for t in lg["tables"]:
        for c in t["columns"]:
            c["data_type"] = TYPE_MAP[dbms].get(c["data_type"], c["data_type"])
        if dbms == "sql_server":
            for f in t["foreign_keys"]:
                f["on_delete"] = "NO ACTION" if f["on_delete"] == "RESTRICT" else f["on_delete"]
                f["on_update"] = "NO ACTION" if f["on_update"] == "RESTRICT" else f["on_update"]
    return lg


class Stage3Tests(ScenarioBase):
    def test_all_four_dbms_are_supported_and_carried_into_design(self):
        self.assertEqual(sorted(dbms_stage.DBMS_IDS), ["mysql_mariadb", "postgresql", "sql_server", "sqlite"])
        for dbms in dbms_stage.DBMS_IDS:
            with self.subTest(dbms):
                s, llm, _ = run_pipeline(self.tmp.name + "/" + dbms, "ordering", dbms=dbms, logical=ordering_for(dbms))
                ch = s.data["dbms_choice"]
                self.assertEqual((ch["selected_id"], ch["selected"]), (dbms, dbms_stage.DBMS_NAMES[dbms]))
                self.assertTrue(ch["justification"] and ch["rejected"] and len(ch["rejected"]) == 3)
                self.assertEqual(s.data["design"]["dbms"], {"id": dbms, "name": dbms_stage.DBMS_NAMES[dbms]})
                self.assertIn(f"## Selected DBMS: {dbms_stage.DBMS_NAMES[dbms]}", (s.output_dir / "dbms-choice.md").read_text(encoding="utf-8"))
                self.assertIn(dbms_stage.DBMS_NAMES[dbms], (s.output_dir / "logical-schema.md").read_text(encoding="utf-8"))
                self.assertIn(f'"selected_id": "{dbms}"', llm.users("design:logical")[0])

    def test_aliases_resolve_but_unknown_dbms_is_rejected_not_substituted(self):
        for alias, want in (("Postgres", "postgresql"), ("MariaDB", "mysql_mariadb"), ("MSSQL", "sql_server"), ("SQLite3", "sqlite")):
            self.assertEqual(dbms_stage.validate_choice(dict(sc.choice(want), selected=alias))["selected_id"], want)
        with self.assertRaises(InvalidResponseError):
            dbms_stage.validate_choice(dict(sc.choice("postgresql"), selected="Oracle"))
        bad = sc.choice("postgresql")
        bad["rejected"] = [r for r in bad["rejected"] if r["dbms"] != "sqlite"]
        with self.assertRaises(InvalidResponseError):                       # every other option must be explained
            dbms_stage.validate_choice(bad)

    def test_design_in_another_dialect_is_rejected_not_silently_converted(self):
        with self.assertRaises(InvalidResponseError):
            run_pipeline(self.tmp.name + "/x", "ordering", dbms="sql_server", logical=sc.ordering_logical())   # RESTRICT is not SQL Server

    def test_explicit_user_override_is_preserved_and_carried_forward(self):
        s, _, _ = self.pipeline("ordering")
        original = copy.deepcopy(s.data["dbms_choice"])

        def to_mysql(d):
            d["dbms"] = {"id": "mysql_mariadb", "name": "MySQL/MariaDB"}
            for t in d["physical"]["tables"]:
                for c in t["columns"]:
                    c["data_type"] = TYPE_MAP["mysql_mariadb"].get(c["data_type"], c["data_type"])
            d["assumptions"].append({"id": "R_MY1", "text": "Timestamps are DATETIME.", "rationale": "MySQL dialect.", "related_to": []})
        rec = revision.revise(s, RevLLM(s, lambda cur: response(cur, to_mysql, ["switch_dbms"], dbms={"id": "mysql_mariadb", "name": "x"})),
                              self.prompts, "Please use MySQL instead")
        ch = s.data["dbms_choice"]
        self.assertEqual((ch["selected_id"], s.data["design"]["dbms"]["id"]), ("mysql_mariadb", "mysql_mariadb"))
        self.assertEqual(ch["overrides"][0]["request"], "Please use MySQL instead")
        self.assertEqual(ch["original"]["selected_id"], original["selected_id"])      # the agent's own analysis is not falsified
        self.assertIn("Please use MySQL instead", (s.output_dir / "dbms-choice.md").read_text(encoding="utf-8"))
        self.assertTrue(approval.check_approvable(s)["problems"] == [])                # design/choice/documents agree
        self.assertEqual(rec["change_types"], ["switch_dbms"])


# ============================================================== Stage 4
def _fk_targets_are_keys(test, d):
    tm = {t["name"]: t for t in d["physical"]["tables"]}
    for t in tm.values():
        test.assertTrue(t["primary_key"], t["name"])
        cols = {c["name"] for c in t["columns"]}
        test.assertTrue(set(t["primary_key"]) <= cols)
        for fk in t["foreign_keys"]:
            ref = tm[fk["ref_table"]]
            test.assertTrue(set(fk["columns"]) <= cols and len(fk["columns"]) == len(fk["ref_columns"]))
            test.assertIn(tuple(fk["ref_columns"]), keys_of(ref), f"{t['name']}.{fk['name']} must reference a key of {ref['name']}")


class Stage4StructureTests(ScenarioBase):
    def test_both_designs_are_valid_documented_and_consistent(self):
        for n in NAMES:
            with self.subTest(n):
                s, _, _ = self.pipeline(n)
                d = s.data["design"]
                dm.link_relationships(dm.validate_design(copy.deepcopy(d)))
                docs = {f: (s.output_dir / f).read_text(encoding="utf-8") for f in dd.DOC_FILES}
                self.assertEqual(dd.check_documents(d, docs), [])
                self.assertEqual(json.loads((s.output_dir / "design.json").read_text(encoding="utf-8")), d)
                self.assertEqual(s.current_stage, "revision")
                _fk_targets_are_keys(self, d)

    def test_every_entity_has_a_table_and_every_many_to_many_a_correct_junction(self):
        for n in NAMES:
            with self.subTest(n):
                d = self.pipeline(n)[0].data["design"]
                sources = [t["source"] for t in d["physical"]["tables"]]
                for e in d["conceptual"]["entities"]:
                    self.assertIn({"type": "entity", "id": e["id"]}, sources)
                for r in d["conceptual"]["relationships"]:
                    mn = all(p["cardinality"]["max"] == "N" for p in r["participants"])
                    junction = [t for t in d["physical"]["tables"] if t["source"] == {"type": "relationship", "id": r["id"]}]
                    self.assertEqual(len(junction), 1 if mn else 0, r["id"])
                    if mn:
                        j = junction[0]
                        self.assertEqual(len(j["foreign_keys"]), 2)
                        self.assertTrue({c for fk in j["foreign_keys"] for c in fk["columns"]} <= set(j["primary_key"]))
                        for at in r["attributes"]:
                            self.assertIn(at["name"], [c["name"] for c in j["columns"]])

    def test_every_business_rule_is_enforced_or_explicitly_documented(self):
        for n in NAMES:
            with self.subTest(n):
                d = self.pipeline(n)[0].data["design"]
                names = {x for t in d["physical"]["tables"] for x in dm._constraint_names(t)}
                for r in d["business_rules"]:
                    e = r["enforcement"]
                    self.assertIn(e["mechanism"], dm.ENFORCEMENTS, r["id"])
                    if e["mechanism"] == "schema":
                        self.assertTrue(e["implemented_by"], r["id"])
                    else:
                        self.assertTrue(e["note"].strip(), f"{r['id']}: a rule outside the schema must say how it is enforced")

    def test_booking_requirements_trace_to_structure(self):
        s, _, s12 = self.pipeline("booking")
        d = s.data["design"]
        rules = {r["id"]: r for r in d["business_rules"]}
        appt = tbl(d, "appointments")
        # customers book services with employees: three mandatory FKs on the appointment
        self.assertEqual({fk["ref_table"] for fk in appt["foreign_keys"]}, {"customers", "employees", "services"})
        self.assertTrue(all(not next(c for c in appt["columns"] if c["name"] == fk["columns"][0])["nullable"] for fk in appt["foreign_keys"]))
        # employees provide selected services: many-to-many junction with composite key
        self.assertEqual(sorted(tbl(d, "employee_services")["primary_key"]), ["employee_id", "service_id"])
        # statuses
        ck = next(c for c in appt["check_constraints"] if c["name"] == "ck_appt_status")
        for st in ("booked", "confirmed", "completed", "cancelled", "no_show"):
            self.assertIn(f"'{st}'", ck["expression"])
        # conflicting active bookings: a structured conditional unique index over (employee, start) for ACTIVE statuses only
        ix = next(i for i in appt["indexes"] if i["name"] == "ux_appt_employee_start_active")
        self.assertTrue(ix["unique"] and ix["columns"] == ["employee_id", "starts_at"])
        self.assertEqual(ix["where"][0]["column"], "status")
        self.assertEqual(set(ix["where"][0]["value"]), {"booked", "confirmed"})       # cancelled/completed do not block a slot
        self.assertEqual(rules["no_double_booking_same_start"]["enforcement"]["mechanism"], "schema")
        # ...but full time-range overlap is NOT structurally enforceable and must not be claimed as schema-enforced
        self.assertNotEqual(rules["no_overlapping_active_bookings"]["enforcement"]["mechanism"], "schema")
        self.assertIn("overlap", rules["no_overlapping_active_bookings"]["enforcement"]["note"].lower())
        # history: append-only status changes, RESTRICT FK (no cascading delete of history)
        sc_t = tbl(d, "status_changes")
        self.assertEqual(sc_t["triggers"][0]["kind"], "append_only")
        self.assertEqual({fk["on_delete"] for fk in sc_t["foreign_keys"]}, {"RESTRICT"})
        self.assertEqual({fk["on_delete"] for fk in appt["foreign_keys"]}, {"RESTRICT"})
        self.assertEqual(rules["history_preserved"]["enforcement"]["implemented_by"], ["tg_status_changes_append_only"])
        # the open question about several services is recorded, and NOT turned into a junction table
        self.assertFalse(any(t["name"] in ("appointment_services",) for t in d["physical"]["tables"]))
        self.assertTrue(any("several services" in q["text"] for q in d["open_questions"]))

    def test_ordering_requirements_trace_to_structure(self):
        s, _, _ = self.pipeline("ordering")
        d = s.data["design"]
        items = tbl(d, "order_items")
        self.assertEqual(sorted(items["primary_key"]), ["order_id", "product_id"])         # many products per order, many orders per product
        price = next(c for c in items["columns"] if c["name"] == "unit_price")
        self.assertFalse(price["nullable"])                                                # purchase-time price is mandatory
        fds = [f for f in d["physical"]["functional_dependencies"] if f["table"] == "order_items"]
        self.assertEqual([f["determinant"] for f in fds], [["order_id", "product_id"]])    # unit_price depends on the WHOLE key, not on product
        self.assertNotIn("unit_price", [c for f in d["physical"]["functional_dependencies"] if f["table"] == "products" for c in f["dependent"]])
        hist = tbl(d, "product_prices")                                                    # price changes over time are stored separately
        self.assertIn(("product_id", "valid_from"), keys_of(hist))
        self.assertEqual({fk["on_delete"] for fk in items["foreign_keys"] if fk["ref_table"] == "products"}, {"RESTRICT"})   # history stays valid
        self.assertEqual(tbl(d, "payments")["foreign_keys"][0]["ref_table"], "sales_orders")
        ck = " ".join(c["expression"] for c in tbl(d, "sales_orders")["check_constraints"])
        for st in ("pending", "paid", "shipped", "cancelled"):
            self.assertIn(f"'{st}'", ck)
        self.assertIn("quantity >= 1", items["check_constraints"][0]["expression"])
        self.assertTrue(any("several payments" in q["text"] for q in d["open_questions"]))
        self.assertEqual(tbl(d, "payments")["unique_constraints"], [])                     # no invented one-payment-per-order rule

    def test_no_stated_requirement_disappears_between_stages(self):
        for n in NAMES:
            with self.subTest(n):
                s, _, s12 = self.pipeline(n)
                d = s.data["design"]
                rule_text = " ".join(r["description"].lower() for r in d["business_rules"]) + " " + " ".join(
                    x["text"].lower() for x in d["assumptions"] + d["open_questions"])
                finding_text = " ".join(f.lower() for c in s.data["requirements"]["categories"] for f in c["findings"])
                keywords = {"booking": [("conflict", "overlap"), ("history", "history"), ("status", "status"), ("service", "provid")],
                            "ordering": [("price", "price"), ("status", "status"), ("histor", "histor"), ("payment", "payment")]}[n]
                for _, kw in keywords:
                    self.assertIn(kw, rule_text + " " + finding_text, kw)
                entities = {e["id"] for e in d["conceptual"]["entities"]}
                for e in s12["intake"]["entities"]:
                    self.assertTrue(any(e["name"].lower() in x or x in e["name"].lower() for x in entities) or
                                    any(e["name"].lower() in t["name"] for t in d["physical"]["tables"]) or
                                    e["name"] == "Order", e["name"])


class Stage4NormalizationTests(ScenarioBase):
    def test_both_designs_are_proven_3nf_by_the_analyzer_not_by_report_text(self):
        for n in NAMES:
            with self.subTest(n):
                d = self.pipeline(n)[0].data["design"]
                result = na.analyze_design(d)
                self.assertEqual(set(result), {t["name"] for t in d["physical"]["tables"]})
                for name, a in result.items():
                    self.assertEqual(a["nf3_status"], na.NF3_PROVEN, name)
                    self.assertTrue(a["formal_3nf"] and a["keys_complete"], name)
                    self.assertFalse(a["unjustified"] or a["findings"] and any(f["form"] in ("2NF", "3NF") for f in a["findings"]), name)
                    self.assertEqual(a["non_atomic_columns"], [], name)                      # 1NF: no multi-valued column types
                ready = approval.normalization_readiness(d)
                self.assertEqual(len(ready["proven_3nf"]), len(result))
                self.assertEqual((ready["known_violations"], ready["unverified_3nf"]), ([], []))

    def test_candidate_keys_and_declared_dependencies_are_consistent_with_the_schema(self):
        for n in NAMES:
            with self.subTest(n):
                d = self.pipeline(n)[0].data["design"]
                result = na.analyze_design(d)
                tm = {t["name"]: t for t in d["physical"]["tables"]}
                for f in d["physical"]["functional_dependencies"]:
                    cols = {c["name"] for c in tm[f["table"]]["columns"]}
                    self.assertTrue(set(f["determinant"]) | set(f["dependent"]) <= cols, f)
                for name, t in tm.items():
                    self.assertIn(list(t["primary_key"]), [list(k) for k in result[name]["candidate_keys"]], name)
        a = na.analyze_design(self.pipeline("booking")[0].data["design"])["customers"]
        self.assertIn(["email"], [list(k) for k in a["candidate_keys"]])                     # the unique e-mail is a second candidate key

    def test_known_2nf_violation_in_order_items_is_detected_and_cannot_be_approved(self):
        lg = sc.ordering_logical()
        items = next(t for t in lg["tables"] if t["name"] == "order_items")
        items["columns"].append(_c("product_name", "VARCHAR(150)"))                           # product_id -> product_name: partial dependency
        for f in lg["functional_dependencies"]:
            if f["table"] == "order_items":
                f["dependent"].append("product_name")
        lg["functional_dependencies"].append(_fd("order_items", ["product_id"], ["product_name"]))
        self.assert_unapprovable(lg, "order_items", "2NF")

    def test_known_3nf_transitive_violation_in_appointments_is_detected_and_cannot_be_approved(self):
        lg = sc.booking_logical()
        appt = next(t for t in lg["tables"] if t["name"] == "appointments")
        appt["columns"].append(_c("customer_name", "TEXT"))                                   # customer_id -> customer_name via appointment
        for f in lg["functional_dependencies"]:
            if f["table"] == "appointments" and f["determinant"] == ["appointment_id"]:
                f["dependent"].append("customer_name")
        lg["functional_dependencies"].append(_fd("appointments", ["customer_id"], ["customer_name"]))
        self.assert_unapprovable(lg, "appointments", "3NF", scenario="booking")

    def assert_unapprovable(self, logical, table, form, scenario="ordering"):
        s, _, _ = run_pipeline(self.tmp.name + f"/neg-{table}", scenario, logical=logical)    # accepted by Stage 4 with a warning (max_attempts=1)
        a = na.analyze_design(s.data["design"])[table]
        self.assertEqual((a["nf3_status"], a["unjustified"], a["formal_3nf"]), (na.NF3_VIOLATED, True, False))
        self.assertIn(form, {f["form"] for f in a["findings"]})
        ready = approval.normalization_readiness(s.data["design"])
        self.assertIn(table, [i["table"] for i in ready["known_violations"]])
        self.assertNotIn(table, [i["table"] for i in ready["proven_3nf"]])
        self.assertTrue(any(table in w and "3NF" in w for w in dd.design_warnings(s.data["design"], na.analyze_design(s.data["design"]))))
        for ack in (False, True):
            with self.assertRaises(approval.ApprovalError):
                approval.approve(s, acknowledge_normalization=ack)
        self.assertNotIn("approval", s.data)
        self.assertEqual(s.current_stage, "revision")
        # the repair loop: while attempts remain the engine sends the below-3NF design back to the LLM instead of accepting it
        conceptual = dm.validate_conceptual(sc.SCENARIOS[scenario]["conceptual"]())
        with self.assertRaises(InvalidResponseError):
            design_stage.validate_logical_response(copy.deepcopy(logical), conceptual, s.data["design"]["dbms"]["id"], nf_gate=lambda: True)

    def test_justified_denormalization_is_an_exception_never_proven_3nf(self):
        lg = sc.ordering_logical()
        items = next(t for t in lg["tables"] if t["name"] == "order_items")
        items["columns"].append(_c("product_name", "VARCHAR(150)"))
        for f in lg["functional_dependencies"]:
            if f["table"] == "order_items":
                f["dependent"].append("product_name")
        lg["functional_dependencies"].append(_fd("order_items", ["product_id"], ["product_name"]))
        lg["denormalizations"] = [{"table": "order_items", "description": "product name copy",
                                   "justification": "invoices must show the name the customer saw at purchase time"}]
        s, _, _ = run_pipeline(self.tmp.name + "/exc", "ordering", logical=lg)
        ready = approval.normalization_readiness(s.data["design"])
        self.assertEqual([i["table"] for i in ready["documented_exceptions"]], ["order_items"])
        self.assertNotIn("order_items", [i["table"] for i in ready["proven_3nf"]])
        self.assertNotEqual(na.analyze_design(s.data["design"])["order_items"]["nf3_status"], na.NF3_PROVEN)
        with self.assertRaises(approval.ApprovalError):                                     # needs the explicit approval of the exception
            approval.approve(s)
        ap, _ = approval.approve(s, acknowledge_normalization=True)
        self.assertEqual(ap["normalization_review"]["denormalization_exceptions_approved"][0]["table"], "order_items")
        self.assertNotIn("order_items", ap["normalization_review"]["proven_3nf_tables"])

    def test_unknown_normalization_is_never_reported_as_proven(self):
        for n in NAMES:
            with self.subTest(n):
                with mock.patch.object(na, "MAX_KEY_SEARCH_STEPS", 0):
                    s, _, _ = self.pipeline(n)
                    result = na.analyze_design(s.data["design"])
                    self.assertTrue(all(a["nf3_status"] == na.NF3_UNVERIFIED and not a["formal_3nf"] for a in result.values()))
                    ready = approval.normalization_readiness(s.data["design"])
                    self.assertEqual((ready["proven_3nf"], ready["known_violations"]), ([], []))
                    self.assertEqual(len(ready["unverified_3nf"]), len(result))
                    with self.assertRaises(approval.ApprovalError):
                        approval.approve(s)
                    ap, _ = approval.approve(s, acknowledge_normalization=True)
                    self.assertEqual(ap["normalization_review"]["proven_3nf_tables"], [])
                    self.assertTrue(ap["normalization_review"]["unverified_3nf_acknowledged"])

    def test_non_atomic_column_is_flagged_for_1nf(self):
        lg = sc.ordering_logical()
        cust = next(t for t in lg["tables"] if t["name"] == "customers")
        cust["columns"].append(_c("phone_numbers", "JSONB", True))
        for f in lg["functional_dependencies"]:
            if f["table"] == "customers" and f["determinant"] == ["customer_id"]:
                f["dependent"].append("phone_numbers")
        s, _, _ = run_pipeline(self.tmp.name + "/1nf", "ordering", logical=lg)
        self.assertEqual(na.analyze_design(s.data["design"])["customers"]["non_atomic_columns"], ["phone_numbers"])


# ============================================================== Stage 5
def add_optional_column(entity_id, table, column):
    def mutate(d):
        e = next(e for e in d["conceptual"]["entities"] if e["id"] == entity_id)
        e["attributes"].append({"name": column, "type": "string", "required": False, "unique": False, "description": column})
        t = tbl(d, table)
        t["columns"].append(_c(column, "TEXT" if d["dbms"]["id"] == "sqlite" else "VARCHAR(40)", True))
        for f in d["physical"]["functional_dependencies"]:
            if f["table"] == table and f["determinant"] == t["primary_key"]:
                f["dependent"].append(column)
    return mutate


def drop_column(table, column, entity_id):
    def mutate(d):
        next(e for e in d["conceptual"]["entities"] if e["id"] == entity_id)["attributes"] = [
            a for e in d["conceptual"]["entities"] if e["id"] == entity_id for a in e["attributes"] if a["name"] != column]
        t = tbl(d, table)
        t["columns"] = [c for c in t["columns"] if c["name"] != column]
        for f in d["physical"]["functional_dependencies"]:
            f["dependent"] = [c for c in f["dependent"] if not (f["table"] == table and c == column)]
    return mutate


CHANGE = {"booking": ("employee", "employees", "job_title"), "ordering": ("customer", "customers", "phone")}
DROP = {"booking": ("appointments", "notes", "appointment"), "ordering": ("payments", "paid_at", "payment")}


class Stage5Tests(ScenarioBase):
    def test_ordinary_revision_updates_design_and_keeps_everything_else(self):
        for n in NAMES:
            with self.subTest(n):
                s, _, _ = self.pipeline(n)
                before = copy.deepcopy(s.data["design"])
                ent, table, col = CHANGE[n]
                rec = revision.revise(s, RevLLM(s, lambda cur: response(cur, add_optional_column(ent, table, col), ["add_attribute"])),
                                      self.prompts, f"add {col}")
                after = s.data["design"]
                self.assertIn(col, [c["name"] for c in tbl(after, table)["columns"]])
                self.assertEqual(rec["unexpected_removals"] + rec["structural_removals"] + rec["weakened_protections"], [])
                self.assertEqual(after["business_rules"], before["business_rules"])                # unrelated requirements untouched
                self.assertEqual([t["name"] for t in after["physical"]["tables"]], [t["name"] for t in before["physical"]["tables"]])
                self.assertEqual(after["open_questions"], before["open_questions"])
                self.assertEqual(after["requirement_gaps"], before["requirement_gaps"])
                for t in before["physical"]["tables"]:
                    if t["name"] != table:
                        self.assertEqual(tbl(after, t["name"]), t)
                self.assertTrue(all(a["nf3_status"] == na.NF3_PROVEN for a in na.analyze_design(after).values()))
                self.assertEqual(len(s.data["revisions"]), 1)
                self.assertIn(col, (s.output_dir / "logical-schema.md").read_text(encoding="utf-8"))
                self.assertEqual(approval.check_approvable(s)["problems"], [])

    def test_unexpected_structural_deletion_is_detected_and_needs_confirmation(self):
        for n in NAMES:
            with self.subTest(n):
                s, _, _ = self.pipeline(n)
                before = copy.deepcopy(s.data["design"])
                table, col, ent = DROP[n]
                ask = lambda: RevLLM(s, lambda cur: response(cur, drop_column(table, col, ent), ["add_attribute"]))
                p = revision.propose(s, ask(), self.prompts, "add something harmless")
                self.assertTrue(any(col in x for x in p.record["unexpected_removals"]), p.record["unexpected_removals"])
                self.assertIn("CHECK CAREFULLY", p.preview)
                with self.assertRaises(revision.RevisionRejected):
                    revision.accept(p)
                with self.assertRaises(revision.RevisionRejected):                                  # unattended / --yes style
                    revision.revise(s, ask(), self.prompts, "add something harmless")
                self.assertEqual(s.data["design"], before)
                self.assertEqual(s.data.get("revisions") or [], [])
                rec = revision.revise(s, ask(), self.prompts, "I do want it removed", allow_suspicious=True)       # explicit confirmation
                self.assertNotIn(col, [c["name"] for c in tbl(s.data["design"], table)["columns"]])
                self.assertTrue(rec["unexpected_removals"])

    def test_valid_designs_are_approved_and_state_is_consistent(self):
        for n in NAMES:
            with self.subTest(n):
                s, _, _ = self.pipeline(n)
                res = approval.check_approvable(s)
                self.assertEqual((res["problems"], res["acknowledgements_required"], res["blocked_normalization"]), ([], [], []))
                ap, new = approval.approve(s)
                self.assertTrue(new)
                self.assertEqual(s.current_stage, "sql")
                self.assertEqual(ap["design_fingerprint"], approval.design_fingerprint(s.data["design"]))
                self.assertEqual(approval.integrity_report(s)["state"], approval.INTACT)
                self.assertEqual(json.loads((s.output_dir / "design.json").read_text(encoding="utf-8")), s.data["design"])
                self.assertEqual(ap["normalization_review"]["proven_3nf_tables"], [t["name"] for t in s.data["design"]["physical"]["tables"]])
                again, new2 = approval.approve(s)
                self.assertEqual((again, new2), (ap, False))                                          # idempotent

    def test_resumed_session_keeps_design_approval_and_documents(self):
        for n in NAMES:
            with self.subTest(n):
                s, _, _ = self.pipeline(n)
                approval.approve(s)
                saved = load_session(self.tmp.name + f"/{n}0", s.session_id)
                self.assertEqual(saved.data["design"], s.data["design"])
                self.assertEqual(saved.data["approval"], s.data["approval"])
                self.assertEqual(saved.current_stage, "sql")
                self.assertEqual(approval.integrity_report(saved)["state"], approval.INTACT)
                self.assertEqual(approval.verify_approved_design(saved)[0]["status"], "approved")
                out = []
                with mock.patch.object(cli, "load_config", lambda p=None: config_from_dict(
                        {"provider": "ollama", "model": "m", "sessions_dir": self.tmp.name + f"/{n}0"}, env={})):
                    code = cli.main(["--sessions-dir", self.tmp.name + f"/{n}0", "resume", s.session_id], input_fn=lambda: "", out=out.append)
                self.assertEqual(code, 0)
                self.assertTrue(any("design is approved" in x for x in out), out[-3:])
                self.assertEqual(load_session(self.tmp.name + f"/{n}0", s.session_id).data["approval"], s.data["approval"])

    def test_resume_after_interrupted_stage4_reuses_the_saved_conceptual_result(self):
        script, s12 = make_script("ordering")
        s = run_pipeline(self.tmp.name + "/p4", "ordering", until="dbms")[0]
        boom = InvalidResponseError("simulated failure in the logical step", retryable=False)
        llm = ScriptedLLM({"design:conceptual": script["design:conceptual"], "design:logical": [boom]})
        with self.assertRaises(InvalidResponseError):
            design_stage.run(s, llm, self.prompts)
        saved = load_session(self.tmp.name + "/p4", s.session_id)
        self.assertIn("conceptual", saved.data["design_progress"])
        llm2 = ScriptedLLM({"design:conceptual": [], "design:logical": script["design:logical"]})       # a repeated conceptual call would raise
        design_stage.run(saved, llm2, self.prompts)
        self.assertEqual(saved.current_stage, "revision")

    def test_revision_after_approval_invalidates_it_and_the_known_violation_stays_blocked(self):
        s, _, _ = self.pipeline("ordering")
        approval.approve(s)
        self.assertEqual(s.current_stage, "sql")
        rec = revision.revise(s, RevLLM(s, lambda cur: response(cur, add_optional_column("customer", "customers", "phone"), ["add_attribute"])),
                              self.prompts, "add phone")
        self.assertTrue(rec.get("approval_invalidated"))
        self.assertNotEqual(approval.integrity_report(s)["state"], approval.INTACT)


if __name__ == "__main__":
    unittest.main()
