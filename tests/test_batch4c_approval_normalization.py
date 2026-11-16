"""Batch 4C: approval/readiness and normalization-status accuracy.

Unknown candidate-key completeness must never read as proven 3NF; known violations, unverified verdicts, documented
exceptions, unresolved requirements and unsupported enforcement are reported separately; newly approving a design with a
known unjustified violation / unverified 3NF needs explicit acknowledgement; historical approvals are not re-gated."""
import copy
import unittest
from unittest import mock

from dba_agent import approval, design_docs as dd, normalization_analysis as na
from dba_agent.normalization_analysis import analyze_table, form_check
from tests.test_normalization_3nf import fd, forms, table
from tests.test_stage5a_revision import add_weight, tbl
from tests.test_stage5b_safety import B, unjustified_denormalization

PAIRS = 6   # x_i <-> y_i for 6 pairs = 2**6 = 64 candidate keys, above the enumeration bound


def cutoff_table():
    cols = [f"{p}{i}" for i in range(PAIRS) for p in "xy"] + ["z", "w"]
    t = table(cols, [f"x{i}" for i in range(PAIRS)])
    fds = ([fd([f"x{i}"], [f"y{i}"]) for i in range(PAIRS)] + [fd([f"y{i}"], [f"x{i}"]) for i in range(PAIRS)]
           + [fd([f"x{i}" for i in range(PAIRS)], ["z", "w"]), fd(["z"], ["w"])])
    return t, fds


class AnalyzerVerdicts(unittest.TestCase):
    def test_bounded_enumeration_is_never_proven_3nf(self):
        t, fds = cutoff_table()
        r = analyze_table(t, fds, [])
        self.assertFalse(r["keys_complete"])
        self.assertEqual(r["status"], "UNVERIFIED")
        self.assertEqual(r["nf3_status"], "unverified")
        self.assertFalse(r["formal_3nf"])                         # the bug: this used to be True
        self.assertFalse(r["unjustified"])                        # unverified is not a known violation
        self.assertEqual(r["highest"], "UNVERIFIED")              # not "BCNF"
        for form in ("2NF", "3NF"):
            self.assertEqual(form_check(r, form), "UNVERIFIED")
        self.assertTrue(r["nf3_unresolved"])

    def test_bounded_enumeration_without_any_dependency_still_unverified(self):
        cols = [f"{p}{i}" for i in range(PAIRS) for p in "xy"]
        t = table(cols, [f"x{i}" for i in range(PAIRS)])
        fds = [fd([f"x{i}"], [f"y{i}"]) for i in range(PAIRS)] + [fd([f"y{i}"], [f"x{i}"]) for i in range(PAIRS)]
        r = analyze_table(t, fds, [])
        self.assertFalse(r["keys_complete"])
        self.assertFalse(r["formal_3nf"])
        self.assertEqual(r["status"], "UNVERIFIED")

    def test_declared_denormalization_does_not_hide_unverified_3nf(self):
        t, fds = cutoff_table()
        r = analyze_table(t, fds, [{"table": "t", "description": "d", "justification": "j"}])
        self.assertEqual(r["nf3_status"], "unverified")
        self.assertIn("unverified", r["status"])

    def test_known_3nf_violation(self):
        t = table("abc", "a")
        r = analyze_table(t, [fd("a", "bc"), fd("b", "c")], [])
        self.assertEqual((r["nf3_status"], r["formal_3nf"], r["unjustified"]), ("violated", False, True))
        self.assertEqual(r["status"], "VIOLATION, not justified")
        self.assertEqual(form_check(r, "3NF"), "VIOLATED")

    def test_justified_violation_is_a_documented_exception(self):
        t = table("abc", "a")
        r = analyze_table(t, [fd("a", "bc"), fd("b", "c")], [{"table": "t", "description": "d", "justification": "j"}])
        self.assertEqual((r["nf3_status"], r["unjustified"]), ("violated", False))
        self.assertEqual(r["status"], "violation, justified denormalization")

    def test_uncertain_3nf_from_conditional_key(self):
        t = table("abc", "a", uniques=["b"], nullable="b")
        r = analyze_table(t, [fd("a", "bc"), fd("b", "ac")], [])
        self.assertIn("UNCERTAIN", forms(r))
        self.assertEqual((r["nf3_status"], r["formal_3nf"]), ("unverified", False))
        self.assertEqual(r["status"], "UNVERIFIED")

    def test_atomicity_hint_alone_does_not_change_the_3nf_verdict(self):
        t = table("abc", "a")
        t["columns"][1]["data_type"] = "JSON"
        r = analyze_table(t, [fd("a", "bc")], [])
        self.assertEqual((r["nf3_status"], r["formal_3nf"]), ("proven", True))
        self.assertEqual(r["status"], "UNVERIFIED")               # 1NF is still reported as unconfirmed

    def test_valid_3nf_is_proven(self):
        t = table("abc", "a")
        r = analyze_table(t, [fd("a", "bc")], [])
        self.assertEqual((r["nf3_status"], r["formal_3nf"], r["status"], r["highest"]), ("proven", True, "OK", "BCNF"))
        self.assertEqual(form_check(r, "3NF"), "satisfied")
        self.assertEqual(r["nf3_unresolved"], [])


