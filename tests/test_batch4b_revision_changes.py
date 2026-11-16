"""Batch 4B: revision change detection is computed from the old/new structured designs, never from the LLM's description.
All offline (scripted LLM)."""
import copy
import unittest

from dba_agent import revision
from dba_agent.errors import InvalidResponseError
from tests.test_stage5a_revision import Base, ScriptedLLM, add_room, add_weight, drop_phone, ent, owners_many_to_many, response, tbl


def clear_notes(d):
    d["assumptions"], d["open_questions"] = [], []


def weight_and_clear(d):
    add_weight(d)
    clear_notes(d)


class RemovedAssumptionsAndQuestions(Base):
    def propose(self, mutate, types, **kw):
        return revision.propose(self.s, ScriptedLLM(self.s, lambda cur: response(cur, mutate, types, **kw)), self.prompts, "x")

    def test_fixture_has_notes_to_remove(self):
        self.assertTrue(self.s.data["design"]["assumptions"])

    def test_diff_reports_every_removed_assumption_and_question(self):
        cur = self.s.data["design"]
        new = copy.deepcopy(cur)
        clear_notes(new)
        diff = revision.diff_designs(cur, new)
        self.assertEqual(diff["assumptions"]["removed"], sorted(a["id"] for a in cur["assumptions"]))
        self.assertEqual(diff["open_questions"]["removed"], sorted(q["id"] for q in cur["open_questions"]))
        self.assertEqual(diff["assumptions"]["added"] + diff["assumptions"]["modified"], [])
        self.assertTrue(any("assumption" in x for x in revision.unexpected_removals(["add_attribute"], diff)))

    def test_additive_revision_that_wipes_notes_is_flagged_and_needs_confirmation(self):
        cur = self.s.data["design"]
        p = self.propose(weight_and_clear, ["add_attribute"])
        rec = p.record
        self.assertEqual(len([x for x in rec["unexpected_removals"] if x.startswith("assumption")]), len(cur["assumptions"]))
        self.assertEqual(len([x for x in rec["unexpected_removals"] if x.startswith("open question")]), len(cur["open_questions"]))
        self.assertTrue(p.needs_explicit_confirmation)
        self.assertIn("CHECK CAREFULLY", p.preview)
        self.assertIn("- assumption", p.preview)
        self.assertIn("- open question", p.preview)
        self.assertEqual(len(rec["suspicious_changes"]), 0)            # reported once (as unexpected removals), not twice

    def test_unattended_revise_refuses_and_changes_nothing(self):
        before_files, before_design = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(revision.RevisionRejected):
            self.apply(weight_and_clear, ["add_attribute"])
        self.assert_unchanged(before_files, before_design)
        self.assertEqual(len(self.s.data.get("revisions", [])), 0)

    def test_explicit_confirmation_commits_and_changelog_lists_removals(self):
        n = len(self.s.data["design"]["assumptions"])
        rec = revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, weight_and_clear, ["add_attribute"])),
                              self.prompts, "x", allow_suspicious=True)
        self.assertEqual(len(rec["affected"]["assumptions"]["removed"]), n)
        self.assertEqual(self.s.data["design"]["assumptions"], [])
        self.assertIn("Assumptions: removed", (self.s.output_dir / "changelog.md").read_text("utf-8"))
        self.assert_consistent()

    def test_rejected_proposal_leaves_files_and_state_untouched(self):
        before_files, before_design = self.files(), copy.deepcopy(self.s.data["design"])
        before_hist = copy.deepcopy(self.s.data.get("revisions", []))
        got = revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, weight_and_clear, ["add_attribute"])),
                              self.prompts, "x", confirm=lambda preview, record: False)
        self.assertIsNone(got)
        self.assert_unchanged(before_files, before_design)
        self.assertEqual(self.s.data.get("revisions", []), before_hist)
        self.assertNotIn("revision_pending", self.s.data)

    def test_removal_explained_by_the_same_revision_is_not_flagged(self):
        self.s.data["design"]["assumptions"][0]["text"] += " Owner phone numbers are optional."     # note about `phone`
        def drop_phone_and_its_note(d):
            drop_phone(d)
            d["assumptions"] = d["assumptions"][1:]
        p = self.propose(drop_phone_and_its_note, ["remove_attribute"])
        self.assertEqual((p.record["unexpected_removals"], p.record["unrelated_removals"], p.record["suspicious_changes"]),
                         ([], [], []))
        self.assertEqual(len(p.record["affected"]["assumptions"]["removed"]), 1)
        self.assertEqual(p.record["structural_removals"], ["attribute `owner.phone`", "column `owners.phone`"])   # still a deletion
        self.assertTrue(p.needs_explicit_confirmation)
        self.assertIn("- assumption", p.preview)

    def test_remove_attribute_label_does_not_authorize_unrelated_note_deletion(self):
        def drop_phone_and_wipe(d):
            drop_phone(d)
            clear_notes(d)
        p = self.propose(drop_phone_and_wipe, ["remove_attribute"])
        self.assertTrue(p.needs_explicit_confirmation)
        self.assertTrue(p.record["unrelated_removals"])
        self.assertIn("POSSIBLY DESTRUCTIVE", p.preview)

    def test_other_label_does_not_authorize_mass_deletion_but_confirmation_does(self):
        p = self.propose(clear_notes, ["other"])
        self.assertTrue(p.needs_explicit_confirmation)
        self.assertEqual(len(p.record["unrelated_removals"]),
                         len(self.s.data["design"]["assumptions"]) + len(self.s.data["design"]["open_questions"]))
        before_files, before_design = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(revision.RevisionRejected):                       # unattended: refused, nothing written
            self.apply(clear_notes, ["other"])
        self.assert_unchanged(before_files, before_design)
        rec = revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, clear_notes, ["other"])),
                              self.prompts, "x", allow_suspicious=True)          # intentional removal stays possible
        self.assertEqual(self.s.data["design"]["assumptions"], [])
        self.assertTrue(rec["affected"]["assumptions"]["removed"])

    def test_cli_yes_does_not_auto_accept_unrelated_removal(self):
        from dba_agent import cli
        lines = []
        confirm = cli.make_confirm(lambda prompt="": "n", lines.append, auto=True)
        p = self.propose(clear_notes, ["other"])
        self.assertFalse(confirm(p.preview, p.record))                           # asked (and answered no), not auto-yes


