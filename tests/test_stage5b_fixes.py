"""Stage 5B fixes: approved-design integrity, suspicious-change detection, propose/accept/commit safety."""
import json

from dba_agent import approval, revision
from dba_agent.approval import ApprovalError
from dba_agent.design_docs import DOC_FILES
from dba_agent.errors import DBAAgentError
from tests.test_stage5a_revision import ScriptedLLM, add_weight, add_room, drop_phone, response, tbl
from tests.test_stage5b_safety import B, pet_weight_but_phone_lost


class IntegrityTests(B):
    def setUp(self):
        super().setUp()
        self.approve()

    def path(self, name):
        return self.s.output_dir / name

    def assertBlocked(self, fragment=None):
        with self.assertRaises(ApprovalError) as cm:
            approval.verify_approved_design(self.s)
        if fragment:
            self.assertIn(fragment, str(cm.exception))

    def test_untouched_approval_passes_and_returns_design_copy(self):
        ap, design = approval.verify_approved_design(self.s)
        self.assertEqual(ap["status"], "approved")
        self.assertEqual(design, self.s.data["design"])
        self.assertIsNot(design, self.s.data["design"])
        self.assertEqual(sorted(ap["artifact_hashes"]), sorted(approval.GUARDED_FILES))

    def test_each_markdown_document_tamper_is_detected(self):
        for name in DOC_FILES + ["dbms-choice.md"]:
            with self.subTest(file=name):
                p = self.path(name)
                original = p.read_text(encoding="utf-8")
                p.write_text(original + "\nsneaky edit\n", encoding="utf-8")
                self.assertBlocked(name)
                p.write_text(original, encoding="utf-8")
                approval.verify_approved_design(self.s)          # restored -> fine again

    def test_missing_documents_are_detected(self):
        for name in DOC_FILES + ["dbms-choice.md", "design.json"]:
            with self.subTest(file=name):
                p = self.path(name)
                original = p.read_text(encoding="utf-8")
                p.unlink()
                self.assertBlocked(f"{name} is missing" if name != "design.json" else "design.json")
                p.write_text(original, encoding="utf-8")

    def test_design_json_modified_or_corrupted(self):
        p = self.path("design.json")
        original = p.read_text(encoding="utf-8")
        data = json.loads(original)
        tbl(data, "owners")["columns"][1]["data_type"] = "TEXT"
        p.write_text(json.dumps(data), encoding="utf-8")
        self.assertBlocked("design.json was modified")
        p.write_text("{not json", encoding="utf-8")
        self.assertBlocked("corrupted")

    def test_verification_never_repairs_tampered_files(self):
        p = self.path("logical-schema.md")
        p.write_text("tampered", encoding="utf-8")
        self.assertBlocked()
        self.assertBlocked()
        self.assertEqual(p.read_text(encoding="utf-8"), "tampered")

    def test_session_design_edited_after_approval(self):
        tbl(self.s.data["design"], "owners")["columns"][1]["data_type"] = "TEXT"
        self.assertBlocked("differs from the approved")

    def test_dbms_selection_inconsistent(self):
        self.s.data["dbms_choice"]["selected_id"] = "sqlite"
        self.assertBlocked("DBMS mismatch")

    def test_pending_revision_blocks(self):
        self.s.data["revision_pending"] = {"candidate": {}, "record": {}}
        self.assertBlocked("pending")

    def test_not_approved_and_invalidated_by_revision(self):
        self.apply(add_weight, ["add_attribute"], request="add pet weight")
        self.assertBlocked("not been approved")

    def test_legacy_approval_without_hashes_must_be_reapproved(self):
        del self.s.data["approval"]["artifact_hashes"]
        self.assertBlocked("predates")
        ap, new = self.approve()                              # idempotent path re-verifies and backfills hashes
        self.assertFalse(new)
        approval.verify_approved_design(self.s)

    def test_require_approved_design_uses_the_same_checks(self):
        self.path("assumptions.md").write_text("x", encoding="utf-8")
        with self.assertRaises(ApprovalError):
            approval.require_approved_design(self.s)


