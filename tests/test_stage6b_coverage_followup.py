"""Batch 3 follow-up: coverage must never demand an example the schema forbids.

Bug 1: nullable column + CHECK (reserved IS NULL) -> a non-NULL example was demanded.
Bug 2: CHECK status IN ('active','inactive') + CHECK status = 'active' -> an 'inactive' example was demanded.
SQLite is really executed (schema.sql + inserts); the other DBMSs are exercised statically only."""
import copy
import sqlite3
import unittest

from dba_agent import seed_data as sd, sql_validate as sv
from dba_agent.schema_sql import generate_schema_sql
from tests.test_stage6a_schema import col, hand, tab
from tests.test_stage6b_seed import LITE, MS, MY, PG

ALL = (LITE, PG, MY, MS)


def reserved_design(dialect=LITE, check="(reserved IS NULL)"):
    return hand(dialect, tab("slots", [col("slot_id"), col("reserved", "VARCHAR(10)", nullable=True)], ["slot_id"],
                             ck=[("ck_reserved", check)]))


def status_design(dialect=LITE, checks=None, extra=()):
    checks = checks or [("ck_in", "status IN ('active','inactive')"), ("ck_eq", "status = 'active'")]
    return hand(dialect, tab("accts", [col("acct_id"), col("status", "VARCHAR(10)"), *extra], ["acct_id"], ck=checks))


def rows(table, key, **cols):
    n = max(len(v) for v in cols.values())
    return {"tables": {table: [dict({key: i + 1}, **{c: v[i] for c, v in cols.items()}) for i in range(n)]}}


def check(design, data):
    m = sd.build_model(design)
    return m, sd.validate_records(m, copy.deepcopy(data), 1)


def sqlite_db(design):
    con = sqlite3.connect(":memory:")
    con.executescript(generate_schema_sql(design, "f" * 64, validate=False).sql)
    return con


def executes_on_sqlite(design, m, res):
    schema = generate_schema_sql(design, "f" * 64, validate=False).sql
    rep = sv.validate_sqlite(m, res.records, schema, sd.render_seed_sql(m, res.records, "f" * 64))
    return rep


class ImpossibleNonNullCoverageTests(unittest.TestCase):
    DATA = rows("slots", "slot_id", reserved=[None, None, None])

    def test_all_null_rows_are_valid_and_no_non_null_example_is_demanded(self):
        d = reserved_design()
        m, res = check(d, self.DATA)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.coverage, [])
        self.assertEqual(res.coverage_uncertain, [])
        self.assertTrue(any("slots.reserved" in n and "non-NULL example is not applicable" in n for n in res.coverage_na), res.coverage_na)
        rep = executes_on_sqlite(d, m, res)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertEqual(rep["level"], "executed")

    def test_sqlite_really_rejects_every_non_null_value(self):
        con = sqlite_db(reserved_design())
        for v in ("x", "R1", "", "0", 1, 3.5):
            with self.assertRaises(sqlite3.IntegrityError, msg=repr(v)):
                con.execute("INSERT INTO slots (slot_id, reserved) VALUES (1, ?)", (v,))
        con.execute("INSERT INTO slots (slot_id, reserved) VALUES (1, NULL)")

    def test_proof_works_for_parenthesised_and_conjoined_forms_in_every_dialect(self):
        for check_ in ("reserved IS NULL", "((reserved IS NULL))", "slot_id > 0 AND reserved IS NULL",
                       "(reserved IS NULL) AND (slot_id > 0)"):
            for dialect in ALL:
                m, res = check(reserved_design(dialect, check_), self.DATA)
                self.assertEqual(res.errors, [], (check_, dialect))
                self.assertEqual(res.coverage, [], (check_, dialect))
                self.assertTrue(any("not applicable" in n for n in res.coverage_na), (check_, dialect))

    def test_genuinely_feasible_non_null_coverage_is_still_required(self):
        for check_ in (None, "reserved IS NULL OR reserved > 0"):
            d = reserved_design(check=check_ or "slot_id > 0")
            _, res = check(d, self.DATA)
            self.assertEqual(res.errors, [], check_)
            self.assertEqual([i["kind"] for i in res.coverage_items], ["value"], check_)
            self.assertIn("NULL in every row", res.coverage[0])

    def test_undecidable_is_uncertain_not_required(self):
        d = reserved_design(check="reserved IS NULL OR reserved GLOB 'R[0-9]'")
        _, res = check(d, self.DATA)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.coverage, [])
        self.assertTrue(any("slots.reserved" in u and "could not be established" in u for u in res.coverage_uncertain))

    def test_values_already_present_are_never_questioned(self):
        _, res = check(reserved_design(check="slot_id > 0"), rows("slots", "slot_id", reserved=["a", None, "b"]))
        self.assertEqual((res.coverage, res.coverage_na, res.coverage_uncertain), ([], [], []))