class FunctionalDependencyAndPhysicalMetadata(Base):
    def propose(self, mutate, types):
        return revision.propose(self.s, ScriptedLLM(self.s, lambda cur: response(cur, mutate, types)), self.prompts, "x")

    @staticmethod
    def shrink_owner_fd(d):
        for f in d["physical"]["functional_dependencies"]:
            if f["table"] == "owners" and f["determinant"] == ["owner_id"]:
                f["dependent"].remove("full_name")

    def test_defect_fd_dependent_removed_in_valid_add_attribute_revision(self):
        def both(d):
            add_weight(d)
            self.shrink_owner_fd(d)
        p = self.propose(both, ["add_attribute"])
        det = p.record["affected"]["fd_detail"]
        self.assertEqual(list(det.values())[0]["lost"], ["full_name"])
        self.assertIn("~ functional dependency owners: (owner_id)", p.preview)
        self.assertIn("- dependent full_name", p.preview)
        self.assertTrue(p.needs_explicit_confirmation)
        self.assertTrue(any("loses dependent(s) full_name" in x for x in p.record["unexpected_removals"]))
        with self.assertRaises(revision.RevisionRejected):
            revision.accept(p)

    def test_fd_growth_alone_is_an_ordinary_additive_change(self):
        p = self.propose(add_weight, ["add_attribute"])
        self.assertEqual(p.record["affected"]["functional_dependencies"]["modified"], ["pets: (pet_id)"])
        self.assertIn("+ dependent weight_kg", p.preview)
        self.assertFalse(p.needs_explicit_confirmation)

    def test_removed_and_added_fds_are_keyed_without_ids(self):
        cur = self.s.data["design"]
        new = copy.deepcopy(cur)
        fds = new["physical"]["functional_dependencies"]
        gone = fds.pop(1)                                                          # owners: (email) -> ...
        fds.append({"table": "pets", "determinant": ["name", "birth_date"], "dependent": ["species"], "note": ""})
        fds.append(copy.deepcopy(fds[0]))                                          # duplicate key: compared separately
        diff = revision.diff_designs(cur, new)
        self.assertEqual(diff["functional_dependencies"]["removed"], [f"{gone['table']}: (email)"])
        self.assertIn("pets: (birth_date, name)", diff["functional_dependencies"]["added"])
        self.assertTrue(any(k.endswith("#2") for k in diff["functional_dependencies"]["added"]))
        self.assertEqual(diff, revision.diff_designs(cur, new))                    # deterministic

    def test_fd_removal_explained_by_removed_column_is_not_destructive(self):
        cur = self.s.data["design"]
        new = copy.deepcopy(cur)
        drop_phone(new)
        diff = revision.diff_designs(cur, new)
        self.assertEqual(revision.unrelated_removals(cur, new, diff), [])
        self.shrink_owner_fd(new)                                                  # now also an unrelated loss
        self.assertEqual(len(revision.unrelated_removals(cur, new, revision.diff_designs(cur, new))), 1)

    def test_denormalization_and_table_metadata_changes(self):
        cur = self.s.data["design"]
        a, b = copy.deepcopy(cur), copy.deepcopy(cur)
        a["physical"]["denormalizations"] = [{"table": "pets", "description": "d", "justification": "j"}]
        b["physical"]["denormalizations"] = [{"table": "pets", "description": "d", "justification": "other"}]
        self.assertEqual(revision.diff_designs(a, b)["denormalizations"]["modified"], ["pets: d"])
        self.assertEqual(revision.diff_designs(a, cur)["denormalizations"]["removed"], ["pets: d"])
        c = copy.deepcopy(cur)
        tbl(c, "pets")["description"] = "changed"
        tbl(c, "pets")["source"] = {"type": "entity", "id": "owner"}
        self.assertEqual(revision.diff_designs(cur, c)["table_fields"], {"pets": ["description", "source"]})
        lines = "\n".join(revision.detail_lines(cur, c, revision.diff_designs(cur, c)))
        self.assertIn("~ table pets: description, source changed", lines)
        self.assertTrue(revision.suspicious_changes(["add_attribute"], revision.diff_designs(cur, c)))

    def test_changelog_lists_fd_changes_and_old_records_still_render(self):
        rec = revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, lambda d: (add_weight(d), self.shrink_owner_fd(d)),
                                                                             ["add_attribute"])),
                              self.prompts, "x", allow_suspicious=True)
        self.assertIn("Functional dependencies: modified", (self.s.output_dir / "changelog.md").read_text("utf-8"))
        for k in ("functional_dependencies", "fd_detail", "denormalizations", "table_fields"):
            rec["affected"].pop(k, None)
        self.assertIn("## Revision 1", revision.render_changelog([rec]))


