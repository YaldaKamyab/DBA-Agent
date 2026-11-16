"""Batch 3 - Stage 6B repairs: conditional NULL coverage, composite-FK NULL semantics, partial UNIQUE indexes,
coverage-only status reporting, offline coverage repair and multi-DBMS behaviour.

SQLite is really executed; PostgreSQL / MySQL-MariaDB / SQL Server are only exercised statically (the Python validator and
SQL text) - nothing here claims those servers ran any SQL. No LLM is ever called (scripted stand-ins only)."""
import copy
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from dba_agent import seed_data as sd, sql_validate as sv
from dba_agent.schema_sql import generate_schema_sql
from dba_agent.stages import sql_seed
from tests.test_stage6a_schema import col, fk, hand, tab
from tests.test_stage6b_seed import (LITE, MS, MY, PG, SeedLLM, SeedStageBase, clinic_data)

ROOT = Path(__file__).resolve().parent.parent
ALL = (LITE, PG, MY, MS)


def build(design):
    return sd.build_model(design)


def validate(design, data, min_rows=1):
    return sd.validate_records(build(design), copy.deepcopy(data), min_rows)


# ------------------------------------------------------------------------------------------ conditional NULL coverage
COND = "(state = 'closed' AND closed_on IS NOT NULL) OR (state <> 'closed' AND closed_on IS NULL)"


def cond_design(dialect=LITE, check=COND, extra_cols=(), state_in=False):
    cks = [("ck_closed", check)] + ([("ck_state", "state IN ('open','closed')")] if state_in else [])
    return hand(dialect, tab("accounts", [col("acc_id"), col("state", "VARCHAR(10)"), col("closed_on", "DATE", nullable=True),
                                          *extra_cols], ["acc_id"], ck=cks))


def accounts(*pairs):
    return {"tables": {"accounts": [{"acc_id": i, "state": s, "closed_on": c} for i, (s, c) in enumerate(pairs, 1)]}}


class ConditionalNullCoverageTests(unittest.TestCase):
    def test_conditional_check_does_not_make_a_column_unconditionally_required(self):
        d = cond_design()
        res = validate(d, accounts(("closed", "2024-01-01"), ("closed", "2024-02-01"), ("closed", "2024-03-01")))
        self.assertFalse(res.errors, res.errors)
        self.assertEqual([i["column"] for i in res.coverage_items if i["kind"] == "null_example"], ["closed_on"])
        self.assertFalse(any("closed_on" in n for n in res.coverage_na))      # was wrongly "not applicable"
        res = validate(d, accounts(("open", None), ("closed", "2024-02-01"), ("open", None)))
        self.assertEqual(res.coverage, [])                                     # a NULL example exists -> covered

    def test_unconditional_is_not_null_is_proved_infeasible(self):
        for check in ("closed_on IS NOT NULL", "(closed_on IS NOT NULL) AND state = 'open'",
                      "length(state) > 1 AND closed_on IS NOT NULL AND state <> 'x'"):
            d = cond_design(check=check)
            data = accounts(("open", "2024-01-01"), ("open", "2024-02-01"), ("open", "2024-03-01"))
            res = validate(d, data)
            self.assertFalse(res.errors, (check, res.errors))
            self.assertEqual(res.coverage, [], check)
            self.assertTrue(any("closed_on" in n and "not applicable" in n for n in res.coverage_na), check)

    def test_genuine_not_null_is_still_enforced(self):
        d = hand(LITE, tab("t", [col("id"), col("note", "VARCHAR(20)")], ["id"]))
        res = validate(d, {"tables": {"t": [{"id": 1, "note": None}]}})
        self.assertTrue(any("NOT NULL" in e for e in res.errors))
        res = validate(cond_design(), accounts(("closed", None)))              # the CHECK itself still rejects bad rows
        self.assertTrue(any("violates CHECK" in e for e in res.errors))

    def test_three_valued_logic_null_operand_passes_a_plain_check(self):
        d = hand(LITE, tab("t", [col("id"), col("qty", nullable=True)], ["id"], ck=[("ck_q", "qty > 0")]))
        res = validate(d, {"tables": {"t": [{"id": i, "qty": 5} for i in (1, 2, 3)]}})
        self.assertEqual(len(res.coverage_items), 1)                           # qty > 0 with NULL is UNKNOWN -> passes -> feasible
        self.assertEqual(sd.null_feasibility(build(d), build(d).tables["t"], "qty", [])[0], "feasible")

    def test_null_strict_conditional_checks_match_schema_sql(self):
        # schema.sql generates this CHECK NULL-strict; the Python validator must reject the same rows
        res = validate(cond_design(), accounts(("open", None), ("closed", None)))
        self.assertTrue(any("accounts row 2" in e and "violates CHECK" in e for e in res.errors), res.errors)

    def test_undecidable_feasibility_is_reported_as_uncertain_not_claimed(self):
        chk = "(state = 'open' AND closed_on IS NOT NULL) OR (state = 'open' AND closed_on > '2000-01-01')"
        d = cond_design(check=chk)
        res = validate(d, accounts(("open", "2024-01-01"), ("open", "2024-02-01"), ("open", "2024-03-01")))
        self.assertFalse(res.errors, res.errors)
        self.assertEqual(res.coverage, [])                                      # not required ...
        self.assertEqual(res.coverage_na, [])                                   # ... and not declared impossible
        self.assertTrue(any("closed_on" in u and "could not be established" in u for u in res.coverage_uncertain))

    def test_feasible_infeasible_unknown_directly(self):
        m = build(cond_design())
        t = m.tables["accounts"]
        self.assertEqual(sd.null_feasibility(m, t, "closed_on", [])[0], "feasible")
        m2 = build(cond_design(check="closed_on IS NOT NULL"))
        self.assertEqual(sd.null_feasibility(m2, m2.tables["accounts"], "closed_on", [])[0], "infeasible")

    def test_conjunct_splitter_only_proves_top_level_requirements(self):
        self.assertTrue(sd._requires_value("a > 1 AND (c IS NOT NULL)", "c"))
        self.assertFalse(sd._requires_value("c IS NOT NULL OR a > 1", "c"))
        self.assertFalse(sd._requires_value("(s = 'x' AND c IS NOT NULL) OR (s <> 'x' AND c IS NULL)", "c"))
        self.assertFalse(sd._requires_value("s = 'a AND c IS NOT NULL'", "c"))   # inside a string literal
        self.assertFalse(sd._requires_value("a BETWEEN 1 AND c IS NOT NULL", "c"))

    def test_conditional_rules_across_dialects(self):
        for dialect in ALL:
            d = cond_design(dialect)
            res = validate(d, accounts(("closed", "2024-01-01"), ("closed", "2024-02-01"), ("closed", "2024-03-01")))
            self.assertFalse(res.errors, (dialect, res.errors))
            self.assertEqual(len(res.coverage_items), 1, dialect)


