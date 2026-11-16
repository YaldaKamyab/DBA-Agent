"""Stage 5B: DBMS-switch propagation, revision preview/accept/reject, version snapshots, explicit approval."""
import copy
import json
from unittest import mock

from dba_agent import approval, cli, design_docs as dd, revision, versions, workflow
from dba_agent.config import config_from_dict
from dba_agent.errors import InvalidResponseError
from dba_agent.state import load_session
from dba_agent.stages import dbms as dbms_stage
from tests.design_fixtures import _c
from tests.test_stage5a_revision import (Base, ScriptedLLM, add_weight, drop_phone, response, tbl, to_mysql)


def pet_weight_but_phone_lost(d):          # an internally valid proposal that does NOT match the request
    add_weight(d)
    drop_phone(d)


def unjustified_denormalization(d):        # transitive dependency, no justification -> below 3NF
    t = tbl(d, "appointments")
    t["columns"].append(_c("pet_name", "VARCHAR(80)", True))
    for f in d["physical"]["functional_dependencies"]:
        if f["table"] == "appointments" and f["determinant"] == ["appointment_id"]:
            f["dependent"].append("pet_name")
    d["physical"]["functional_dependencies"].append(
        {"table": "appointments", "determinant": ["pet_id"], "dependent": ["pet_name"], "note": ""})


def full_choice():
    """A complete Stage 3 result (what the real stage stores), with its dbms-choice.md."""
    ids = dbms_stage.DBMS_IDS
    return dbms_stage.validate_choice({
        "selected": "postgresql", "summary": "PostgreSQL fits.", "justification": "Strong integrity and concurrency.",
        "key_requirements": ["many concurrent users"],
        "comparison": [{"criterion": c, "relevance": "r", "ratings": {i: f"{i} ok" for i in ids}} for c in dbms_stage.CRITERIA_IDS],
        "rejected": [{"dbms": i, "reasons": [f"{i} is weaker here"]} for i in ids if i != "postgresql"]})


class B(Base):
    def setUp(self):
        super().setUp()
        self.s.data["dbms_choice"] = full_choice()
        self.s.write_output("dbms-choice.md", dbms_stage.render_markdown(self.s.data["dbms_choice"], None, "Demo"))
        self.s.save()

    def state(self):
        return load_session(self.tmp.name, self.s.session_id).data

    def switch(self):
        return self.apply(to_mysql, ["switch_dbms"], request="Switch to MySQL", dbms={"id": "mysql", "name": "x"})

    def approve(self):
        return approval.approve(self.s)

    def run_cli(self, cmd, argv=(), inputs=(), llm=None):
        out, it = [], iter(inputs)

        def read():
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        with mock.patch.object(cli, "build_runtime", lambda s, c: (llm, self.prompts)), \
                mock.patch.object(cli, "load_config", lambda p=None: config_from_dict(
                    {"provider": "ollama", "model": "m", "sessions_dir": self.tmp.name}, env={})):
            code = cli.main(["--sessions-dir", self.tmp.name, cmd, self.s.session_id] + list(argv),
                            input_fn=read, out=out.append)
        return code, "\n".join(map(str, out))