def add_color(d):
    ent(d, "pet")["attributes"].append({"name": "color", "type": "string", "required": False, "unique": False, "description": "coat color"})
    from tests.design_fixtures import _c
    tbl(d, "pets")["columns"].append(_c("color", "VARCHAR(30)", True))
    for f in d["physical"]["functional_dependencies"]:
        if f["table"] == "pets" and f["determinant"] == ["pet_id"]:
            f["dependent"].append("color")


def color_and_drop_phone(d):
    add_color(d)
    drop_phone(d)


class StructuralDeletionNeedsConfirmation(Base):
    def propose(self, mutate, types):
        return revision.propose(self.s, ScriptedLLM(self.s, lambda cur: response(cur, mutate, types)), self.prompts, "Add a new color field.")

    def test_reproduction_other_label_cannot_hide_a_deleted_attribute(self):
        for types in (["other"], ["remove_attribute"], ["add_attribute", "other"]):
            p = self.propose(color_and_drop_phone, types)
            self.assertEqual(p.record["structural_removals"], ["attribute `owner.phone`", "column `owners.phone`"], types)
            self.assertTrue(p.needs_explicit_confirmation, types)
            self.assertIn("DELETES STRUCTURE", p.preview)
            self.setUp()                                                  # fresh session for the next label

    def test_unattended_refuses_and_changes_nothing(self):
        before_files, before_design = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(revision.RevisionRejected):
            self.apply(color_and_drop_phone, ["other"])
        self.assert_unchanged(before_files, before_design)

    def test_cli_yes_never_silently_accepts_structural_deletion(self):
        from dba_agent import cli
        asked, lines = [], []
        def answer(prompt=""):
            asked.append(prompt)
            return "n"
        confirm = cli.make_confirm(answer, lines.append, auto=True)
        p = self.propose(color_and_drop_phone, ["other"])
        self.assertFalse(confirm(p.preview, p.record))
        self.assertTrue(asked, "--yes must still ask about a deletion")
        self.assertTrue(cli.make_confirm(lambda prompt="": "y", lines.append, auto=True)(p.preview, p.record))   # explicit yes

    def test_cli_yes_still_auto_accepts_ordinary_additions(self):
        from dba_agent import cli
        def never(prompt=""):
            raise AssertionError("an ordinary addition must not be questioned under --yes")
        confirm = cli.make_confirm(never, lambda *_: None, auto=True)
        for mutate, types in ((add_weight, ["add_attribute"]), (add_color, ["other"]), (add_room, ["add_entity", "add_relationship"])):
            p = self.propose(mutate, types)
            self.assertEqual(p.record["structural_removals"], [])
            self.assertFalse(p.needs_explicit_confirmation)
            self.assertTrue(confirm(p.preview, p.record))
            self.setUp()

    def test_explicitly_confirmed_removal_is_committed(self):
        rec = revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, color_and_drop_phone, ["other"])), self.prompts,
                              "x", confirm=lambda preview, record: "DELETES STRUCTURE" in preview)
        self.assertEqual(rec["affected"]["attributes"]["owner"]["removed"], ["phone"])
        self.assertIn("color", [c["name"] for c in tbl(self.s.data["design"], "pets")["columns"]])
        self.assertNotIn("phone", [c["name"] for c in tbl(self.s.data["design"], "owners")["columns"]])
        self.assert_consistent()

    def test_declined_removal_leaves_everything_untouched(self):
        before_files, before_design = self.files(), copy.deepcopy(self.s.data["design"])
        self.assertIsNone(revision.revise(self.s, ScriptedLLM(self.s, lambda c: response(c, color_and_drop_phone, ["other"])),
                                          self.prompts, "x", confirm=lambda preview, record: False))
        self.assert_unchanged(before_files, before_design)

    def test_other_structure_kinds_are_detected(self):
        cur = self.s.data["design"]
        new = copy.deepcopy(cur)
        owners_many_to_many(new)
        diff = revision.diff_designs(cur, new)
        found = revision.structural_removals(diff)
        self.assertIn("column `pets.owner_id`", found)
        self.assertTrue(any(x.startswith("foreign key `fk_pets_owners`") for x in found))
        self.assertTrue(any(x.startswith("index `ix_pets_owner`") for x in found))
        gone = copy.deepcopy(cur)
        gone["physical"]["tables"] = [t for t in gone["physical"]["tables"] if t["name"] != "pets"]
        gone["conceptual"]["entities"] = [e for e in gone["conceptual"]["entities"] if e["id"] != "pet"]
        found = revision.structural_removals(revision.diff_designs(cur, gone))
        self.assertIn("entity `pet`", found)
        self.assertIn("table `pets`", found)
        self.assertFalse(any("on `pets`" in x for x in found))          # a dropped table's own constraints are not repeated

    def test_no_duplicate_reports_when_already_flagged(self):
        def add_and_drop(d):
            drop_phone(d)
            add_weight(d)
        p = self.propose(add_and_drop, ["add_attribute"])                # add-only label: reported as unexpected removals
        self.assertTrue(p.record["unexpected_removals"])
        self.assertEqual(p.record["structural_removals"], [])
        self.assertEqual(len(p.suspicious), len(set(p.suspicious)))
        self.assertEqual(sum("column `owners.phone`" in x for x in p.suspicious), 1)

        def drop_check(d):                                               # out-of-scope constraint removal: reported once
            add_weight(d)
            tbl(d, "pets")["check_constraints"] = [c for c in tbl(d, "pets")["check_constraints"] if c["name"] == "ck_pets_weight"]
        q = self.propose(drop_check, ["add_entity"])
        self.assertEqual(q.record["structural_removals"], [])
        self.assertEqual(len(q.suspicious), len(set(q.suspicious)))