# ------------------------------------------------------------------------------------------ composite FK NULL semantics
def fk_design(dialect=LITE, nullable_child=True, match=None):
    f = fk("fk_r_l", ["order_id", "line_no"], "lines", ["order_id", "line_no"])
    if match:
        f["match"] = match
    return hand(dialect,
                tab("lines", [col("order_id"), col("line_no"), col("sku", "VARCHAR(20)")], ["order_id", "line_no"]),
                tab("returns", [col("return_id"), col("order_id", nullable=nullable_child), col("line_no", nullable=nullable_child)],
                    ["return_id"], [f]))


def fk_data(*pairs):
    return {"tables": {"lines": [{"order_id": 1, "line_no": 1, "sku": "KB-2210"}, {"order_id": 1, "line_no": 2, "sku": "MS-0042"}],
                       "returns": [{"return_id": i, "order_id": o, "line_no": n} for i, (o, n) in enumerate(pairs, 1)]}}


class CompositeForeignKeyNullTests(unittest.TestCase):
    def test_partly_null_key_needs_no_parent_row_in_every_dialect(self):
        for dialect in ALL:
            for pair in ((None, None), (7, None), (None, 5), (1, 2)):
                res = validate(fk_design(dialect), fk_data(pair))
                self.assertFalse(res.errors, (dialect, pair, res.errors))

    def test_non_null_invalid_reference_is_still_rejected(self):
        for dialect in ALL:
            res = validate(fk_design(dialect), fk_data((1, 9)))
            self.assertTrue(any("does not exist" in e for e in res.errors), dialect)
            res = validate(fk_design(dialect), fk_data((9, 1)))
            self.assertTrue(any("does not exist" in e for e in res.errors), dialect)

    def test_separate_not_null_is_respected(self):
        d = fk_design()
        for t in d["physical"]["tables"]:
            if t["name"] == "returns":
                t["columns"][2]["nullable"] = False                              # line_no NOT NULL, order_id nullable
        res = validate(d, fk_data((None, None)))
        self.assertTrue(any("`line_no` is NOT NULL" in e for e in res.errors), res.errors)
        res = validate(d, fk_data((None, 2)))
        self.assertFalse(res.errors, res.errors)                                 # NULL order_id, line_no filled: MATCH SIMPLE
        d2 = fk_design(nullable_child=False)
        res = validate(d2, fk_data((None, 1)))
        self.assertTrue(any("NOT NULL" in e for e in res.errors))

    def test_declared_match_full_and_unknown_semantics_are_not_assumed_simple(self):
        res = validate(fk_design(PG, match="FULL"), fk_data((7, None)))
        self.assertTrue(any("MATCH FULL" in e for e in res.errors), res.errors)
        res = validate(fk_design(PG, match="FULL"), fk_data((None, None)))       # all-NULL is always fine
        self.assertFalse(res.errors)
        original = dict(sd.FK_PARTIAL_NULL)
        try:
            del sd.FK_PARTIAL_NULL[MS]
            res = validate(fk_design(MS), fk_data((7, None)))
            self.assertTrue(any("not verified for SQL Server" in e for e in res.errors), res.errors)
        finally:
            sd.FK_PARTIAL_NULL.clear()
            sd.FK_PARTIAL_NULL.update(original)
        self.assertEqual(set(sd.FK_PARTIAL_NULL), set(ALL))

    def test_sqlite_really_accepts_partly_null_composite_key(self):
        d = fk_design(LITE)
        m = build(d)
        res = sd.validate_records(m, fk_data((None, None), (7, None), (1, 2)), 1)
        self.assertFalse(res.errors)
        schema = generate_schema_sql(d, "f" * 64, validate=False).sql
        seed = sd.render_seed_sql(m, res.records, "f" * 64)
        rep = sv.validate_sqlite(m, res.records, schema, seed)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertEqual(rep["level"], "executed")

    def test_circular_deferred_key_keeps_partly_null_values(self):
        # deferred keys are loaded NULL then UPDATEd: a partly-NULL key must still get its non-NULL component
        d = hand(LITE,
                 tab("a", [col("a1"), col("a2"), col("b1", nullable=True), col("b2", nullable=True)], ["a1", "a2"],
                     [fk("fk_ab", ["b1", "b2"], "b", ["b1", "b2"])]),
                 tab("b", [col("b1"), col("b2"), col("a1", nullable=True), col("a2", nullable=True)], ["b1", "b2"],
                     [fk("fk_ba", ["a1", "a2"], "a", ["a1", "a2"])]))
        m = build(d)
        data = {"tables": {"a": [{"a1": 1, "a2": 1, "b1": 5, "b2": None}], "b": [{"b1": 1, "b2": 1, "a1": None, "a2": None}]}}
        res = sd.validate_records(m, data, 1)
        self.assertFalse(res.errors, res.errors)
        sql = sd.render_seed_sql(m, res.records)
        self.assertIn('UPDATE "a" SET "b1" = 5, "b2" = NULL', sql)