class SuspiciousChangeTests(B):
    def diff_for(self, mutate):
        old = self.s.data["design"]
        import copy
        new = copy.deepcopy(old)
        mutate(new)
        return revision.diff_designs(old, new)

    def test_clean_changes_are_not_flagged(self):
        self.assertEqual(revision.suspicious_changes(["add_attribute"], self.diff_for(add_weight)), [])
        self.assertEqual(revision.suspicious_changes(["add_entity", "add_relationship"], self.diff_for(add_room)), [])
        self.assertEqual(revision.suspicious_changes(["remove_attribute"], self.diff_for(drop_phone)), [])

    def test_attribute_addition_declared_as_business_rule_is_flagged(self):
        found = revision.suspicious_changes(["add_business_rule"], self.diff_for(add_weight))
        self.assertTrue(any("attribute `pet.weight_kg`" in f for f in found))

    def test_entity_addition_inside_attribute_request_is_flagged(self):
        found = revision.suspicious_changes(["add_attribute"], self.diff_for(add_room))
        joined = " ".join(found)
        self.assertIn("entity `room`", joined)
        self.assertIn("relationship `room_appointment`", joined)

    def test_removal_inside_remove_attribute_of_other_things(self):
        def both(d):
            drop_phone(d)
            add_room(d)
        found = revision.suspicious_changes(["remove_attribute"], self.diff_for(both))
        self.assertTrue(any("entity `room`" in f for f in found))

    def test_constraint_and_rule_changes_flagged_outside_scope(self):
        def drop_check(d):
            tbl(d, "pets")["check_constraints"] = []
        found = revision.suspicious_changes(["add_entity"], self.diff_for(drop_check))
        self.assertTrue(any("check `ck_pets_species`" in f and f.startswith("removed") for f in found))

        def drop_rule(d):
            d["business_rules"] = d["business_rules"][1:]
        found = revision.suspicious_changes(["add_attribute"], self.diff_for(drop_rule))
        self.assertTrue(any("business rule" in f for f in found))

    def test_dbms_change_without_switch_type_flagged(self):
        diff = self.diff_for(lambda d: None)
        diff["dbms"] = {"from": "PostgreSQL", "to": "MySQL"}
        self.assertTrue(revision.suspicious_changes(["add_attribute"], diff))
        self.assertEqual(revision.suspicious_changes(["switch_dbms"], diff), [])

    def test_other_and_unknown_types_give_no_flags(self):
        self.assertEqual(revision.suspicious_changes(["other"], self.diff_for(add_room)), [])

    def test_record_and_preview_contain_the_warning(self):
        p = revision.propose(self.s, ScriptedLLM(self.s, lambda c: response(c, add_weight, ["add_business_rule"])),
                             self.prompts, "x")
        self.assertTrue(p.record["suspicious_changes"])
        self.assertIn("POSSIBLY OUT OF SCOPE", p.preview)
        self.assertTrue(p.needs_explicit_confirmation)

    def test_cli_yes_never_silently_accepts_suspicious(self):
        before = json.dumps(self.s.data["design"], sort_keys=True)
        llm = ScriptedLLM(self.s, lambda c: response(c, add_weight, ["add_business_rule"]))
        code, text = self.run_cli("revise", ["--request", "x", "--yes"], [], llm=llm)          # no answer -> no
        self.assertIn("Not accepted automatically (--yes)", text)
        self.assertEqual(json.dumps(self.state()["design"], sort_keys=True), before)
        llm = ScriptedLLM(self.s, lambda c: response(c, add_weight, ["add_business_rule"]))
        code, text = self.run_cli("revise", ["--request", "x", "--yes"], ["y"], llm=llm)       # explicit yes
        self.assertEqual(len(self.state()["revisions"]), 1)

    def test_cli_yes_still_accepts_ordinary_changes(self):
        llm = ScriptedLLM(self.s, lambda c: response(c, add_weight, ["add_attribute"]))
        self.run_cli("revise", ["--request", "x", "--yes"], [], llm=llm)
        self.assertEqual(len(self.state()["revisions"]), 1)