class ValidAdditionsAndOrdinaryEdits(Base):
    def propose(self, mutate, types):
        return revision.propose(self.s, ScriptedLLM(self.s, lambda cur: response(cur, mutate, types)), self.prompts, "x")

    def test_additive_revision_with_new_notes_is_clean(self):
        def mutate(d):
            add_weight(d)
            d["assumptions"].append({"id": "A_NEW", "text": "Weights are in kg.", "rationale": "", "related_to": []})
            d["open_questions"].append({"id": "Q_NEW", "text": "Is weight mandatory?", "impact": "NOT NULL"})
        p = self.propose(mutate, ["add_attribute"])
        self.assertEqual((p.record["unexpected_removals"], p.record["suspicious_changes"]), ([], []))
        self.assertFalse(p.needs_explicit_confirmation)
        self.assertEqual(p.record["affected"]["assumptions"]["added"], ["A_NEW"])
        self.assertEqual(p.record["affected"]["open_questions"]["added"], ["Q_NEW"])
        self.assertIn("+ assumption A_NEW", p.preview)

    def test_rewording_notes_is_an_ordinary_edit(self):
        def mutate(d):
            add_weight(d)
            d["assumptions"][0]["text"] += " (clarified)"
            d["open_questions"][0]["impact"] = "changed impact"
        p = self.propose(mutate, ["add_attribute"])
        self.assertFalse(p.needs_explicit_confirmation)
        self.assertEqual(len(p.record["affected"]["assumptions"]["modified"]), 1)
        self.assertEqual(len(p.record["affected"]["open_questions"]["modified"]), 1)
        self.assertIn("~ assumption", p.preview)

    def test_entity_level_change_without_attribute_change_is_detected(self):
        def mutate(d):
            add_weight(d)
            ent(d, "owner")["description"] += " Edited."
        p = self.propose(mutate, ["add_attribute"])
        self.assertEqual(p.record["affected"]["entity_fields"], {"owner": ["description"]})
        self.assertIn("~ entity owner: description changed", p.preview)
        self.assertEqual(p.record["suspicious_changes"], [])

    def test_preview_and_diff_are_deterministic(self):
        mut = lambda d: (add_weight(d), clear_notes(d))
        a = self.propose(mut, ["add_attribute"])
        b = self.propose(mut, ["add_attribute"])
        self.assertEqual(a.preview, b.preview)
        self.assertEqual(a.record["affected"], b.record["affected"])