class ApprovalAcknowledgement(B):
    def test_proven_design_needs_no_acknowledgement_and_states_limits(self):
        res = approval.check_approvable(self.s)
        self.assertEqual((res["problems"], res["acknowledgements_required"]), ([], []))
        self.assertTrue(res["readiness"]["proven_3nf"])
        self.assertTrue(any("MECHANICAL" in x for x in res["limitations"]))
        ap, new = self.approve()
        self.assertTrue(new)
        rev = ap["normalization_review"]
        self.assertTrue(rev["mechanical_review_only"])
        self.assertFalse(rev["acknowledged_by_user"])

    def test_known_violation_is_never_approvable_by_acknowledgement(self):      # Batch 6A policy
        self.apply(unjustified_denormalization, ["add_attribute"])
        res = approval.check_approvable(self.s)
        self.assertEqual([i["kind"] for i in res["blocked_normalization"]], ["known_violation"])
        self.assertEqual(res["acknowledgements_required"], [])
        self.assertEqual(res["structural_problems"], [])
        self.assertTrue(res["problems"])
        for ack in (False, True):
            with self.assertRaises(approval.ApprovalError):
                approval.approve(self.s, acknowledge_normalization=ack)
        self.assertNotIn("approval", self.s.data)
        self.assertTrue(approval.check_approvable(self.s, acknowledged=True)["problems"])   # still blocked
        self.assertNotEqual(self.s.current_stage, "sql")

    def test_documented_exception_needs_explicit_approval(self):
        def justified(d):
            unjustified_denormalization(d)
            d["physical"]["denormalizations"].append(
                {"table": "appointments", "description": "pet name copy", "justification": "reporting speed, reviewed"})
        self.apply(justified, ["add_attribute"])
        res = approval.check_approvable(self.s)
        self.assertEqual([i["kind"] for i in res["acknowledgements_required"]], ["denormalization_exception"])
        self.assertTrue(res["readiness"]["documented_exceptions"])
        self.assertFalse(res["readiness"]["known_violations"])
        with self.assertRaises(approval.ApprovalError):
            self.approve()
        ap, _ = approval.approve(self.s, acknowledge_normalization=True)
        rev = ap["normalization_review"]
        self.assertTrue(rev["documented_exceptions"] and rev["acknowledged_by_user"])
        self.assertEqual(rev["denormalization_exceptions_approved"][0]["table"], "appointments")
        self.assertEqual(rev["known_violations_acknowledged"], [])
        self.assertNotIn("appointments", rev["proven_3nf_tables"])        # never claimed as 3NF

    def test_unverified_3nf_needs_acknowledgement_and_is_not_reported_as_proven(self):
        p = mock.patch.object(na, "MAX_KEY_SEARCH_STEPS", 0)
        p.start()
        self.addCleanup(p.stop)
        self.apply(add_weight, ["add_attribute"])
        res = approval.check_approvable(self.s)
        kinds = {i["kind"] for i in res["acknowledgements_required"]}
        self.assertEqual(kinds, {"unverified_3nf"})
        self.assertFalse(res["readiness"]["known_violations"])
        self.assertTrue(any("UNVERIFIED" in x for x in res["problems"]))
        self.assertTrue(any("cut off" in w or "enumeration" in w for w in res["warnings"]))   # warning agrees with status
        for a in approval.analyze_design(self.s.data["design"]).values():
            if a["nf3_status"] == "unverified":
                self.assertFalse(a["formal_3nf"])
        norm = (self.s.output_dir / "normalization.md").read_text(encoding="utf-8")
        self.assertNotIn("**3NF:** satisfied", norm)                        # report agrees with the status
        with self.assertRaises(approval.ApprovalError):
            self.approve()
        code, text = self.run_cli("approve", ["--yes"])                    # --yes alone is not an acknowledgement
        self.assertEqual(code, 2)
        self.assertIn("ACKNOWLEDGEMENT REQUIRED", text)
        self.assertNotIn("approval", self.state())
        code, text = self.run_cli("approve", [], ["yes"])                  # a plain yes is not the acknowledgement either
        self.assertEqual(code, 2)
        self.assertNotIn("approval", self.state())
        code, text = self.run_cli("approve", [], [approval.ACK_PHRASE])    # typed phrase
        self.assertEqual(code, 0, text)
        self.assertTrue(self.state()["approval"]["normalization_review"]["acknowledged_by_user"])

    def test_cli_flag_and_phrase_never_accept_known_violation(self):
        self.apply(unjustified_denormalization, ["add_attribute"])
        for argv, inputs in ((["--yes"], []), (["--acknowledge-normalization"], []), (["--acknowledge-normalization", "--yes"], []),
                             ([], [approval.ACK_PHRASE])):
            code, text = self.run_cli("approve", argv, inputs)
            self.assertEqual(code, 2, text)
            self.assertIn("CANNOT APPROVE", text)
            self.assertNotIn("approval", self.state())
            self.assertEqual(self.state()["current_stage"], "revision")

    def test_enforcement_and_requirement_gaps_are_separate_warnings(self):
        self.s.data["design"]["requirement_gaps"] = ["gap one"]
        res = approval.check_approvable(self.s)
        self.assertTrue(any("unresolved requirement" in w for w in res["warnings"]))
        self.assertEqual(res["readiness"]["unresolved_requirements"], 1)
        enf = approval.enforcement_findings({"dbms": {"id": "mysql"}, "business_rules": [
            {"id": "BR1", "enforcement": {"mechanism": "trigger"}}, {"id": "BR2", "enforcement": {"mechanism": "application"}},
            {"id": "BR3", "enforcement": {"mechanism": "schema"}}]})
        self.assertEqual(len(enf["unsupported"]), 1)
        self.assertEqual(len(enf["outside_schema"]), 2)