class ImpossibleStatusCoverageTests(unittest.TestCase):
    DATA = rows("accts", "acct_id", status=["active", "active", "active"])

    def test_listed_value_forbidden_by_another_check_is_not_demanded(self):
        d = status_design()
        m, res = check(d, self.DATA)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.coverage, [])
        self.assertEqual(res.coverage_uncertain, [])
        self.assertTrue(any("accts.status" in n and "'inactive'" in n and "not applicable" in n for n in res.coverage_na), res.coverage_na)
        rep = executes_on_sqlite(d, m, res)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertEqual(rep["level"], "executed")

    def test_sqlite_really_rejects_the_listed_value(self):
        con = sqlite_db(status_design())
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO accts (acct_id, status) VALUES (1, 'inactive')")
        con.execute("INSERT INTO accts (acct_id, status) VALUES (1, 'active')")

    def test_other_proof_shapes_and_every_dialect(self):
        shapes = ([("a", "status IN ('active','inactive')"), ("b", "status <> 'inactive'")],
                  [("a", "status IN ('active','inactive')"), ("b", "status IN ('active')")],
                  [("a", "status IN ('active','inactive')"), ("b", "(status = 'active') AND length(status) > 1")])
        for i, chk in enumerate(shapes):
            for dialect in ALL:
                _, res = check(status_design(dialect, chk), self.DATA)
                self.assertEqual(res.coverage, [], (i, dialect, res.coverage))
                if i != 1:                                      # shape 1 narrows the allowed list itself: nothing left to classify
                    self.assertTrue(any("not applicable" in n for n in res.coverage_na), (i, dialect))

    def test_genuinely_feasible_status_coverage_is_still_required(self):
        d = status_design(checks=[("ck_in", "status IN ('active','inactive')")])
        _, res = check(d, self.DATA)
        self.assertEqual([i["kind"] for i in res.coverage_items], ["status"])
        self.assertIn("'inactive'", res.coverage[0])
        _, res = check(d, rows("accts", "acct_id", status=["active", "inactive", "active"]))
        self.assertEqual(res.coverage, [])

    def test_value_allowed_only_in_another_row_state_is_still_required(self):
        d = status_design(checks=[("ck_in", "status IN ('active','inactive')"),
                                  ("ck_kind", "(kind = 'a' AND status = 'active') OR kind <> 'a'")],
                          extra=[col("kind", "VARCHAR(5)")])
        data = rows("accts", "acct_id", status=["active"] * 3, kind=["a", "a", "a"])
        m, res = check(d, data)
        self.assertEqual(res.errors, [])
        self.assertEqual([i["kind"] for i in res.coverage_items], ["status"])        # feasible with kind <> 'a'
        self.assertEqual(res.coverage_na, [])

    def test_undecidable_value_is_uncertain_not_required(self):
        d = status_design(checks=[("ck_in", "status IN ('active','inactive')"), ("ck_eq", "status = lower('ACTIVE')")])
        _, res = check(d, self.DATA)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.coverage, [])
        self.assertTrue(any("accts.status" in u and "could not be established" in u for u in res.coverage_uncertain), res.coverage_uncertain)

    def test_partially_feasible_values_only_demand_the_feasible_ones(self):
        d = status_design(checks=[("ck_in", "status IN ('active','inactive','closed')"), ("ck_ne", "status <> 'inactive'")])
        _, res = check(d, self.DATA)
        self.assertEqual(len(res.coverage_items), 1)
        self.assertIn("['closed']", res.coverage[0])
        self.assertNotIn("inactive", res.coverage[0])
        self.assertTrue(any("'inactive'" in n for n in res.coverage_na))

    def test_numeric_values_compare_numerically(self):
        d = hand(LITE, tab("lv", [col("lv_id"), col("level", "INTEGER")], ["lv_id"],
                           ck=[("ck_in", "level IN (1, 2)"), ("ck_eq", "level = 1")]))
        _, res = check(d, rows("lv", "lv_id", level=[1, 1, 1]))
        self.assertEqual(res.errors, [])
        self.assertEqual(res.coverage, [])
        self.assertTrue(any("not applicable" in n for n in res.coverage_na))
        con = sqlite_db(d)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO lv (lv_id, level) VALUES (1, 2)")

    def test_repair_never_touches_impossible_value_cases(self):
        m, res = check(status_design(), self.DATA)
        self.assertIs(sd.repair_coverage(m, res), res)


if __name__ == "__main__":
    unittest.main()