# ------------------------------------------------------------------------------------------ partial UNIQUE indexes
def partial_design(dialect=LITE, where=None, unique_cols=("book_id",), extra_unique=None, nullable_key=False):
    where = [{"column": "status", "operator": "=", "value": "active"}] if where is None else where
    t = tab("loans", [col("loan_id"), col("book_id", nullable=nullable_key), col("copy_no", nullable=True),
                      col("status", "VARCHAR(10)"), col("voided_at", "TIMESTAMP" if dialect != LITE else "TEXT", nullable=True)],
            ["loan_id"], ck=[("ck_status", "status IN ('active','returned','lost')")])
    t["indexes"].append({"name": "uq_one_active", "columns": list(unique_cols), "unique": True, "purpose": "", "where": where})
    if extra_unique:
        t["indexes"].append({"name": "uq_plain", "columns": list(extra_unique), "unique": True, "purpose": ""})
    return hand(dialect, t)


def loans(*rows):
    return {"tables": {"loans": [dict({"loan_id": i, "book_id": 1, "copy_no": None, "status": "active", "voided_at": None}, **r)
                                 for i, r in enumerate(rows, 1)]}}


class PartialUniqueTests(unittest.TestCase):
    def test_model_carries_the_structured_condition(self):
        u = build(partial_design()).tables["loans"]["uniques"][0]
        self.assertEqual(u["where"], [{"column": "status", "operator": "=", "value": "active"}])

    def test_duplicate_historical_records_outside_the_condition_are_accepted(self):
        for dialect in (LITE, PG, MS):
            res = validate(partial_design(dialect), loans({"status": "returned"}, {"status": "returned"}, {"status": "lost"}))
            self.assertFalse(res.errors, (dialect, res.errors))

    def test_duplicate_active_records_inside_the_condition_are_rejected(self):
        for dialect in (LITE, PG, MS):
            res = validate(partial_design(dialect), loans({"status": "active"}, {"status": "active"}))
            self.assertTrue(any("violates UNIQUE `uq_one_active`" in e and "among rows where status = 'active'" in e
                                for e in res.errors), (dialect, res.errors))

    def test_one_matching_and_one_nonmatching_row_is_accepted(self):
        res = validate(partial_design(), loans({"status": "active"}, {"status": "returned"}, {"status": "lost"}))
        self.assertFalse(res.errors, res.errors)
        res = validate(partial_design(), loans({"status": "active"}, {"status": "returned"}, {"status": "active", "book_id": 2}))
        self.assertFalse(res.errors, res.errors)                                  # different key value: fine

    def test_ordinary_unique_index_is_unchanged(self):
        d = hand(LITE, tab("t", [col("id"), col("code", "VARCHAR(10)")], ["id"], ix=[("uq_code", ["code"], True)]))
        res = validate(d, {"tables": {"t": [{"id": 1, "code": "A"}, {"id": 2, "code": "A"}]}})
        self.assertTrue(any("violates UNIQUE `uq_code`" in e and "among rows" not in e for e in res.errors))
        res = validate(partial_design(extra_unique=["book_id", "copy_no"]),
                       loans({"status": "returned", "copy_no": 1}, {"status": "returned", "copy_no": 1}))
        self.assertTrue(any("uq_plain" in e for e in res.errors), res.errors)     # unconditional one still applies to all rows

    def test_nullable_unique_values_are_distinct(self):
        d = partial_design(nullable_key=True)
        res = validate(d, loans({"book_id": None}, {"book_id": None}, {"book_id": None}))
        self.assertFalse(res.errors, res.errors)                                  # NULLs never collide, even inside the condition

    def test_composite_partial_unique(self):
        d = partial_design(unique_cols=("book_id", "copy_no"),
                           where=[{"column": "status", "operator": "IN", "value": ["active", "lost"]}])
        ok = loans({"copy_no": 1, "status": "active"}, {"copy_no": 2, "status": "active"}, {"copy_no": 1, "status": "returned"},
                   {"copy_no": 1, "status": "returned"})
        self.assertFalse(validate(d, ok).errors)
        bad = loans({"copy_no": 1, "status": "active"}, {"copy_no": 1, "status": "lost"})
        self.assertTrue(any("uq_one_active" in e for e in validate(d, bad).errors))
        partial_null = loans({"copy_no": None}, {"copy_no": None})                 # NULL component: distinct
        self.assertFalse(validate(d, partial_null).errors)

    def test_is_null_and_is_not_null_predicates_and_conjunctions(self):
        d = partial_design(where=[{"column": "voided_at", "operator": "IS NULL"}, {"column": "status", "operator": "<>", "value": "lost"}])
        ok = loans({"status": "active"}, {"status": "lost"}, {"status": "returned", "voided_at": "2024-01-01 00:00:00"})
        self.assertFalse(validate(d, ok).errors)
        bad = loans({"status": "active"}, {"status": "returned"})
        self.assertTrue(validate(d, bad).errors)
        d2 = partial_design(where=[{"column": "voided_at", "operator": "IS NOT NULL"}])
        two = loans({"voided_at": "2024-01-01 00:00:00"}, {"voided_at": "2024-01-02 00:00:00"}, {"voided_at": None}, {"voided_at": None})
        self.assertTrue(validate(d2, two).errors)
        self.assertFalse(validate(d2, loans({"voided_at": None}, {"voided_at": None}, {"voided_at": "2024-01-02 00:00:00"})).errors)

    def test_null_operand_excludes_the_row_three_valued(self):
        m = build(partial_design())
        t = m.tables["loans"]
        w = [{"column": "voided_at", "operator": "=", "value": "x"}]
        self.assertFalse(sd.where_matches(m, t, {"voided_at": None}, w))           # NULL = 'x' is UNKNOWN -> not indexed
        self.assertIsNone(sd.where_matches(m, t, {"voided_at": "x"}, [{"column": "voided_at", "operator": "LIKE", "value": "x"}]))
        self.assertIsNone(sd.where_matches(m, t, {"nope": 1}, [{"column": "nope", "operator": "=", "value": 1}]))

    def test_unevaluable_predicate_is_a_warning_not_an_invented_verdict(self):
        d = partial_design()
        d["physical"]["tables"][0]["indexes"][0]["where"] = [{"column": "status", "operator": "LIKE", "value": "act%"}]
        res = validate(d, loans({"status": "active"}, {"status": "active"}))
        self.assertFalse(res.errors, res.errors)
        self.assertTrue(any("cannot be evaluated here" in w for w in res.warnings))

    def test_default_values_are_used_when_literal_and_unknown_when_dynamic(self):
        d = partial_design()
        d["physical"]["tables"][0]["columns"][3]["default"] = "'active'"
        res = validate(d, {"tables": {"loans": [{"loan_id": 1, "book_id": 1}, {"loan_id": 2, "book_id": 1}]}})
        self.assertTrue(any("uq_one_active" in e for e in res.errors), res.errors)
        d["physical"]["tables"][0]["columns"][3]["default"] = "current_user"
        res = validate(d, {"tables": {"loans": [{"loan_id": 1, "book_id": 1}, {"loan_id": 2, "book_id": 1}]}})
        self.assertFalse(res.errors)
        self.assertTrue(any("cannot be evaluated here" in w for w in res.warnings))

    def test_partial_unique_does_not_cap_the_required_row_count(self):
        d = hand(LITE,
                 tab("books", [col("book_id")], ["book_id"]),
                 tab("holds", [col("hold_id"), col("book_id"), col("status", "VARCHAR(8)")], ["hold_id"],
                     [fk("fk_h_b", ["book_id"], "books", ["book_id"])]))
        d["physical"]["tables"][1]["indexes"].append({"name": "uq_h", "columns": ["book_id"], "unique": True, "purpose": "",
                                                      "where": [{"column": "status", "operator": "=", "value": "open"}]})
        req, notes = sd.required_rows(build(d), 8)
        self.assertEqual(req["holds"], 8, notes)

    def test_dialect_support_and_unsupported_dialect(self):
        for dialect in (LITE, PG, MS):
            build(partial_design(dialect))
        with self.assertRaises(sd.SeedError) as cm:
            build(partial_design(MY))
        self.assertIn("MySQL/MariaDB does not support", str(cm.exception))
        self.assertEqual(sd.PARTIAL_UNIQUE_DIALECTS, (LITE, PG, MS))

    def test_prompt_description_states_the_condition(self):
        text = sd.describe_schema(build(partial_design()))
        self.assertIn("ONLY AMONG ROWS WHERE status = 'active'", text)

    def test_sqlite_execution_agrees_with_the_python_validator(self):
        d = partial_design()
        m = build(d)
        data = loans({"status": "returned", "book_id": 1}, {"status": "returned", "book_id": 1}, {"status": "active", "book_id": 1})
        res = sd.validate_records(m, copy.deepcopy(data), 1)
        self.assertFalse(res.errors, res.errors)
        schema = generate_schema_sql(d, "f" * 64, validate=False).sql
        self.assertIn("WHERE", schema)
        rep = sv.validate_sqlite(m, res.records, schema, sd.render_seed_sql(m, res.records))
        self.assertTrue(rep["ok"], rep["errors"])
        bad = loans({"status": "active", "book_id": 1}, {"status": "active", "book_id": 1})
        self.assertTrue(sd.validate_records(m, copy.deepcopy(bad), 1).errors)       # Python rejects ...
        con = sqlite3.connect(":memory:")                                             # ... exactly what the real index rejects
        con.executescript(schema)
        con.execute("INSERT INTO loans (loan_id, book_id, status) VALUES (1, 1, 'active')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO loans (loan_id, book_id, status) VALUES (2, 1, 'active')")
        con.execute("INSERT INTO loans (loan_id, book_id, status) VALUES (3, 1, 'returned')")


