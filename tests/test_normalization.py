"""FD / key analysis (normalization_analysis.py), including the false-positive 2NF regression."""
import unittest

from dba_agent.normalization_analysis import analyze_table, candidate_keys, closure, form_check


def table(cols, pk, uniques=(), unique_indexes=(), nullable=()):
    return {"name": "t", "columns": [{"name": c, "data_type": "INT", "nullable": c in nullable} for c in cols],
            "primary_key": list(pk), "unique_constraints": [{"name": f"u{i}", "columns": list(u)} for i, u in enumerate(uniques)],
            "indexes": [{"name": f"i{i}", "columns": list(u), "unique": True} for i, u in enumerate(unique_indexes)]}


def fd(det, dep):
    return (frozenset(det), frozenset(dep))


class ClosureTests(unittest.TestCase):
    def test_closure_is_transitive(self):
        self.assertEqual(closure({"a"}, [fd("a", "b"), fd("b", "c")]), {"a", "b", "c"})

    def test_closure_needs_whole_determinant(self):
        self.assertEqual(closure({"a"}, [fd("ab", "c")]), {"a"})


class RegressionTwoNF(unittest.TestCase):
    """PRIMARY KEY (a, b), UNIQUE (a), declared a -> v must NOT be a partial-key dependency."""

    def test_unique_subset_of_pk_is_not_a_partial_dependency(self):
        r = analyze_table(table("abv", "ab", uniques=["a"]), [fd("a", "v")], [])
        self.assertEqual(r["candidate_keys"], [["a"]])
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["highest"], "BCNF")
        self.assertTrue(any("not minimal" in n for n in r["notes"]))

    def test_same_with_unique_index(self):
        r = analyze_table(table("abv", "ab", unique_indexes=["a"]), [fd("a", "v")], [])
        self.assertEqual(r["findings"], [])

    def test_without_the_unique_key_it_is_a_real_violation(self):
        r = analyze_table(table("abv", "ab"), [fd("a", "v")], [])
        self.assertEqual([f["form"] for f in r["findings"]], ["2NF"])
        self.assertEqual(r["highest"], "1NF")
        self.assertEqual(r["status"], "VIOLATION, not justified")
        self.assertEqual(form_check(r, "2NF"), "VIOLATED")

    def test_unique_on_the_dependent_column_makes_it_a_key_and_implied_key_is_flagged(self):
        # v is UNIQUE and a -> v: then a determines everything, so (a) is the candidate key, v another one
        r = analyze_table(table("abv", "ab", uniques=["v"]), [fd("a", "v")], [])
        self.assertEqual(sorted(r["candidate_keys"]), [["a"], ["v"]])
        # no normal-form violation; but "a" is an implied key that nothing enforces -> reported as KEY finding
        self.assertEqual([f["form"] for f in r["findings"]], ["KEY"])


class NormalFormTests(unittest.TestCase):
    def test_transitive_dependency_is_3nf_violation(self):
        r = analyze_table(table(["id", "zip", "city"], ["id"]), [fd(["id"], ["zip", "city"]), fd(["zip"], ["city"])], [])
        self.assertEqual([f["form"] for f in r["findings"]], ["3NF"])
        self.assertEqual(r["highest"], "2NF")
        self.assertEqual(form_check(r, "2NF"), "satisfied")
        self.assertEqual(form_check(r, "3NF"), "VIOLATED")

    def test_bcnf_only_violation_keeps_3nf(self):
        # classic: R(s, c, t): (s,c)->t, t->c ; keys (s,c) and (s,t); c is prime
        r = analyze_table(table(["s", "c", "t"], ["s", "c"], uniques=[["s", "t"]]),
                          [fd(["s", "c"], ["t"]), fd(["t"], ["c"])], [])
        self.assertEqual([f["form"] for f in r["findings"]], ["BCNF"])
        self.assertEqual(r["highest"], "3NF")
        self.assertEqual(form_check(r, "3NF"), "satisfied")
        self.assertEqual(form_check(r, "BCNF"), "VIOLATED")

    def test_clean_table_is_bcnf(self):
        r = analyze_table(table("abc", "a"), [fd("a", "bc")], [])
        self.assertEqual((r["highest"], r["status"], r["findings"]), ("BCNF", "OK", []))

    def test_denormalization_marks_violation_as_justified(self):
        d = [{"table": "t", "description": "", "justification": "reporting snapshot"}]
        r = analyze_table(table("abv", "ab"), [fd("a", "v")], d)
        self.assertEqual(r["status"], "violation, justified denormalization")
        self.assertFalse(r["unjustified"])

    def test_declared_denormalization_without_detected_violation(self):
        d = [{"table": "t", "description": "", "justification": "x"}]
        self.assertEqual(analyze_table(table("abc", "a"), [fd("a", "bc")], d)["status"], "declared denormalization")

    def test_undeclared_key_dependency_is_reported(self):
        # FD says b determines everything but nothing enforces b as unique
        r = analyze_table(table("abc", "a"), [fd("a", "bc"), fd("b", "ac")], [])
        self.assertTrue(any(f["form"] == "KEY" for f in r["findings"]))

    def test_nullable_unique_key_is_noted(self):
        r = analyze_table(table("abc", "a", uniques=["b"], nullable="b"), [fd("a", "bc")], [])
        self.assertTrue(any("nullable" in n for n in r["notes"]))

    def test_json_column_flagged_for_1nf(self):
        t = table("ab", "a")
        t["columns"][1]["data_type"] = "JSONB"
        self.assertEqual(analyze_table(t, [fd("a", "b")], [])["non_atomic_columns"], ["b"])

    def test_incomplete_fds_cannot_reveal_violation(self):
        """Documented limitation: with the FD a -> v missing, the same table looks fine."""
        r = analyze_table(table("abv", "ab"), [fd("ab", "v")], [])
        self.assertEqual(r["findings"], [])
        self.assertEqual(r["highest"], "BCNF")

    def test_candidate_keys_helper_minimizes(self):
        t = table("abv", "ab", uniques=["a"])
        fds = [fd("a", "abv"), fd("ab", "abv")]
        self.assertEqual(candidate_keys(t, fds), [("a",)])


if __name__ == "__main__":
    unittest.main()