class NestedConstraintChanges(Base):
    def diff(self, mutate):
        cur = self.s.data["design"]
        new = copy.deepcopy(cur)
        mutate(new)
        return revision.diff_designs(cur, new), cur, new

    def test_changes_nested_in_constraints_are_modifications(self):
        def fk_action(d):
            fk = tbl(d, "pets")["foreign_keys"][0]
            fk["on_delete"] = "CASCADE" if fk["on_delete"] != "CASCADE" else "RESTRICT"

        def check_text(d):
            tbl(d, "appointments")["check_constraints"][0]["description"] = "reworded"

        def check_expr(d):
            tbl(d, "appointments")["check_constraints"][0]["expression"] += " AND 1 = 1"

        def cond_index(d):
            ix = tbl(d, "pets")["indexes"][0] if tbl(d, "pets")["indexes"] else None
            if ix is None:
                ix = {"name": "ix_pets_x", "columns": ["name"], "unique": True, "purpose": ""}
                tbl(d, "pets")["indexes"].append(ix)
            ix["where"] = [{"column": "name", "operator": "IS NOT NULL"}]
            ix["conditional"] = True

        def trigger(d):
            tbl(d, "pets").setdefault("triggers", []).append({"name": "trg_x", "kind": "x", "operations": ["INSERT"]})

        for fn in (fk_action, check_text, check_expr):
            diff, cur, _ = self.diff(fn)
            self.assertEqual(len(diff["constraints"]["modified"]), 1, fn.__name__)
            self.assertEqual(diff["constraints"]["added"] + diff["constraints"]["removed"], [])
        diff, cur, _ = self.diff(cond_index)
        self.assertTrue(diff["constraints"]["modified"] or diff["constraints"]["added"])
        diff, _, _ = self.diff(trigger)
        self.assertEqual(diff["constraints"]["added"], ["trg_x"])
        diff, _, _ = self.diff(lambda d: tbl(d, "pets").__setitem__("triggers", [{"name": "trg_x", "kind": "x", "operations": ["INSERT"]}]))
        self.assertEqual(diff["constraint_info"]["trg_x"]["kind"], "trigger")

    def test_trigger_body_change_is_detected(self):
        cur = self.s.data["design"]
        a, b = copy.deepcopy(cur), copy.deepcopy(cur)
        for d, body in ((a, "RAISE(ABORT, 'a')"), (b, "RAISE(ABORT, 'b')")):
            tbl(d, "pets")["triggers"] = [{"name": "trg_x", "kind": "guard", "body": body}]
        self.assertEqual(revision.diff_designs(a, b)["constraints"]["modified"], ["trg_x"])

    def test_nested_constraint_removal_in_additive_revision_is_flagged(self):
        def drop_check(d):
            add_weight(d)
            tbl(d, "pets")["check_constraints"] = [c for c in tbl(d, "pets")["check_constraints"] if c["name"] == "ck_pets_weight"]
        p = revision.propose(self.s, ScriptedLLM(self.s, lambda c: response(c, drop_check, ["add_attribute"])), self.prompts, "x")
        self.assertTrue(p.needs_explicit_confirmation)
        self.assertTrue(any("check" in x for x in p.record["suspicious_changes"]))


class NoOp(Base):
    def test_identical_design_is_rejected_and_nothing_changes(self):
        before_files, before_design = self.files(), copy.deepcopy(self.s.data["design"])
        with self.assertRaises(InvalidResponseError):
            self.apply(lambda d: None, ["add_attribute"])
        self.assert_unchanged(before_files, before_design)

    def test_identical_designs_have_an_empty_diff(self):
        cur = self.s.data["design"]
        diff = revision.diff_designs(cur, copy.deepcopy(cur))
        for k in ("entities", "relationships", "tables", "constraints", "business_rules", "assumptions", "open_questions"):
            self.assertEqual(diff[k], {"added": [], "removed": [], "modified": []}, k)
        self.assertEqual(diff["entity_fields"], {})
        self.assertEqual(revision.detail_lines(cur, cur, diff), [])


class SavedSessionsStillWork(Base):
    def test_old_records_without_new_keys_still_render(self):
        rec = self.apply(add_weight, ["add_attribute"])
        for k in ("assumptions", "open_questions", "entity_fields"):
            rec["affected"].pop(k, None)
        text = revision.render_changelog([rec])
        self.assertIn("## Revision 1", text)
        self.assertNotIn("Open questions:", text)


if __name__ == "__main__":
    unittest.main()