# ------------------------------------------------------------------------------------------ coverage repair + status
class CoverageRepairUnitTests(unittest.TestCase):
    def test_repair_fills_only_the_missing_null_example_and_revalidates(self):
        d = cond_design()
        m = build(d)
        res = sd.validate_records(m, copy.deepcopy(accounts(("open", None), ("open", None), ("closed", "2024-01-01"))), 1)
        self.assertEqual(res.coverage, [])
        full = {"tables": {"accounts": [{"acc_id": i, "state": "open", "closed_on": None} for i in (1, 2, 3)]}}
        d2 = hand(LITE, tab("people", [col("pid"), col("name", "VARCHAR(20)"), col("nick", "VARCHAR(20)", nullable=True)], ["pid"]))
        m2 = build(d2)
        data = {"tables": {"people": [{"pid": i, "name": f"Name{i}x", "nick": f"nick{i}x"} for i in (1, 2, 3)]}}
        res = sd.validate_records(m2, copy.deepcopy(data), 1)
        self.assertEqual(len(res.coverage), 1)
        fixed = sd.repair_coverage(m2, res)
        self.assertEqual(fixed.coverage, [])
        self.assertEqual(len(fixed.repairs), 1)
        self.assertEqual(fixed.repairs[0]["column"], "nick")
        self.assertEqual(sum(1 for r in fixed.records["people"] if r["nick"] is None), 1)       # exactly one cell changed
        self.assertEqual([r["name"] for r in fixed.records["people"]], [r["name"] for r in res.records["people"]])
        self.assertEqual(res.repairs, [])                                                         # input untouched
        self.assertTrue(res.records["people"][0]["nick"])

    def test_repair_prefers_the_sparsest_row(self):
        d = hand(LITE, tab("p", [col("pid"), col("a", "VARCHAR(9)", nullable=True), col("b", "VARCHAR(9)", nullable=True)], ["pid"]))
        m = build(d)
        data = {"tables": {"p": [{"pid": 1, "a": "x1", "b": "y1"}, {"pid": 2, "a": "x2", "b": None}, {"pid": 3, "a": "x3", "b": "y3"}]}}
        res = sd.validate_records(m, data, 1)
        fixed = sd.repair_coverage(m, res)
        self.assertEqual(fixed.coverage, [])
        self.assertEqual([r["row"] for r in fixed.repairs], [2])          # row 2 already has a NULL sibling -> nulling `a` there

    def test_repair_refuses_when_no_single_cell_change_is_valid(self):
        # the NULL example is feasible only together with another column's value; changing the one cell breaks the CHECK
        d = cond_design(state_in=True)                                     # 'open' is a known value, so a NULL example IS feasible
        data = accounts(("closed", "2024-01-01"), ("closed", "2024-02-01"), ("closed", "2024-03-01"))
        m = build(d)
        res = sd.validate_records(m, data, 1)
        self.assertTrue(any("closed_on" in c for c in res.coverage), res.coverage)
        fixed = sd.repair_coverage(m, res)
        self.assertEqual(fixed.repairs, [])
        self.assertEqual(fixed.coverage, res.coverage)                    # coverage check is not weakened

    def test_repair_never_hides_other_gaps_or_creates_new_ones(self):
        res = sd.Validated()
        res.errors = ["x"]
        self.assertIs(sd.repair_coverage(build(cond_design()), res), res)

    def test_repair_does_not_touch_status_gaps(self):
        d = hand(LITE, tab("s", [col("sid"), col("st", "VARCHAR(8)")], ["sid"], ck=[("ck", "st IN ('a','b','c')")]))
        m = build(d)
        res = sd.validate_records(m, {"tables": {"s": [{"sid": i, "st": "a"} for i in (1, 2, 3)]}}, 1)
        self.assertEqual(len(res.coverage), 1)
        self.assertEqual(sd.repair_coverage(m, res).repairs, [])