# ---------------------------------------------------------------- Part 1: DBMS switch propagation
class DbmsSwitchPropagationTests(B):
    def test_state_design_and_document_agree_after_switch(self):
        rec = self.switch()
        d, st = self.s.data["design"], self.state()
        self.assertEqual(d["dbms"]["id"], "mysql_mariadb")
        self.assertEqual(st["dbms_choice"]["selected_id"], "mysql_mariadb")
        self.assertEqual(st["dbms_choice"]["selected"], "MySQL/MariaDB")
        md = self.s.output_dir.joinpath("dbms-choice.md").read_text(encoding="utf-8")
        self.assertIn("## Selected DBMS: MySQL/MariaDB", md)
        self.assertIn("dbms-choice.md", rec["files_regenerated"])
        self.assertIn("`dbms-choice.md`", self.files()["changelog.md"])
        self.assertEqual(approval.check_approvable(self.s)["problems"], [])      # all three agree

    def test_override_is_identified_and_original_reasoning_is_kept_not_falsified(self):
        self.switch()
        choice = self.state()["dbms_choice"]
        self.assertEqual(choice["overrides"][0]["from_id"], "postgresql")
        self.assertEqual(choice["overrides"][0]["request"], "Switch to MySQL")
        self.assertEqual(choice["original"]["selected_id"], "postgresql")        # first analysis kept as it was
        md = self.s.output_dir.joinpath("dbms-choice.md").read_text(encoding="utf-8")
        self.assertIn("User-Requested DBMS Override", md)
        self.assertIn("Switch to MySQL", md)
        self.assertIn("Superseded Original Recommendation: PostgreSQL", md)
        self.assertIn("not re-evaluated", md)
        self.assertIn("Originally recommended; replaced", md)                  # PostgreSQL is listed as superseded

    def test_switching_back_keeps_both_overrides(self):
        self.switch()

        def back(d):
            for t in d["physical"]["tables"]:
                for c in t["columns"]:
                    c["data_type"] = {"DATETIME": "TIMESTAMP"}.get(c["data_type"], c["data_type"])
            d["dbms"] = {"id": "postgresql", "name": "PostgreSQL"}
            d["assumptions"].append({"id": "R_PG1", "text": "Back to timestamps.", "rationale": "r", "related_to": []})
        self.apply(back, ["switch_dbms"], dbms={"id": "postgresql", "name": "x"})
        ch = self.state()["dbms_choice"]
        self.assertEqual(ch["selected_id"], "postgresql")
        self.assertEqual([o["to_id"] for o in ch["overrides"]], ["mysql_mariadb", "postgresql"])
        self.assertEqual(approval.check_approvable(self.s)["problems"], [])

    def test_failed_switch_restores_every_file_and_state(self):
        before, bd, choice = self.files(), copy.deepcopy(self.s.data["design"]), copy.deepcopy(self.s.data["dbms_choice"])
        before_choice_md = (self.s.output_dir / "dbms-choice.md").read_text(encoding="utf-8") \
            if (self.s.output_dir / "dbms-choice.md").exists() else None
        real = revision._atomic_write

        def flaky(path, text):                       # fails while writing the LAST file, after dbms-choice.md
            if str(path).endswith("changelog.md") and "Switch" in text:
                raise OSError("disk full")
            real(path, text)
        with mock.patch.object(revision, "_atomic_write", flaky):
            with self.assertRaises(OSError):
                self.switch()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.s.data["design"], bd)
        self.assertEqual(self.s.data["dbms_choice"], choice)
        self.assertEqual(self.state()["dbms_choice"], choice)
        p = self.s.output_dir / "dbms-choice.md"
        self.assertEqual(p.read_text(encoding="utf-8") if p.exists() else None, before_choice_md)
        self.assertEqual(self.s.data["revisions"] if "revisions" in self.s.data else [], [])

    def test_rejected_switch_proposal_changes_nothing(self):
        before, bd, choice = self.files(), copy.deepcopy(self.s.data["design"]), copy.deepcopy(self.s.data["dbms_choice"])
        llm = ScriptedLLM(self.s, lambda cur: response(cur, to_mysql, ["switch_dbms"], dbms={"id": "mysql", "name": "x"}))
        self.assertIsNone(revision.revise(self.s, llm, self.prompts, "Switch to MySQL", confirm=lambda p, r: False))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.s.data["dbms_choice"], choice)
        self.assertEqual(self.s.data["design"], bd)