class ReportsAgree(B):
    def test_normalization_md_agrees_with_status(self):
        d = self.s.data["design"]
        text = dd.render_normalization(d)
        self.assertIn("3NF verdict:** proven", text)
        self.assertEqual(dd.check_documents(d, {n: (self.s.output_dir / n).read_text(encoding="utf-8") for n in dd.DOC_FILES}), [])
        with mock.patch.object(na, "MAX_KEY_SEARCH_STEPS", 0):
            text2 = dd.render_normalization(d)
            self.assertNotEqual(dd.check_documents(d, {n: (self.s.output_dir / n).read_text(encoding="utf-8")
                                                      for n in dd.DOC_FILES}), [])   # old "satisfied" docs are now stale
        self.assertIn("UNVERIFIED", text2)
        self.assertNotIn("**3NF:** satisfied", text2)
        self.assertIn("mechanical review", text2)


class HistoricalCompatibility(B):
    def test_approval_is_not_invalidated_by_stricter_analysis(self):
        ap, _ = self.approve()
        before = copy.deepcopy(self.s.data["approval"])
        with mock.patch.object(na, "MAX_KEY_SEARCH_STEPS", 0):            # a "newer", stricter analyzer
            rep = approval.integrity_report(self.s)
            self.assertEqual(rep["blocking"], [])                           # never tampered because of newer rules
            self.assertIn(rep["state"], (approval.INTACT, approval.LEGACY))
            again, new = approval.approve(self.s)                           # no re-gating, no acknowledgement demanded
            self.assertFalse(new)
        self.assertEqual(again, before)
        self.assertNotIn("acknowledged_by_user", again)
        self.assertEqual(self.s.current_stage, "sql")

    def test_old_approval_record_without_new_fields_still_verifies(self):
        self.approve()
        self.s.data["approval"].pop("normalization_review")
        self.assertEqual(approval.integrity_report(self.s)["blocking"], [])
        self.assertEqual(approval.verify_approved_design(self.s)[0]["status"], "approved")

    def test_known_violation_in_historical_approval_not_regated(self):
        self.apply(unjustified_denormalization, ["add_attribute"])
        d = self.s.data                       # a historical approval (made under the old policy) of this exact design
        from dba_agent.state import now_iso
        d["approval"] = {"status": "approved", "approved_at": now_iso(), "design_version": len(d["revisions"]),
                         "design_fingerprint": approval.design_fingerprint(d["design"]), "dbms_id": d["design"]["dbms"]["id"],
                         "artifact_hashes": approval.artifact_hashes(self.s)}
        self.s.save()
        before = copy.deepcopy(d["approval"])
        ap, new = approval.approve(self.s)
        self.assertFalse(new)
        self.assertEqual(ap, before)
        self.assertEqual(approval.integrity_report(self.s)["blocking"], [])
        code, text = self.run_cli("approve", [])                           # CLI does not re-gate it either
        self.assertEqual(code, 0, text)
        self.assertIn("already approved", text)
        self.assertEqual(self.state()["approval"], before)


if __name__ == "__main__":
    unittest.main()