class CoverageStageTests(SeedStageBase):
    def _missing_null(self):
        data = clinic_data()
        for r in data["tables"]["owners"]:
            r["phone"] = r["phone"] or "+49-69-555-0100"
        return data

    def _flat_status(self):
        data = clinic_data()
        for r in data["tables"]["appointments"]:
            r["status"] = "booked"
        return data

    def test_missing_null_example_is_repaired_offline_without_extra_llm_calls(self):
        self.prepare(sqlite=True)
        res, llm = self.seed(self._missing_null())
        self.assertEqual(len(llm.calls), 1)                                   # one LLM call, no coverage retry
        self.assertEqual(llm.rejected, [])
        self.assertTrue(res.completed)
        rec = self.s.data["sql_stage"]["seed"]
        self.assertEqual(len(rec["offline_repairs"]), 1)
        self.assertEqual(rec["offline_repairs"][0]["column"], "phone")
        self.assertTrue(any("offline repair" in w for w in rec["warnings"]))
        saved = json.loads(self.path("seed-data.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["repairs"]), 1)
        self.assertEqual(sum(1 for r in saved["records"]["owners"] if r.get("phone") is None), 1)
        sm = rec["validation"]["status_summary"]
        self.assertEqual((sm["sql_execution"], sm["coverage"], sm["stage6"]), ("passed", "complete", "complete"))

    def test_coverage_only_failure_is_not_reported_as_an_sql_failure(self):
        self.prepare(sqlite=True)
        flat = self._flat_status()
        res, llm = self.seed(flat, flat, flat)
        self.assertFalse(res.completed)
        v = self.s.data["sql_stage"]["seed"]["validation"]
        self.assertEqual(v["level"], "executed")
        self.assertTrue(v["ok"])                                               # the SQL checks passed
        self.assertEqual(v["sql_errors"], [])
        self.assertTrue(v["coverage_missing"])
        self.assertFalse(v["completes_stage"])
        sm = v["status_summary"]
        self.assertEqual(sm["sql_execution"], "passed")
        self.assertIn("passed", sm["referential_integrity"])
        self.assertEqual(sm["coverage"], "incomplete")
        self.assertEqual(sm["stage6"], "incomplete")
        self.assertIn("seed generation", " ".join(sm).replace("_", " "))
        self.assertEqual(sm["business_rules"]["status"], "unverified")
        st = sql_seed.seed_status(self.s)
        self.assertEqual(st["state"], "coverage_incomplete")
        self.assertIn("SQL executed", st["detail"])
        self.assertIn("not an SQL failure", self.s.data["sql_stage"]["note"])
        self.assertTrue(self.path().exists())                                  # partial output preserved
        self.assertTrue(self.path("seed-data.json").exists())

    def test_real_sql_failure_stays_failed_validation(self):
        self.prepare(sqlite=True)
        bad = {"dbms": "sqlite", "level": "failed", "ok": False, "completes_stage": False, "executed_on": "SQLite",
               "checks": [], "errors": ["seed.sql statement 2 failed"], "warnings": [], "row_counts": {}, "reason": ""}
        from unittest import mock
        with mock.patch.object(sql_seed.sql_validate, "validate_sqlite", return_value=bad):
            self.seed(clinic_data())
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "failed_validation")
        self.assertEqual(self.s.data["sql_stage"]["seed"]["validation"]["status_summary"]["sql_execution"], "failed")

    def _saved_repairable(self):
        """A finished run, then seed-data.json replaced by data that only lacks a NULL example (state reopened)."""
        self.prepare(sqlite=True)
        self.seed(clinic_data())
        saved = json.loads(self.path("seed-data.json").read_text(encoding="utf-8"))
        for r in saved["records"]["owners"]:
            r["phone"] = r["phone"] or "+49-69-555-0100"
        self.path("seed-data.json").write_text(json.dumps(saved), encoding="utf-8")
        self.s.data["sql_stage"].pop("seed")
        self.s.data["completed_stages"].remove("sql")

    def test_plain_seed_reuses_saved_data_and_repairs_offline_without_the_llm(self):
        self._saved_repairable()
        res, llm = self.seed()                                                  # no scripted answer: any LLM call would fail
        self.assertEqual(llm.calls, [])
        self.assertTrue(res.reused_data)
        self.assertTrue(res.completed)
        self.assertEqual(len(self.s.data["sql_stage"]["seed"]["offline_repairs"]), 1)

    def test_regenerate_and_force_still_ask_the_llm(self):
        for kw in ({"regenerate": True}, {"force": True}):
            self._saved_repairable()
            res, llm = self.seed(clinic_data(), **kw)
            self.assertEqual(len(llm.calls), 1, kw)
            self.assertFalse(res.reused_data, kw)
            self.assertTrue(res.completed, kw)
            self.tearDown()
            self.setUp()

    def test_saved_data_with_unrepairable_gap_asks_the_llm_again_and_keeps_old_file_on_failure(self):
        self.prepare(sqlite=True)
        flat = self._flat_status()
        self.seed(flat, flat, flat)
        before = self.path("seed-data.json").read_text(encoding="utf-8")
        res, llm = self.seed(clinic_data())
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(res.completed)
        from dba_agent.errors import LLMError
        self.s.data["sql_stage"].pop("seed")
        self.s.data["completed_stages"].remove("sql")
        self.path("seed-data.json").write_text(before, encoding="utf-8")
        with self.assertRaises(LLMError):
            self.seed(flat, flat, flat, regenerate=True) if False else self.seed(*([{"bad": 1}] * 3), regenerate=True)
        self.assertEqual(self.path("seed-data.json").read_text(encoding="utf-8"), before)      # diagnostics not destroyed

    def test_repair_is_not_applied_to_a_dataset_that_already_covers_everything(self):
        self.prepare(sqlite=True)
        res, llm = self.seed(clinic_data())
        self.assertEqual(self.s.data["sql_stage"]["seed"]["offline_repairs"], [])
        self.assertNotIn("repairs", json.loads(self.path("seed-data.json").read_text(encoding="utf-8")))

    def test_cli_reports_the_separate_facts_and_coverage_only_wording(self):
        self.prepare(sqlite=True)
        flat = self._flat_status()
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(flat, flat, flat))
        self.assertEqual(code, 1, text)
        self.assertIn("Stage 6B status:", text)
        self.assertIn("SQL execution:          passed", text)
        self.assertIn("sample-data coverage:   incomplete", text)
        self.assertIn("the SQL itself is fine", text)
        self.assertNotIn("Validation: FAILED", text)
        code, text = self.run_cli("seed", ["--status"])
        self.assertEqual(code, 1)
        self.assertIn("coverage_incomplete", text)
        self.assertIn("Stage 6: not complete", text)

    def test_cli_prints_offline_repair(self):
        self.prepare(sqlite=True)
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(self._missing_null()))
        self.assertEqual(code, 0, text)
        self.assertIn("REPAIRED offline (no LLM call)", text)
        self.assertIn("Stage 6 is COMPLETE", text)