# ---------------------------------------------------------------- Part 2: preview / accept / reject
class PreviewAndConfirmationTests(B):
    def propose(self, mutate, types, confirm, **kw):
        llm = ScriptedLLM(self.s, lambda cur: response(cur, mutate, types, **kw))
        return revision.revise(self.s, llm, self.prompts, "make the change", confirm=confirm), llm

    def test_rejected_revision_leaves_files_state_and_history_unchanged(self):
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        rec, llm = self.propose(add_weight, ["add_attribute"], lambda p, r: False)
        self.assertIsNone(rec)
        self.assert_unchanged(before, bd)
        self.assertEqual(self.s.data.get("revisions", []), [])
        self.assertNotIn("revision_pending", self.s.data)
        self.assertEqual(len(self.state()["revision_rejections"]), 1)
        self.assertEqual(len(llm.calls), 1)                                   # no extra LLM call for the rejection
        self.assertFalse(versions.has_version(self.s, 1))

    def test_preview_shows_summary_and_deterministic_diff_before_commit(self):
        seen = {}

        def confirm(preview, record):
            seen["preview"] = preview
            seen["files_on_disk_unchanged"] = "weight_kg" not in self.files()["logical-schema.md"]
            seen["state_unchanged"] = "revisions" not in self.s.data or not self.s.data["revisions"]
            return True
        rec, _ = self.propose(add_weight, ["add_attribute"], confirm, summary="Track pet weight")
        p = seen["preview"]
        self.assertIn("Proposed change: Track pet weight", p)
        self.assertIn("+ attribute pet.weight_kg", p)
        self.assertIn("+ column pets.weight_kg", p)
        self.assertIn("+ check ck_pets_weight on pets", p)
        self.assertIn("Files that would be regenerated", p)
        self.assertTrue(seen["files_on_disk_unchanged"] and seen["state_unchanged"])      # nothing written yet
        self.assertEqual(rec["number"], 1)
        self.assertIn("weight_kg", self.files()["logical-schema.md"])

    def test_wrong_change_is_flagged_pet_weight_removes_owner_phone(self):
        seen = {}
        before = self.files()
        self.propose(pet_weight_but_phone_lost, ["add_attribute"], lambda p, r: seen.update(p=p, r=r) or False)
        self.assertIn("CHECK CAREFULLY", seen["p"])
        self.assertIn("attribute `owner.phone`", seen["p"])
        self.assertIn("- column owners.phone", seen["p"])
        self.assertTrue(seen["r"]["unexpected_removals"])
        self.assertEqual(self.files(), before)

    def test_genuine_removal_is_not_flagged(self):
        seen = {}
        self.propose(drop_phone, ["remove_attribute"], lambda p, r: seen.update(p=p) or False)
        self.assertNotIn("CHECK CAREFULLY", seen["p"])
        self.assertIn("- attribute owner.phone", seen["p"])

    def test_cli_default_asks_and_defaults_to_no(self):
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        mk = lambda: ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"], summary="Weight"))
        code, text = self.run_cli("revise", ["--request", "track weight"], ["", ], mk())      # just Enter
        self.assertEqual(code, 0)
        self.assertIn("Accept this revision? [y/N]", text)
        self.assertIn("Revision declined", text)
        self.assert_unchanged(before, bd)
        code, text = self.run_cli("revise", ["--request", "track weight"], [], mk())            # no input at all
        self.assertIn("treated as 'no'", text)
        self.assert_unchanged(before, bd)
        code, text = self.run_cli("revise", ["--request", "track weight"], ["y"], mk())
        self.assertIn("Revision 1 accepted", text)
        self.assertIn("approve", text)

    def test_cli_yes_refuses_unintended_removals_but_accepts_normal_changes(self):
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        bad = ScriptedLLM(self.s, lambda cur: response(cur, pet_weight_but_phone_lost, ["add_attribute"]))
        code, text = self.run_cli("revise", ["--request", "x", "--yes"], [], bad)
        self.assertIn("Not accepted automatically", text)
        self.assert_unchanged(before, bd)
        good = ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"]))
        code, text = self.run_cli("revise", ["--request", "x", "--yes"], [], good)
        self.assertIn("Revision 1 accepted", text)

    def test_last_valid_design_survives_reject_then_accept(self):
        self.propose(add_weight, ["add_attribute"], lambda p, r: False)
        rec, _ = self.propose(drop_phone, ["remove_attribute"], lambda p, r: True)
        self.assertEqual(rec["number"], 1)                                      # rejected proposals use no number
        self.assert_consistent()