class ProposeAcceptCommitTests(B):
    def llm(self, mutate=add_weight, types=("add_attribute",)):
        return ScriptedLLM(self.s, lambda c: response(c, mutate, list(types)))

    def test_propose_writes_nothing(self):
        before_files, before_design = self.files(), json.loads(json.dumps(self.s.data["design"]))
        p = revision.propose(self.s, self.llm(), self.prompts, "add weight")
        self.assertEqual(p.status, "proposed")
        self.assertEqual(self.files(), before_files)
        self.assertEqual(self.s.data["design"], before_design)
        self.assertEqual(self.s.data.get("revisions", []), [])
        self.assertNotIn("revision_pending", self.s.data)

    def test_commit_requires_acceptance(self):
        p = revision.propose(self.s, self.llm(), self.prompts, "add weight")
        with self.assertRaises(revision.RevisionRejected):
            revision.commit_accepted(self.s, p)
        self.assertEqual(self.s.data.get("revisions", []), [])
        revision.accept(p)
        rec = revision.commit_accepted(self.s, p)
        self.assertEqual((rec["number"], p.status), (1, "committed"))
        with self.assertRaises(revision.RevisionRejected):          # cannot commit twice
            revision.commit_accepted(self.s, p)

    def test_suspicious_proposal_needs_acknowledgement(self):
        p = revision.propose(self.s, self.llm(pet_weight_but_phone_lost), self.prompts, "add weight")
        with self.assertRaises(revision.RevisionRejected):
            revision.accept(p)
        self.assertEqual(p.status, "proposed")
        revision.accept(p, acknowledge_suspicious=True)
        revision.commit_accepted(self.s, p)
        self.assertEqual(len(self.s.data["revisions"]), 1)

    def test_rejected_proposal_cannot_be_committed(self):
        p = revision.propose(self.s, self.llm(), self.prompts, "add weight")
        revision.reject(self.s, p)
        self.assertEqual(self.s.data["revision_rejections"][-1]["reason"], "declined by the user")
        with self.assertRaises(revision.RevisionRejected):
            revision.commit_accepted(self.s, p)
        self.assertEqual(self.s.data.get("revisions", []), [])

    def test_stale_proposal_is_refused(self):
        p = revision.propose(self.s, self.llm(), self.prompts, "add weight")
        self.apply(add_room, ["add_entity", "add_relationship"], request="rooms")
        revision.accept(p)
        with self.assertRaises(revision.RevisionRejected):
            revision.commit_accepted(self.s, p)
        self.assertEqual(len(self.s.data["revisions"]), 1)

    def test_unattended_revise_refuses_suspicious_by_default(self):
        before = json.loads(json.dumps(self.s.data["design"]))
        with self.assertRaises(revision.RevisionRejected):
            revision.revise(self.s, self.llm(pet_weight_but_phone_lost), self.prompts, "add weight")
        self.assertEqual(self.s.data["design"], before)
        self.assertEqual(self.s.data.get("revisions", []), [])
        self.assertNotIn("revision_pending", self.s.data)
        self.assert_consistent()

    def test_unattended_revise_with_explicit_override_and_ordinary_default(self):
        rec = revision.revise(self.s, self.llm(), self.prompts, "add weight")                # ordinary: backward compatible
        self.assertEqual(rec["number"], 1)
        rec = revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, drop_phone, ["add_attribute"])),
                              self.prompts, "x", allow_suspicious=True)
        self.assertEqual(rec["number"], 2)

    def test_confirm_callback_false_rejects_and_true_accepts(self):
        seen = []
        res = revision.revise(self.s, self.llm(), self.prompts, "w", confirm=lambda pv, rec: seen.append(pv) or False)
        self.assertIsNone(res)
        self.assertIn("Differences between", seen[0])
        self.assertEqual(self.s.data.get("revisions", []), [])
        res = revision.revise(self.s, self.llm(), self.prompts, "w", confirm=lambda pv, rec: True)
        self.assertEqual(res["number"], 1)
        self.assert_consistent()