# ------------------------------------------------------------------------------------------ semantic + multi-DBMS + history
class SemanticAndDialectTests(unittest.TestCase):
    def test_declared_mandatory_participation_is_reported_not_blocking(self):
        d = hand(LITE, tab("parents", [col("pid")], ["pid"]),
                 tab("kids", [col("kid"), col("pid")], ["kid"], [dict(fk("fk_k_p", ["pid"], "parents", ["pid"]), relationship="r1")]))
        d["conceptual"] = {"relationships": [{"id": "r1", "type": "one_to_many", "participants": [
            {"entity": "parents", "role": "", "cardinality": {"min": 1, "max": 1}},
            {"entity": "kids", "role": "", "cardinality": {"min": 1, "max": "N"}}]}]}
        res = validate(d, {"tables": {"parents": [{"pid": 1}, {"pid": 2}], "kids": [{"kid": 1, "pid": 1}]}})
        self.assertFalse(res.errors)
        self.assertEqual(len(res.semantic_unmet), 1)
        self.assertIn("`parents` row(s) have no `kids` row", res.semantic_unmet[0])
        d["conceptual"]["relationships"][0]["participants"][1]["cardinality"]["min"] = 0       # not declared -> nothing invented
        self.assertEqual(validate(d, {"tables": {"parents": [{"pid": 1}, {"pid": 2}], "kids": [{"kid": 1, "pid": 1}]}}).semantic_unmet, [])
        d.pop("conceptual")
        self.assertEqual(build(d).participation, [])

    def test_all_four_dialects_build_validate_and_render_without_sqlite_assumptions(self):
        for dialect in (LITE, PG, MY, MS):
            d = cond_design(dialect)
            m = build(d)
            res = sd.validate_records(m, accounts(("open", None), ("closed", "2024-01-01"), ("open", None)), 1)
            self.assertFalse(res.errors, (dialect, res.errors))
            sql = sd.render_seed_sql(m, res.records)
            self.assertIn("INSERT INTO", sql)
            rep = sv.validate(m, res.records, "-- schema", sql)
            if dialect == LITE:
                self.assertIn(rep["level"], ("failed", "executed"))
            else:
                self.assertEqual(rep["level"], "unverified")                            # never claimed as executed
                self.assertIn("NOT executed", rep["reason"])

    def test_prompts_cover_the_new_rules_and_stay_scenario_agnostic(self):
        text = (ROOT / "prompts" / "seed_system.md").read_text(encoding="utf-8")
        for needle in ("CONDITIONAL CHECK", "partial index", "Composite foreign keys", "internally consistent"):
            self.assertIn(needle, text)
        for word in ("library", "borrow", "audit", "book"):
            self.assertNotIn(word, text.lower())
        src = (ROOT / "dba_agent" / "seed_data.py").read_text(encoding="utf-8").lower()
        for word in ("borrowing", "audit_entries", "voided_at", "account_closed"):
            self.assertNotIn(word, src)                                                 # no scenario-specific rules in production code


