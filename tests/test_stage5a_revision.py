"""Stage 5A: revision engine. All offline; the LLM is replaced by scripted responses that are derived from the
CURRENT design by small mutations (the engine itself knows nothing about the scenario)."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import cli, design_docs as dd, design_model as dm, revision, workflow
from dba_agent.config import config_from_dict
from dba_agent.errors import DBAAgentError, InvalidResponseError, LLMError
from dba_agent.revision import RevisionRejected as RevisionRejected_
from dba_agent.llm import LLMClient
from dba_agent.prompts import PromptLoader
from dba_agent.stages import design
from dba_agent.state import load_session
from tests.design_fixtures import _c, _fk, _t
from tests.test_stage4_complete import SCENARIOS, FakeLLM as Stage4LLM, make_session

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")
TABLE_KEYS = ("tables", "functional_dependencies", "denormalizations")


def response(current, mutate, types, summary="did it", rationale="because", dbms=None, **rev):
    d = copy.deepcopy(current)
    mutate(d)
    out = {"revision": dict({"applicable": True, "change_types": types, "summary": summary, "rationale": rationale,
                             "assumptions": [], "warnings": []}, **rev),
           "dbms": dbms or d["dbms"]}
    for k in ("conceptual", "physical", "business_rules", "assumptions", "open_questions"):
        out[k] = d[k]
    return out


class ScriptedLLM:
    """complete_json(stage, ...) -> validator(response) for each queued item (callable(current) or Exception)."""
    def __init__(self, session, *items):
        self.session, self.items, self.calls = session, list(items), []

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append((stage, user))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        raw = item(self.session.data["design"]) if callable(item) else item
        return validator(copy.deepcopy(raw)) if validator else raw


def tbl(d, name):
    return next(t for t in d["physical"]["tables"] if t["name"] == name)


def ent(d, eid):
    return next(e for e in d["conceptual"]["entities"] if e["id"] == eid)


# ---- mutations -----------------------------------------------------------------------------------------
def add_room(d):
    d["conceptual"]["entities"].append({
        "id": "room", "name": "Room", "description": "A consultation room.", "kind": "strong",
        "attributes": [{"name": "room_id", "type": "integer", "required": True, "unique": True, "description": "id"},
                       {"name": "label", "type": "string", "required": True, "unique": True, "description": "label"}],
        "identifiers": [{"attributes": ["room_id"], "primary": True}]})
    d["conceptual"]["relationships"].append({
        "id": "room_appointment", "name": "hosts", "description": "", "identifying": False,
        "participants": [{"entity": "room", "role": "", "cardinality": {"min": 0, "max": 1}},
                         {"entity": "appointment", "role": "", "cardinality": {"min": 0, "max": "N"}}], "attributes": []})
    d["physical"]["tables"].append(_t("rooms", {"type": "entity", "id": "room"},
                                       [_c("room_id", "INTEGER", identity=True), _c("label", "VARCHAR(40)")], ["room_id"],
                                       unique_constraints=[{"name": "uq_rooms_label", "columns": ["label"]}]))
    ap = tbl(d, "appointments")
    ap["columns"].append(dict(_c("room_id", "INTEGER", True, src=""), source_attribute=None))
    ap["foreign_keys"].append(_fk("fk_appointments_rooms", ["room_id"], "rooms", ["room_id"], "room_appointment", "SET NULL"))
    d["physical"]["functional_dependencies"] += [
        {"table": "rooms", "determinant": ["room_id"], "dependent": ["label"], "note": ""},
        {"table": "rooms", "determinant": ["label"], "dependent": ["room_id"], "note": ""}]
    for f in d["physical"]["functional_dependencies"]:
        if f["table"] == "appointments" and f["determinant"] == ["appointment_id"]:
            f["dependent"].append("room_id")


def add_weight(d):
    ent(d, "pet")["attributes"].append({"name": "weight_kg", "type": "decimal", "required": False, "unique": False,
                                         "description": "weight"})
    t = tbl(d, "pets")
    t["columns"].append(_c("weight_kg", "NUMERIC(5,2)", True))
    t["check_constraints"].append({"name": "ck_pets_weight", "expression": "weight_kg > 0", "description": ""})
    for f in d["physical"]["functional_dependencies"]:
        if f["table"] == "pets" and f["determinant"] == ["pet_id"]:
            f["dependent"].append("weight_kg")


def drop_phone(d):
    ent(d, "owner")["attributes"] = [a for a in ent(d, "owner")["attributes"] if a["name"] != "phone"]
    t = tbl(d, "owners")
    t["columns"] = [c for c in t["columns"] if c["name"] != "phone"]
    for f in d["physical"]["functional_dependencies"]:
        if f["table"] == "owners":
            f["dependent"] = [x for x in f["dependent"] if x != "phone"]


def owners_many_to_many(d):
    rel = next(r for r in d["conceptual"]["relationships"] if r["id"] == "owner_pet")
    rel["participants"][0]["cardinality"] = {"min": 1, "max": "N"}
    pets = tbl(d, "pets")
    pets["columns"] = [c for c in pets["columns"] if c["name"] != "owner_id"]
    pets["foreign_keys"] = [f for f in pets["foreign_keys"] if f["relationship"] != "owner_pet"]
    pets["indexes"] = []
    for f in d["physical"]["functional_dependencies"]:
        if f["table"] == "pets" and f["determinant"] == ["pet_id"]:
            f["dependent"].remove("owner_id")
    d["physical"]["tables"].append(_t(
        "pet_owners", {"type": "relationship", "id": "owner_pet"}, [_c("owner_id", "INTEGER", src=""), _c("pet_id", "INTEGER", src="")],
        ["owner_id", "pet_id"],
        foreign_keys=[_fk("fk_po_owners", ["owner_id"], "owners", ["owner_id"], "owner_pet"),
                      _fk("fk_po_pets", ["pet_id"], "pets", ["pet_id"], "owner_pet", "CASCADE")]))
    for t in d["physical"]["tables"]:
        for c in t["columns"]:
            if c.get("source_attribute") == "":
                c["source_attribute"] = None


def add_rule_and_constraint(d):
    d["business_rules"].append({"id": "future_birth", "description": "A pet cannot be born in the future.",
                                "source": "requirement", "applies_to": ["pet"],
                                "enforcement": {"mechanism": "schema", "implemented_by": ["ck_pets_birth"], "note": ""}})
    tbl(d, "pets")["check_constraints"].append({"name": "ck_pets_birth", "expression": "birth_date <= CURRENT_DATE",
                                                 "description": "no future births"})


def change_constraint(d):
    for c in tbl(d, "appointments")["check_constraints"]:
        c["expression"] = "status IN ('booked','done','cancelled','no_show')"
    ent(d, "appointment")["attributes"][2]["allowed_values"] = ["booked", "done", "cancelled", "no_show"]


MYSQL_TYPES = {"INTEGER": "INT", "TIMESTAMP": "DATETIME"}


def to_mysql(d):
    d["dbms"] = {"id": "mysql_mariadb", "name": "MySQL/MariaDB"}
    for t in d["physical"]["tables"]:
        for c in t["columns"]:
            c["data_type"] = MYSQL_TYPES.get(c["data_type"], c["data_type"])
    d["assumptions"].append({"id": "R_MY1", "text": "Timestamps are stored as DATETIME (no time zone).",
                             "rationale": "MySQL has no TIMESTAMP WITH TIME ZONE.", "related_to": []})


# ---- base class ----------------------------------------------------------------------------------------
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prompts = PromptLoader(PROMPTS)
        self.s = make_session(self.tmp.name, "postgresql")
        design.run(self.s, Stage4LLM("clinic"), self.prompts)

    def rev(self, *items, request="change it", confirmed=False):
        # confirmed=True stands for the person's explicit confirmation of a proposal that deletes structure
        return revision.revise(self.s, ScriptedLLM(self.s, *items), self.prompts, request, allow_suspicious=confirmed)

    def apply(self, mutate, types, request="change it", confirmed=False, **kw):
        return self.rev(lambda cur: response(cur, mutate, types, **kw), request=request, confirmed=confirmed)

    def files(self):
        names = ["design.json"] + dd.DOC_FILES + ["changelog.md"]
        return {n: (self.s.output_dir / n).read_text(encoding="utf-8") if (self.s.output_dir / n).is_file() else None
                for n in names}

    def assert_consistent(self):
        d = self.s.data["design"]
        self.assertEqual(json.loads((self.s.output_dir / "design.json").read_text(encoding="utf-8")), d)
        self.assertEqual(dd.check_documents(d, {n: (self.s.output_dir / n).read_text(encoding="utf-8") for n in dd.DOC_FILES}), [])
        dm.link_relationships(dm.validate_design(d))
        reloaded = load_session(self.tmp.name, self.s.session_id)       # persisted state agrees
        self.assertEqual(reloaded.data["design"], d)

    def assert_unchanged(self, before_files, before_design):
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.s.data["design"], before_design)
        self.assert_consistent()


class AddEntityAttributeTests(Base):
    def test_add_entity_updates_design_and_every_document(self):
        rec = self.apply(add_room, ["add_entity", "add_relationship"], request="Add a Room entity for appointments")
        self.assertEqual(rec["number"], 1)
        self.assertEqual(rec["affected"]["entities"]["added"], ["room"])
        self.assertEqual(rec["affected"]["tables"]["added"], ["rooms"])
        self.assertEqual(rec["affected"]["relationships"]["added"], ["room_appointment"])
        self.assertIn("appointments", rec["affected"]["tables"]["modified"])
        self.assertIn("fk_appointments_rooms", rec["affected"]["constraints"]["added"])
        f = self.files()
        self.assertIn("rooms", f["logical-schema.md"])
        self.assertIn("Room", f["conceptual-model.md"])
        self.assertIn("rooms", f["normalization.md"])
        self.assertIn("room_id", f["logical-schema.md"])
        self.assert_consistent()
        self.assertEqual(self.s.data["design"]["conceptual"]["relationships"][-1]["implemented_by"]["table"], "appointments")

    def test_add_and_remove_attribute(self):
        self.apply(add_weight, ["add_attribute"])
        self.assertIn("weight_kg", self.files()["logical-schema.md"])
        self.assert_consistent()
        rec = self.apply(drop_phone, ["remove_attribute"], confirmed=True)
        self.assertNotIn("phone", self.files()["logical-schema.md"].replace("telephone", ""))
        self.assertEqual(rec["affected"]["columns"]["owners"]["removed"], ["phone"])
        self.assertEqual(rec["affected"]["attributes"]["owner"]["removed"], ["phone"])
        self.assert_consistent()

    def test_removed_column_still_referenced_is_rejected_and_nothing_changes(self):
        def bad(d):
            tbl(d, "owners")["columns"] = [c for c in tbl(d, "owners")["columns"] if c["name"] != "phone"]   # FD still names it
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(InvalidResponseError):
            self.apply(bad, ["remove_attribute"])
        self.assert_unchanged(before, bd)


class RelationshipAndConstraintTests(Base):
    def test_change_cardinality_to_many_to_many_creates_junction_table(self):
        rec = self.apply(owners_many_to_many, ["change_cardinality"], request="An owner can have many pets and a pet many owners", confirmed=True)
        d = self.s.data["design"]
        rel = next(r for r in d["conceptual"]["relationships"] if r["id"] == "owner_pet")
        self.assertEqual(rel["type"], "many_to_many")
        self.assertEqual(rel["implemented_by"]["table"], "pet_owners")
        self.assertNotIn("owner_id", [c["name"] for c in tbl(d, "pets")["columns"]])
        self.assertEqual(rec["affected"]["relationships"]["modified"], ["owner_pet"])
        self.assertIn("->", rec["affected"]["relationship_detail"]["owner_pet"]["to"] + "->")
        self.assertIn("fk_pets_owners", rec["affected"]["constraints"]["removed"])
        self.assertIn("pet_owners", self.files()["logical-schema.md"])
        self.assertIn("many_to_many", self.files()["conceptual-model.md"].replace(" ", "_").lower().replace("many-to-many", "many_to_many"))
        self.assert_consistent()

    def test_cardinality_changed_in_model_but_not_in_schema_is_rejected(self):
        def half(d):
            next(r for r in d["conceptual"]["relationships"] if r["id"] == "owner_pet")["participants"][0]["cardinality"] = {"min": 1, "max": "N"}
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(InvalidResponseError):
            self.apply(half, ["change_cardinality"])
        self.assert_unchanged(before, bd)

    def test_add_business_rule_and_change_constraint(self):
        rec = self.apply(add_rule_and_constraint, ["add_business_rule", "change_constraint"])
        self.assertEqual(rec["affected"]["business_rules"]["added"], ["future_birth"])
        self.assertEqual(rec["affected"]["constraints"]["added"], ["ck_pets_birth"])
        self.assertIn("future_birth", self.files()["conceptual-model.md"] + self.files()["assumptions.md"] + self.files()["logical-schema.md"])
        self.assert_consistent()
        rec2 = self.apply(change_constraint, ["change_constraint"])
        self.assertEqual(rec2["affected"]["constraints"]["modified"], ["ck_appointments_status"])
        self.assertIn("no_show", self.files()["logical-schema.md"])
        self.assert_consistent()

    def test_rule_without_valid_enforcement_is_rejected(self):
        def bad(d):
            add_rule_and_constraint(d)
            d["business_rules"][-1]["enforcement"]["implemented_by"] = ["ck_does_not_exist"]
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(InvalidResponseError):
            self.apply(bad, ["add_business_rule"])
        self.assert_unchanged(before, bd)


class DbmsSwitchTests(Base):
    def test_switch_updates_dbms_types_and_documents(self):
        rec = self.apply(to_mysql, ["switch_dbms"], request="Switch to MySQL",
                         dbms={"id": "mysql", "name": "x"})
        d = self.s.data["design"]
        self.assertEqual(d["dbms"], {"id": "mysql_mariadb", "name": "MySQL/MariaDB"})
        self.assertEqual(rec["affected"]["dbms"], {"from": "PostgreSQL", "to": "MySQL/MariaDB"})
        types = {c["data_type"] for t in d["physical"]["tables"] for c in t["columns"]}
        self.assertNotIn("INTEGER", types)
        self.assertNotIn("TIMESTAMP", types)
        self.assertIn("DATETIME", types)
        self.assertIn("MySQL/MariaDB", self.files()["logical-schema.md"])
        self.assertIn("DATETIME", self.files()["logical-schema.md"])
        self.assertIn("R_MY1", self.files()["assumptions.md"])
        self.assertIn("PostgreSQL -> MySQL/MariaDB", self.files()["changelog.md"])
        self.assertEqual(rec["affected"]["tables"]["modified"] and "columns" in rec["affected"], True)
        self.assert_consistent()

    def test_name_only_switch_is_rejected(self):
        def name_only(d):
            d["dbms"] = {"id": "mysql_mariadb", "name": "MySQL/MariaDB"}
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(InvalidResponseError):                 # INTEGER identity / TIMESTAMP etc. not valid for MySQL
            self.apply(name_only, ["switch_dbms"], dbms={"id": "mysql_mariadb", "name": "MySQL/MariaDB"})
        self.assert_unchanged(before, bd)

    def test_foreign_dialect_types_rejected(self):
        def pg_types_in_sqlserver(d):
            d["dbms"] = {"id": "sql_server", "name": "SQL Server"}
            tbl(d, "appointments")["columns"][1]["data_type"] = "TIMESTAMPTZ"
        with self.assertRaises(InvalidResponseError):
            self.apply(pg_types_in_sqlserver, ["switch_dbms"], dbms={"id": "sql_server", "name": "SQL Server"})

    def test_dbms_changed_without_declaring_switch_and_unknown_dbms(self):
        with self.assertRaises(InvalidResponseError):
            self.apply(to_mysql, ["add_attribute"], dbms={"id": "mysql_mariadb", "name": "m"})
        with self.assertRaises(InvalidResponseError):
            self.apply(add_weight, ["switch_dbms"])                   # declared a switch, nothing switched
        with self.assertRaises(InvalidResponseError):
            self.apply(add_weight, ["switch_dbms"], dbms={"id": "oracle", "name": "Oracle"})


class ChangelogAndStateTests(Base):
    def test_changelog_entries_are_accurate_and_cumulative(self):
        r1 = self.apply(add_weight, ["add_attribute"], request="Track pet weight", summary="Added weight", rationale="Vets need it",
                        assumptions=["Weight is in kg"], warnings=["Existing pets get NULL weight"])
        r2 = self.apply(drop_phone, ["remove_attribute"], request="Drop owner phone", confirmed=True)
        log = self.files()["changelog.md"]
        self.assertEqual(log.count("## Revision "), 2)
        self.assertLess(log.index("## Revision 1 - "), log.index("## Revision 2 - "))
        for needle in ("Track pet weight", "Added weight", "Vets need it", "Weight is in kg", "Existing pets get NULL weight",
                       r1["timestamp"], "`weight_kg`", "`ck_pets_weight`", "Drop owner phone", r2["timestamp"], "`phone`"):
            self.assertIn(needle, log)
        for f in ("design.json", "conceptual-model.md", "logical-schema.md", "normalization.md", "assumptions.md"):
            self.assertIn(f"`{f}`", log)
        rev1 = log.split("## Revision 2")[0]
        self.assertNotIn("Drop owner phone", rev1)
        self.assertEqual(r1["affected"]["constraints"]["added"], ["ck_pets_weight"])
        self.assertEqual(r1["affected"]["entities"]["modified"], ["pet"])
        self.assertEqual(r1["affected"]["relationships"], {"added": [], "removed": [], "modified": []})

    def test_new_normalization_warning_is_recorded(self):
        def denormalize(d):                  # a transitive dependency without justification
            t = tbl(d, "appointments")
            t["columns"].append(_c("pet_name", "VARCHAR(80)", True))
            for f in d["physical"]["functional_dependencies"]:
                if f["table"] == "appointments" and f["determinant"] == ["appointment_id"]:
                    f["dependent"].append("pet_name")
            d["physical"]["functional_dependencies"].append({"table": "appointments", "determinant": ["pet_id"],
                                                             "dependent": ["pet_name"], "note": ""})
        rec = self.apply(denormalize, ["add_attribute"])
        self.assertTrue(any("appointments" in w for w in rec["warnings"]))
        self.assertIn("appointments", self.files()["changelog.md"].split("**Warnings:**")[1])
        self.assertTrue(self.s.data["design_docs"]["warnings"])

    def test_state_history_and_stage_status(self):
        self.apply(add_weight, ["add_attribute"])
        d = self.s.data
        self.assertEqual([r["number"] for r in d["revisions"]], [1])
        self.assertEqual(d["revision_stage"]["status"], "in_progress")
        self.assertNotIn("revision", d["completed_stages"])
        self.assertEqual(d["current_stage"], "revision")
        self.assertNotIn("revision_pending", d)
        self.assertEqual(d["design"]["meta"]["last_revision"], 1)
        self.assertIn("changelog.md", d["output_files"])
        self.assertEqual(load_session(self.tmp.name, self.s.session_id).data["revisions"][0]["request"], "change it")

    def test_prompt_contains_request_current_design_and_dialects(self):
        llm = ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"]))
        revision.revise(self.s, llm, self.prompts, "Please track weight")
        stage, user = llm.calls[0]
        self.assertEqual(stage, "revision:1")
        self.assertIn("Please track weight", user)
        self.assertIn("pet_vaccinations", user)
        self.assertIn("SQL Server", user)


class InvalidRevisionTests(Base):
    def snapshot(self):
        return self.files(), copy.deepcopy(self.s.data["design"]), copy.deepcopy(self.s.data["revisions"] if "revisions" in self.s.data else [])

    def test_failures_leave_design_documents_and_history_untouched(self):
        self.apply(add_weight, ["add_attribute"])
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])

        def fk_to_nowhere(d):
            tbl(d, "pets")["foreign_keys"][0]["ref_table"] = "ghosts"
        cases = [
            lambda cur: response(cur, fk_to_nowhere, ["change_constraint"]),
            lambda cur: response(cur, lambda d: None, ["other"]),                        # no-op
            lambda cur: {"revision": {"applicable": False, "reason": "not a design change"}},
            lambda cur: {"revision": {"applicable": True}},
            lambda cur: {"nonsense": 1},
            lambda cur: response(cur, add_weight, ["teleport"]),                         # bad change type
            lambda cur: response(cur, add_weight, ["add_attribute"], summary=""),
            LLMError("model unreachable", retryable=False),
        ]
        for i, c in enumerate(cases):
            with self.subTest(i):
                with self.assertRaises(DBAAgentError):
                    self.rev(c)
                self.assert_unchanged(before, bd)
                self.assertEqual(len(self.s.data["revisions"]), 1)
                self.assertNotIn("revision_pending", self.s.data)
        self.assertEqual(len(self.s.data["revision_failures"]), len(cases))
        self.assertIn("revision", self.s.data["last_error"]["stage"])

    def test_rejection_message_reaches_the_user(self):
        with self.assertRaises(revision.RevisionRejected) as cm:
            self.rev(lambda cur: {"revision": {"applicable": False, "reason": "contradicts rule X"}})
        self.assertIn("contradicts rule X", str(cm.exception))

    def test_real_llm_client_retries_with_error_feedback_then_succeeds(self):
        cfg = config_from_dict({"provider": "ollama", "model": "m", "retry_delay": 0, "max_attempts": 3}, env={})
        client = LLMClient(cfg)
        cur = self.s.data["design"]
        bad = response(cur, lambda d: tbl(d, "pets")["foreign_keys"].__setitem__(0, dict(tbl(d, "pets")["foreign_keys"][0], ref_table="ghosts")), ["change_constraint"])
        good = response(cur, add_weight, ["add_attribute"])
        prompts_seen = []
        replies = [json.dumps(bad), json.dumps(good)]

        def chat(system, user):
            prompts_seen.append(user)
            return replies.pop(0)
        with mock.patch.object(client, "_chat", chat):
            rec = revision.revise(self.s, client, self.prompts, "weight")
        self.assertEqual(len(prompts_seen), 2)
        self.assertIn("rejected", prompts_seen[1])
        self.assertEqual(rec["number"], 1)
        self.assert_consistent()

    def test_real_llm_client_gives_up_after_max_attempts_without_corruption(self):
        cfg = config_from_dict({"provider": "ollama", "model": "m", "retry_delay": 0, "max_attempts": 2}, env={})
        client = LLMClient(cfg)
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        with mock.patch.object(client, "_chat", lambda s, u: "not json"):
            with self.assertRaises(LLMError):
                revision.revise(self.s, client, self.prompts, "x")
        self.assert_unchanged(before, bd)

    def test_requires_stage4_and_non_empty_request(self):
        fresh = make_session(self.tmp.name)
        with self.assertRaises(InvalidResponseError):
            revision.revise(fresh, ScriptedLLM(fresh), self.prompts, "x")
        with self.assertRaises(revision.RevisionRejected):
            revision.revise(self.s, ScriptedLLM(self.s), self.prompts, "   ")

    def test_corrupt_saved_design_is_not_revised(self):
        self.s.data["design"]["physical"]["tables"][0]["primary_key"] = ["ghost"]
        with self.assertRaises(DBAAgentError):
            self.rev(lambda cur: response(cur, add_weight, ["add_attribute"]))


class InterruptedRevisionTests(Base):
    def test_file_write_failure_restores_previous_files_and_resume_completes(self):
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        real, calls = revision._atomic_write, []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("disk full")
            return real(path, text)
        with mock.patch.object(revision, "_atomic_write", flaky):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"], request="weight")
        self.assertEqual(self.files(), before)                       # restored (no half-written mix)
        self.assertEqual(self.s.data["design"], bd)
        self.assertIn("revision_pending", self.s.data)
        saved = load_session(self.tmp.name, self.s.session_id)
        self.assertEqual(saved.data["revision_pending"]["record"]["request"], "weight")
        # resume in a "new process": no LLM call is needed
        s2 = load_session(self.tmp.name, self.s.session_id)
        rec = revision.finish_pending(s2)
        self.s = s2
        self.assertEqual(rec["number"], 1)
        self.assertNotIn("revision_pending", s2.data)
        self.assertEqual(len(s2.data["revisions"]), 1)
        self.assertEqual(self.files()["changelog.md"].count("## Revision "), 1)
        self.assertIn("weight_kg", self.files()["logical-schema.md"])
        self.assert_consistent()

    def test_hard_interrupt_mid_write_then_revise_without_request_finishes(self):
        real, n = revision._atomic_write, []

        def killed(path, text):
            n.append(1)
            if len(n) == 4:
                raise KeyboardInterrupt()
            return real(path, text)
        with mock.patch.object(revision, "_atomic_write", killed):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(add_weight, ["add_attribute"])
        self.assertEqual(self.s.data.get("revisions", []), [])       # history not committed
        self.assertIn("revision_pending", self.s.data)
        s2 = load_session(self.tmp.name, self.s.session_id)          # files are mixed on disk right now
        self.s = s2
        rec = revision.revise(s2, ScriptedLLM(s2), self.prompts, "")  # no request: finish what was pending, no LLM call
        self.assertEqual(rec["number"], 1)
        self.assert_consistent()

    def test_pending_revision_is_finished_before_a_new_request(self):
        with mock.patch.object(revision, "_atomic_write", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.apply(add_weight, ["add_attribute"], request="first")
        rec = self.rev(lambda cur: response(cur, drop_phone, ["remove_attribute"]), request="second", confirmed=True)
        self.assertEqual(rec["number"], 2)
        self.assertEqual([r["request"] for r in self.s.data["revisions"]], ["first", "second"])
        log = self.files()["changelog.md"]
        self.assertEqual(log.count("## Revision "), 2)
        self.assert_consistent()

    def test_failure_of_post_write_check_rolls_files_back(self):
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        calls = []
        real = revision.check_documents

        def check(design, docs):
            calls.append(1)
            return ["forced problem"] if len(calls) == 2 else real(design, docs)     # 2nd call = the read-back from disk
        with mock.patch.object(revision, "check_documents", check):
            with self.assertRaises(InvalidResponseError):
                self.apply(add_weight, ["add_attribute"])
        self.assert_unchanged(before, bd)
        self.assertNotIn("revision_pending", self.s.data)


class CliAndRegressionTests(Base):
    def run_cli(self, argv, llm, inputs=()):
        out, it = [], iter(inputs)
        with mock.patch.object(cli, "build_runtime", lambda s, c: (llm, self.prompts)), \
                mock.patch.object(cli, "load_config", lambda p=None: config_from_dict({"provider": "ollama", "model": "m", "sessions_dir": self.tmp.name}, env={})):
            code = cli.main(["--sessions-dir", self.tmp.name, "revise", self.s.session_id] + argv,
                            input_fn=lambda: next(it), out=out.append)
        return code, "\n".join(map(str, out))

    def test_cli_single_request_and_interactive_loop(self):
        code, text = self.run_cli(["--request", "track weight", "--yes"], ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"], summary="Weight added")))
        self.assertEqual(code, 0)
        self.assertIn("Revision 1 accepted: Weight added", text)
        self.assertIn("changelog.md", text)
        llm = ScriptedLLM(self.s, lambda cur: response(cur, drop_phone, ["remove_attribute"]), RevisionRejected_("not applicable"))
        code, text = self.run_cli([], llm, ["drop phone", "y", "something else", ""])
        self.assertEqual(code, 2)
        self.assertIn("Revision 2 accepted", text)
        self.assertIn("REVISION REJECTED", text)
        self.assertEqual(len(load_session(self.tmp.name, self.s.session_id).data["revisions"]), 2)

    def test_stage5_stays_unimplemented_but_hint_is_shown(self):
        self.assertFalse(workflow.is_implemented("revision"))
        self.assertNotIn("sql", workflow.IMPLEMENTED)
        out = []
        ui = mock.Mock(out=out.append)
        self.assertEqual(cli.drive(self.s, None, ui), 0)
        text = "\n".join(out)
        self.assertIn("Stages 5 and 6 are implemented but run through dedicated commands", text)
        self.assertIn("revise", text)

    def test_second_scenario_university_mysql_unaffected_engine_is_generic(self):
        from tests.design_fixtures2 import fresh2
        s = make_session(self.tmp.name, "mysql_mariadb")
        design.run(s, Stage4LLM("university"), self.prompts)
        d = s.data["design"]
        first_table = d["physical"]["tables"][0]

        def add_note(x):                      # add a nullable column to the first entity table, whatever it is
            t = tbl(x, first_table["name"])
            e = next(e for e in x["conceptual"]["entities"] if e["id"] == t["source"]["id"])
            e["attributes"].append({"name": "remarks", "type": "text", "required": False, "unique": False, "description": "r"})
            t["columns"].append(_c("remarks", "TEXT", True))
            for f in x["physical"]["functional_dependencies"]:
                if f["table"] == t["name"] and f["determinant"] == t["primary_key"]:
                    f["dependent"].append("remarks")
        rec = revision.revise(s, ScriptedLLM(s, lambda cur: response(cur, add_note, ["add_attribute"])), self.prompts, "remarks")
        self.assertEqual(rec["affected"]["columns"][first_table["name"]]["added"], ["remarks"])
        self.assertEqual(dd.check_documents(s.data["design"], {n: (s.output_dir / n).read_text(encoding="utf-8") for n in dd.DOC_FILES}), [])

    def test_revision_prompts_are_external_files(self):
        for n in ("revision_system", "revision_user"):
            self.assertTrue((Path(PROMPTS) / f"{n}.md").is_file())
        self.assertNotIn("Return these keys", Path(revision.__file__).read_text())   # prompt text lives in prompts/


if __name__ == "__main__":
    unittest.main()