# ---------------------------------------------------------------- Part 2: snapshots / recovery
class VersionTests(B):
    def test_snapshots_for_initial_design_and_each_accepted_revision(self):
        original = copy.deepcopy(self.s.data["design"])
        self.apply(add_weight, ["add_attribute"])
        self.apply(drop_phone, ["remove_attribute"], confirmed=True)
        self.assertEqual([m["number"] for m in versions.list_versions(self.s)], [0, 1, 2])
        meta, d0, _ = versions.load_version(self.s, 0)
        self.assertEqual(d0, original)
        _, d1, _ = versions.load_version(self.s, 1)
        self.assertIn("weight_kg", [a["name"] for e in d1["conceptual"]["entities"] for a in e["attributes"]])
        folder = versions.version_dir(self.s, 1)
        for n in ["design.json", "changelog.md", "logical-schema.md", "conceptual-model.md"]:
            self.assertTrue((folder / n).is_file(), n)
        self.assertEqual(json.loads((folder / "design.json").read_text(encoding="utf-8")), d1)

    def test_restore_earlier_version_restores_design_documents_and_logs_it(self):
        original = copy.deepcopy(self.s.data["design"])
        self.apply(add_weight, ["add_attribute"])
        self.apply(drop_phone, ["remove_attribute"], confirmed=True)
        rec = revision.restore_version(self.s, 1)
        self.assertEqual(rec["number"], 3)
        self.assertEqual(rec["restored_from"], 1)
        d = self.s.data["design"]
        self.assertIn("phone", [a["name"] for e in d["conceptual"]["entities"] for a in e["attributes"]])
        self.assertIn("weight_kg", [a["name"] for e in d["conceptual"]["entities"] for a in e["attributes"]])
        self.assert_consistent()
        self.assertIn("Restored version 1", self.files()["changelog.md"])
        self.assertEqual(len(self.s.data["revisions"]), 3)                      # history only grows
        self.assertTrue(versions.has_version(self.s, 3))
        revision.restore_version(self.s, 0)
        self.assertEqual(approval.design_fingerprint(self.s.data["design"]), approval.design_fingerprint(original))
        self.assert_consistent()

    def test_restore_brings_back_dbms_choice_and_document(self):
        self.apply(add_weight, ["add_attribute"])
        self.switch()
        self.assertEqual(self.s.data["dbms_choice"]["selected_id"], "mysql_mariadb")
        revision.restore_version(self.s, 1)
        self.assertEqual(self.s.data["design"]["dbms"]["id"], "postgresql")
        self.assertEqual(self.s.data["dbms_choice"]["selected_id"], "postgresql")
        self.assertEqual(approval.check_approvable(self.s)["problems"], [])
        self.assertNotIn("Override", self.s.output_dir.joinpath("dbms-choice.md").read_text(encoding="utf-8"))

    def test_restore_errors_leave_everything_unchanged(self):
        self.apply(add_weight, ["add_attribute"])
        before, bd = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(Exception):
            revision.restore_version(self.s, 99)
        with self.assertRaises(Exception):
            revision.restore_version(self.s, 1)                                  # already current
        self.assertIsNone(revision.restore_version(self.s, 0, confirm=lambda p, r: False))
        self.assert_unchanged(before, bd)

    def test_cli_versions_and_restore(self):
        self.apply(add_weight, ["add_attribute"])
        code, text = self.run_cli("versions")
        self.assertIn("v000", text)
        self.assertIn("v001", text)
        self.assertIn("<- current", text)
        code, text = self.run_cli("restore", ["--version", "0"], ["n"])
        self.assertEqual(code, 1)
        self.assertIn("weight_kg", self.files()["logical-schema.md"])             # declined: still there
        code, text = self.run_cli("restore", ["--version", "0", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("Restored version 0", text)
        self.assertNotIn("weight_kg", self.files()["logical-schema.md"])


# ---------------------------------------------------------------- Part 3: explicit approval
class ApprovalTests(B):
    def test_stage5_is_never_approved_automatically(self):
        self.apply(add_weight, ["add_attribute"])
        st = self.state()
        self.assertEqual(st["current_stage"], "revision")
        self.assertNotIn("revision", st["completed_stages"])
        self.assertNotIn("approval", st)
        self.assertEqual(st["revision_stage"]["status"], "in_progress")

    def test_explicit_approval_saves_status_time_version_and_advances(self):
        self.apply(add_weight, ["add_attribute"])
        ap, new = self.approve()
        self.assertTrue(new)
        st = self.state()                                                        # persisted
        self.assertEqual(st["current_stage"], "sql")
        self.assertIn("revision", st["completed_stages"])
        a = st["approval"]
        self.assertEqual((a["status"], a["design_version"]), ("approved", 1))
        self.assertTrue(a["approved_at"])
        self.assertEqual(a["design_fingerprint"], approval.design_fingerprint(st["design"]))
        self.assertEqual(st["revision_stage"]["status"], "approved")
        self.assertTrue(a["limitations_acknowledged"])                           # limits of the analysis are recorded
        again, new2 = self.approve()                                             # idempotent
        self.assertFalse(new2)
        self.assertEqual(again["approved_at"], a["approved_at"])

    def test_approval_works_without_any_revision(self):
        ap, _ = self.approve()
        self.assertEqual(ap["design_version"], 0)
        self.assertEqual(self.s.current_stage, "sql")

    def test_refused_when_documents_or_design_files_are_inconsistent(self):
        p = self.s.output_dir / "logical-schema.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n| ghost | x |\n", encoding="utf-8")
        before = copy.deepcopy(self.s.data)
        with self.assertRaises(approval.ApprovalError):
            self.approve()
        self.assertEqual(self.s.data, before)
        self.assertEqual(self.s.current_stage, "revision")
        (self.s.output_dir / "logical-schema.md").unlink()
        self.assertTrue(any("Missing documents" in x for x in approval.check_approvable(self.s)["problems"]))

    def test_refused_when_saved_dbms_choice_disagrees_with_design(self):
        self.s.data["dbms_choice"]["selected_id"] = "sqlite"
        problems = approval.check_approvable(self.s)["problems"]
        self.assertTrue(any("Saved DBMS selection" in x for x in problems))
        with self.assertRaises(approval.ApprovalError):
            self.approve()
        self.assertNotIn("approval", self.s.data)

    def test_refused_for_unjustified_normalization_violation_and_limits_are_reported(self):
        self.apply(unjustified_denormalization, ["add_attribute"])
        res = approval.check_approvable(self.s)
        self.assertTrue(any("below 3NF" in x and "appointments" in x for x in res["problems"]))
        self.assertTrue(res["limitations"])
        with self.assertRaises(approval.ApprovalError) as cm:
            self.approve()
        self.assertIn("appointments", str(cm.exception))
        self.assertEqual(self.s.current_stage, "revision")
        code, text = self.run_cli("approve", ["--yes"])
        self.assertEqual(code, 2)
        self.assertIn("CANNOT APPROVE", text)
        self.assertIn("declared dependencies", text)                             # limitation shown to the user

    def test_refused_while_a_revision_is_pending(self):
        self.s.data["revision_pending"] = {"candidate": {}, "record": {}}
        self.assertTrue(any("pending" in x for x in approval.check_approvable(self.s)["problems"]))

    def test_refused_before_stage4(self):
        s2 = type(self.s)(self.s.root, {"current_stage": "design", "session_id": "x"})
        self.assertTrue(approval.check_approvable(s2)["problems"])

    def test_further_accepted_revision_invalidates_approval(self):
        self.approve()
        rec = self.apply(add_weight, ["add_attribute"])
        st = self.state()
        self.assertNotIn("approval", st)
        self.assertEqual(st["current_stage"], "revision")
        self.assertNotIn("revision", st["completed_stages"])
        self.assertEqual(st["approval_history"][0]["status"], "invalidated")
        self.assertEqual(st["approval_history"][0]["invalidated_by_revision"], 1)
        self.assertTrue(rec["approval_invalidated"])
        self.assertIn("approval", self.files()["changelog.md"].lower())
        with self.assertRaises(approval.ApprovalError):
            approval.require_approved_design(self.s)
        self.approve()                                                           # can be approved again
        self.assertEqual(self.s.data["approval"]["design_version"], 1)

    def test_rejected_or_failed_revision_keeps_approval(self):
        self.approve()
        revision.revise(self.s, ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"])),
                        self.prompts, "x", confirm=lambda p, r: False)
        with self.assertRaises(InvalidResponseError):
            self.apply(lambda d: None, ["add_attribute"])
        self.assertEqual(self.s.data["approval"]["status"], "approved")
        self.assertEqual(self.s.current_stage, "sql")

    def test_stage6_guard_detects_tampering_after_approval(self):
        with self.assertRaises(approval.ApprovalError):
            approval.require_approved_design(self.s)                             # not approved yet
        self.approve()
        self.assertEqual(approval.require_approved_design(self.s)["status"], "approved")
        self.s.data["design"]["business_rules"].pop()
        with self.assertRaises(approval.ApprovalError):
            approval.require_approved_design(self.s)

    def test_cli_review_and_approve_flow(self):
        code, text = self.run_cli("review")
        self.assertEqual(code, 0)
        self.assertIn("Ready for approval", text)
        self.assertIn("Fingerprint", text)
        self.assertNotIn("approval", self.state())                               # review never approves
        code, text = self.run_cli("approve", [], ["n"])
        self.assertEqual(code, 1)
        self.assertNotIn("approval", self.state())
        code, text = self.run_cli("approve", [], [])                              # EOF -> not approved
        self.assertEqual(code, 1)
        code, text = self.run_cli("approve", [], ["y"])
        self.assertEqual(code, 0)
        self.assertIn("Design approved", text)
        self.assertEqual(self.state()["current_stage"], "sql")
        code, text = self.run_cli("approve", ["--yes"])
        self.assertIn("already approved", text)

    def test_save_resume_and_drive_with_approval(self):
        self.apply(add_weight, ["add_attribute"])
        out = []
        ui = mock.Mock(out=out.append)
        self.assertEqual(cli.drive(load_session(self.tmp.name, self.s.session_id), None, ui), 0)
        self.assertIn("approve", "\n".join(out))                                 # Stage 5 hint before approval
        self.approve()
        reloaded = load_session(self.tmp.name, self.s.session_id)
        self.assertEqual(reloaded.current_stage, "sql")
        out.clear()
        self.assertEqual(cli.drive(reloaded, None, ui), 0)
        self.assertIn("approved", "\n".join(out))
        code, text = self.run_cli("resume")                                       # resume does not break or revert it
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["current_stage"], "sql")
        self.assertEqual(approval.require_approved_design(reloaded)["design_version"], 1)
        self.assertFalse(workflow.is_implemented("sql"))                          # Stage 6 is NOT implemented here
