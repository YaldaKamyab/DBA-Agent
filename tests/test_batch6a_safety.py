"""Batch 6A: (1) an AI revision cannot silently weaken constraints; (2) approval policy for known 3NF violations."""
import copy
import unittest

from dba_agent import approval, revision
from dba_agent.errors import DBAAgentError
from tests.test_stage5a_revision import add_weight, ent, response, tbl, ScriptedLLM
from tests.test_stage5b_safety import B, unjustified_denormalization


def _find(d, kind):
    for t in d["physical"]["tables"]:
        for c in t[kind]:
            yield t, c


def add_unique_index(d):
    tbl(d, "owners")["indexes"].append({"name": "ux_owners_phone", "columns": ["phone"], "unique": True, "purpose": "one owner per phone"})


class WeakeningDetection(B):
    def setUp(self):
        super().setUp()
        self.apply(add_unique_index, ["change_constraint"])        # accepted revision 1: a UNIQUE index exists now

    def propose(self, mutate, types=("other",), **kw):
        return revision.propose(self.s, ScriptedLLM(self.s, lambda cur: response(cur, mutate, list(types), **kw)),
                                self.prompts, "relax it")

    def cur(self):
        return copy.deepcopy(self.s.data["design"])

    def weak(self, mutate):
        old = self.cur()
        new = copy.deepcopy(old)
        mutate(new)
        return revision.weakened_protections(old, new)

    def test_unique_true_to_false_declared_other_is_flagged_and_needs_confirmation(self):
        def m(d):
            t, i = next((t, i) for t, i in _find(d, "indexes") if i.get("unique"))
            i["unique"] = False
        p = self.propose(m, ["other"])
        self.assertTrue(any("unique: true -> false" in x for x in p.record["weakened_protections"]))
        self.assertIn("WEAKENS DATABASE PROTECTIONS", p.preview)
        self.assertTrue(p.needs_explicit_confirmation)
        with self.assertRaises(revision.RevisionRejected):
            revision.accept(p)

    def test_unattended_and_yes_refuse_but_explicit_confirmation_commits(self):
        def m(d):
            next(i for _, i in _find(d, "indexes") if i.get("unique"))["unique"] = False
        before = copy.deepcopy(self.s.data["design"])
        with self.assertRaises(revision.RevisionRejected):
            self.rev(lambda cur: response(cur, m, ["other"]))
        self.assertEqual(self.s.data["design"], before)
        # CLI --yes: never accepted automatically, EOF means no
        code, text = self.run_cli("revise", ["--request", "relax", "--yes"], [],
                                  ScriptedLLM(self.s, lambda cur: response(cur, m, ["other"])))
        self.assertIn("Not accepted automatically", text)
        self.assertEqual(self.state()["design"], before)
        # explicit "y" -> the legitimate change is preserved
        code, text = self.run_cli("revise", ["--request", "relax", "--yes"], ["y"],
                                  ScriptedLLM(self.s, lambda cur: response(cur, m, ["other"])))
        self.assertIn("Revision 2 accepted", text)
        self.assertNotEqual(self.state()["design"], before)
        self.assertTrue(self.state()["revisions"][1]["weakened_protections"])

    def test_non_destructive_revision_is_not_flagged(self):
        p = self.propose(add_weight, ["add_attribute"])
        self.assertEqual(p.record["weakened_protections"], [])
        self.assertFalse(p.needs_explicit_confirmation)
        code, text = self.run_cli("revise", ["--request", "weight", "--yes"], [],
                                  ScriptedLLM(self.s, lambda cur: response(cur, add_weight, ["add_attribute"])))
        self.assertIn("Revision 2 accepted", text)

    def test_adding_or_strengthening_is_not_weakening(self):
        def stronger(d):
            t = tbl(d, "owners")
            t["unique_constraints"].append({"name": "uq_owners_phone_x", "columns": ["phone"]})
            t["check_constraints"].append({"name": "ck_owners_x", "expression": "phone <> ''", "description": ""})
        self.assertEqual(self.weak(stronger), [])

    def test_unique_constraint_removed_and_renamed(self):
        t, u = next(((t, u) for t, u in _find(self.cur(), "unique_constraints")), (None, None))
        if u is None:
            self.skipTest("fixture has no UNIQUE constraint")
        def rm(d):
            tbl(d, t["name"])["unique_constraints"] = []
        self.assertTrue(any("unique constraint" in x for x in self.weak(rm)))
        def rename(d):
            for x in tbl(d, t["name"])["unique_constraints"]:
                x["name"] = x["name"] + "_renamed"
        self.assertEqual(self.weak(rename), [])

    def test_unique_index_narrowed_by_condition_is_weakened_but_subset_key_is_not(self):
        old = self.cur()
        new = copy.deepcopy(old)
        idx = lambda d: next(i for i in tbl(d, "owners")["indexes"] if i["name"] == "ux_owners_phone")
        idx(new)["where"] = [{"column": "email", "operator": "is_not_null"}]
        idx(new)["conditional"] = True
        self.assertTrue(revision.weakened_protections(old, new))
        new2 = copy.deepcopy(old)                          # unique on (phone) -> unique on (phone, email) is weaker too
        idx(new2)["columns"] = ["phone", "email"]
        self.assertTrue(revision.weakened_protections(old, new2))

    def test_primary_key_weakened(self):
        def m(d):
            t = tbl(d, "owners")
            t["primary_key"] = []
        self.assertTrue(any("primary key" in x for x in self.weak(m)))

    def test_foreign_key_removed_or_less_protective(self):
        t, fk = next(_find(self.cur(), "foreign_keys"))
        def rm(d):
            tbl(d, t["name"])["foreign_keys"] = [x for x in tbl(d, t["name"])["foreign_keys"] if x["name"] != fk["name"]]
        self.assertTrue(any("foreign key" in x for x in self.weak(rm)))
        def act(d):
            for x in tbl(d, t["name"])["foreign_keys"]:
                if x["name"] == fk["name"]:
                    x["on_delete"] = "CASCADE"
                    x["on_update"] = "CASCADE"
        old = self.cur()
        for x in tbl(old, t["name"])["foreign_keys"]:
            x["on_delete"] = "RESTRICT"
        new = copy.deepcopy(old)
        for x in tbl(new, t["name"])["foreign_keys"]:
            x["on_delete"] = "SET NULL"
        self.assertTrue(any("ON DELETE" in x for x in revision.weakened_protections(old, new)))
        stricter = copy.deepcopy(new)
        self.assertEqual(revision.weakened_protections(new, stricter), [])
        self.assertEqual(revision.weakened_protections(new, old), [])          # SET NULL -> RESTRICT is stronger

    def test_check_removed_and_trigger_weakened(self):
        old = self.cur()
        tbl(old, "pets")["check_constraints"].append({"name": "ck_p_age", "expression": "pet_id > 0", "description": ""})
        tbl(old, "pets")["triggers"] = [{"name": "tg_pets_append", "kind": "append_only", "description": "", "operations": ["UPDATE", "DELETE"]}]
        new = copy.deepcopy(old)
        tbl(new, "pets")["check_constraints"] = [c for c in tbl(new, "pets")["check_constraints"] if c["name"] != "ck_p_age"]
        tbl(new, "pets")["triggers"][0]["operations"] = ["UPDATE"]
        found = revision.weakened_protections(old, new)
        self.assertTrue(any("check constraint" in x for x in found))
        self.assertTrue(any("trigger" in x and "DELETE" in x for x in found))
        gone = copy.deepcopy(old)
        tbl(gone, "pets")["triggers"] = []
        self.assertTrue(any("trigger" in x for x in revision.weakened_protections(old, gone)))

    def test_conceptual_unique_flag_weakened(self):
        def m(d):
            a = next(a for e in d["conceptual"]["entities"] for a in e["attributes"] if a.get("unique"))
            a["unique"] = False
        self.assertTrue(any("no longer unique" in x for x in self.weak(m)))

    def test_restore_also_requires_explicit_confirmation(self):
        from dba_agent import cli
        out = []
        confirm = cli.make_confirm(lambda: "", out.append, auto=True)
        rec = {"restored_from": 0, "weakened_protections": ["x"], "structural_removals": []}
        self.assertFalse(confirm("preview", rec))                      # --yes, EOF-like empty answer -> no
        rec["weakened_protections"] = []
        self.assertTrue(confirm("preview", rec))