class HistoricalSessionTests(unittest.TestCase):
    SESSION = ROOT / "sessions" / "20261113-192128-c94d"

    def _digest(self):
        out = {}
        for p in sorted(self.SESSION.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(self.SESSION))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def test_archived_session_is_read_only_and_its_real_gap_is_detected_precisely(self):
        if not self.SESSION.exists():
            self.skipTest("archived session not present")
        before = self._digest()
        design = json.loads((self.SESSION / "output" / "design.json").read_text(encoding="utf-8"))
        data = json.loads((self.SESSION / "output" / "seed-data.json").read_text(encoding="utf-8"))["records"]
        model = sd.build_model(design, "")                                              # malformed old index declaration: still loads
        res = sd.validate_records(model, {"tables": copy.deepcopy(data)}, 1)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.coverage, ["audit_entries.changed_fields is an optional column but every row has a value; "
                                        "include at least one row where it is NULL."])
        # conditional columns of the real run are no longer declared "not applicable"
        for name in ("account_closed_date", "return_date", "voided_at", "reason"):
            self.assertFalse(any(name in n for n in res.coverage_na), name)
        fixed = sd.repair_coverage(model, res)                                          # in memory only
        self.assertEqual(fixed.coverage, [])
        self.assertEqual(len(fixed.repairs), 1)
        self.assertEqual(self._digest(), before)                                        # nothing on disk changed


if __name__ == "__main__":
    unittest.main()