class ThreeNfPolicy(B):
    def test_proven_3nf_normal_approval(self):
        res = approval.check_approvable(self.s)
        self.assertEqual((res["problems"], res["acknowledgements_required"], res["blocked_normalization"]), ([], [], []))
        ap, new = self.approve()
        self.assertTrue(new)
        self.assertTrue(ap["normalization_review"]["proven_3nf_tables"])

    def test_known_violation_cannot_be_approved_by_any_acknowledgement(self):
        self.apply(unjustified_denormalization, ["add_attribute"])
        r = approval.normalization_readiness(self.s.data["design"])
        self.assertTrue(r["known_violations"])
        self.assertNotIn("appointments", [i["table"] for i in r["proven_3nf"]])
        self.assertIn("does NOT satisfy 3NF", r["known_violations"][0]["message"])
        for kw in ({}, {"acknowledge_normalization": True}):
            with self.assertRaises(approval.ApprovalError):
                approval.approve(self.s, **kw)
        code, text = self.run_cli("approve", ["--yes", "--acknowledge-normalization"])
        self.assertEqual(code, 2)
        self.assertNotIn("approval", self.state())

    def test_trivial_justification_is_not_an_exception(self):
        def trivial(d):
            unjustified_denormalization(d)
            d["physical"]["denormalizations"].append({"table": "appointments", "description": "x", "justification": "speed"})
        self.apply(trivial, ["add_attribute"])
        self.assertTrue(approval.normalization_readiness(self.s.data["design"])["known_violations"])
        with self.assertRaises(approval.ApprovalError):
            approval.approve(self.s, acknowledge_normalization=True)

    def test_justified_exception_cli_flow(self):
        def justified(d):
            unjustified_denormalization(d)
            d["physical"]["denormalizations"].append(
                {"table": "appointments", "description": "pet name copy", "justification": "read-heavy reporting; reviewed with the owner"})
        self.apply(justified, ["add_attribute"])
        code, text = self.run_cli("approve", ["--yes"])                 # --yes alone is not the approval of the exception
        self.assertEqual(code, 2)
        self.assertNotIn("approval", self.state())
        code, text = self.run_cli("approve", [], [approval.ACK_PHRASE])
        self.assertEqual(code, 0, text)
        rev = self.state()["approval"]["normalization_review"]
        self.assertEqual(rev["denormalization_exceptions_approved"][0]["table"], "appointments")
        self.assertNotIn("appointments", rev["proven_3nf_tables"])

    def test_unverified_still_needs_acknowledgement_only(self):
        from unittest import mock
        from dba_agent import normalization_analysis as na
        with mock.patch.object(na, "MAX_KEY_SEARCH_STEPS", 0):
            self.apply(add_weight, ["add_attribute"])               # documents are regenerated under the stricter analysis
            res = approval.check_approvable(self.s)
            self.assertEqual({i["kind"] for i in res["acknowledgements_required"]}, {"unverified_3nf"})
            self.assertEqual(res["blocked_normalization"], [])
            ap, _ = approval.approve(self.s, acknowledge_normalization=True)
        self.assertTrue(ap["normalization_review"]["unverified_3nf_acknowledged"])


if __name__ == "__main__":
    unittest.main()
