"""Stage 6A SQL-generation defects, verified by REALLY EXECUTING the generated SQL on SQLite (positive + negative cases).

  1. partial (conditional) unique index  - repeated historical rows allowed, a second ACTIVE row refused
  2. NULL-strict conditional CHECK        - UNKNOWN is a violation (voided record without actor/reason)
  3. structured triggers (append-only, immutable columns) + honest enforcement report
  4. split_statements keeps trigger bodies whole
  5. other dialects: refused (triggers) / static only - never executed here
"""
import copy
import json
import sqlite3
import unittest
from pathlib import Path

from dba_agent import design_model as dm
from dba_agent.errors import InvalidResponseError
from dba_agent.schema_sql import SchemaGenerationError, generate_schema_sql, is_null_aware
from dba_agent.sql_validate import split_statements
from tests.design_fixtures import fresh
from tests.test_stage6a_schema import DIALECTS, LITE, MS, MY, PG, col, gen, hand, tab

ACTIVE = [{"column": "status", "operator": "=", "value": "active"}]
VOID_CHECK = ("(status = 'voided' AND voided_at IS NOT NULL AND length(trim(voided_by_username)) > 0 "
              "AND length(trim(void_reason)) > 0) OR (status <> 'voided' AND voided_at IS NULL "
              "AND voided_by_username IS NULL AND void_reason IS NULL)")


def connect(sql):
    """Run a generated script statement by statement (as the validator does) on a fresh in-memory database."""
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in split_statements(sql):
        con.execute(stmt)
    return con


def refused(test, con, sql, *args):
    with test.assertRaises(sqlite3.IntegrityError, msg=sql):
        con.execute(sql, args)


def loans(where=ACTIVE, purpose="one active loan per copy", text="TEXT"):
    t = tab("loans", [col("id"), col("copy_id", text), col("status", text)], ["id"])
    ix = {"name": "uq_loans_active_copy", "columns": ["copy_id"], "unique": True, "purpose": purpose}
    if where:
        ix["where"] = copy.deepcopy(where)
    t["indexes"].append(ix)
    return t


# ------------------------------------------------------------------ 1. partial unique index
class PartialUniqueTests(unittest.TestCase):
    def test_conditional_unique_index_executed(self):
        sql = gen(LITE, loans()).sql
        self.assertIn('CREATE UNIQUE INDEX "uq_loans_active_copy" ON "loans" ("copy_id") WHERE "status" = \'active\';', sql)
        con = connect(sql)
        ins = "INSERT INTO loans(id, copy_id, status) VALUES (?, ?, ?)"
        for i, st in enumerate(["returned", "returned", "lost", "voided"], 1):       # history: repeats are legitimate
            con.execute(ins, (i, "C1", st))
        con.execute(ins, (10, "C1", "active"))                                       # one active loan
        con.execute(ins, (11, "C2", "active"))                                       # another copy is independent
        refused(self, con, ins, 12, "C1", "active")                                  # second active loan of C1
        con.execute("UPDATE loans SET status = 'returned' WHERE id = 10")            # release ...
        con.execute(ins, (13, "C1", "active"))                                       # ... and C1 can be loaned again
        refused(self, con, "UPDATE loans SET status = 'active' WHERE id = 1")        # reactivating an old row conflicts

    def test_unconditional_unique_would_have_been_wrong(self):
        con = connect(gen(LITE, loans(where=None, purpose="plain")).sql)             # ordinary full unique index
        con.execute("INSERT INTO loans VALUES (1, 'C1', 'returned')")
        refused(self, con, "INSERT INTO loans VALUES (2, 'C1', 'returned')")         # this is the defect being avoided

    def test_partial_in_purpose_without_where_is_refused_not_emitted_wrongly(self):
        for purpose in ("Partial unique index WHERE status = 'active'; one active loan per copy.", "conditional key"):
            with self.assertRaises(SchemaGenerationError) as cm:
                gen(LITE, loans(where=None, purpose=purpose))
            self.assertIn("uq_loans_active_copy", str(cm.exception))
            self.assertIn("where", str(cm.exception))

    def test_historical_session_design_is_refused_unchanged(self):
        files = sorted(Path(__file__).resolve().parent.parent.glob("sessions/*/output/design.json"))
        if not files:
            self.skipTest("no historical session in this checkout")
        raw = files[0].read_bytes()
        design = json.loads(raw)
        if not any(i.get("unique") and not i.get("where") and "artial" in (i.get("purpose") or "")
                   for t in design["physical"]["tables"] for i in t["indexes"]):
            self.skipTest("historical session has no description-only partial index")
        with self.assertRaises(SchemaGenerationError) as cm:
            generate_schema_sql(design, "f" * 64)
        self.assertIn("uq_borrowing_records_active_copy", str(cm.exception))
        self.assertEqual(files[0].read_bytes(), raw)                                  # the session is not modified

    def test_other_dialects(self):
        self.assertIn('WHERE "status" = \'active\';', gen(PG, loans()).sql)
        self.assertIn("WHERE [status] = 'active';", gen(MS, loans(text="VARCHAR(20)")).sql)
        with self.assertRaises(SchemaGenerationError):                                # MySQL has no partial indexes
            gen(MY, loans())


# ------------------------------------------------------------------ 2. NULL-strict conditional CHECK
def voided_table():
    cols = [col("id"), col("status", "TEXT"), col("voided_at", "TEXT", True), col("voided_by_username", "TEXT", True),
            col("void_reason", "TEXT", True), col("year", "INTEGER", True)]
    return tab("tx", cols, ["id"], ck=[("ck_tx_void_metadata", VOID_CHECK),
                                       ("ck_tx_year", "year IS NULL OR year BETWEEN 1000 AND 9999"),
                                       ("ck_tx_year_plain", "year > 0")])


class NullStrictCheckTests(unittest.TestCase):
    INS = "INSERT INTO tx(id, status, voided_at, voided_by_username, void_reason) VALUES (?, ?, ?, ?, ?)"

    def test_plain_sql_check_really_accepts_the_invalid_row(self):
        con = sqlite3.connect(":memory:")                                             # the defect, shown without the fix
        con.execute(f"CREATE TABLE tx (id INTEGER, status TEXT, voided_at TEXT, voided_by_username TEXT, "
                    f"void_reason TEXT, CHECK ({VOID_CHECK}))")
        con.execute(self.INS, (1, "voided", "2026-01-01", None, "typo"))              # NULL actor slips through

    def test_voided_record_needs_actor_and_reason_executed(self):
        con = connect(gen(LITE, voided_table()).sql)
        con.execute(self.INS, (1, "voided", "2026-01-01", "lib1", "created by mistake"))
        con.execute(self.INS, (2, "active", None, None, None))
        for i, row in enumerate([("voided", "2026-01-01", None, "typo"),              # actor missing (NULL)
                                 ("voided", "2026-01-01", "lib1", None),              # reason missing (NULL)
                                 ("voided", None, "lib1", "typo"),                    # timestamp missing
                                 ("voided", "2026-01-01", "   ", "typo"),             # blank actor
                                 ("voided", "2026-01-01", "lib1", ""),                # blank reason
                                 ("active", "2026-01-01", None, None),                # void data on a normal record
                                 ("active", None, "lib1", None)], 10):
            refused(self, con, self.INS, i, *row)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM tx").fetchone()[0], 2)

    def test_checks_without_explicit_null_tests_keep_standard_semantics(self):
        sql = gen(LITE, voided_table()).sql
        self.assertEqual(sql.count("CASE WHEN ("), 2)                                 # void_metadata + year (IS NULL OR ...)
        self.assertIn("CHECK (year > 0)", sql)                                        # NULL still passes a plain check
        con = connect(sql)
        con.execute("INSERT INTO tx(id, status, year) VALUES (1, 'active', NULL)")
        con.execute("INSERT INTO tx(id, status, year) VALUES (2, 'active', 2024)")
        refused(self, con, "INSERT INTO tx(id, status, year) VALUES (3, 'active', 12)")
        refused(self, con, "INSERT INTO tx(id, status, year) VALUES (4, 'active', -5)")
        self.assertFalse(is_null_aware("x > 0"))
        self.assertFalse(is_null_aware("x IS NOT NULL"))
        self.assertFalse(is_null_aware("note = 'a IS NULL OR b'"))                    # literals are ignored
        self.assertTrue(is_null_aware("a IS NULL OR a > 0"))

    def test_other_dialects_get_the_same_strict_form_static(self):
        for d in (PG, MY, MS):
            t = tab("tx", [col("id"), col("status", "VARCHAR(10)"), col("voided_by", "VARCHAR(20)", True)], ["id"],
                    ck=[("ck_tx_void", "(status = 'voided' AND voided_by IS NOT NULL AND voided_by <> '') OR "
                                       "(status <> 'voided' AND voided_by IS NULL)")])
            sql = gen(d, t).sql
            self.assertIn("CASE WHEN (", sql, d)
            self.assertIn("THEN 1 ELSE 0 END = 1", sql, d)
        self.assertIn("NULL-strict", " ".join(gen(LITE, voided_table()).notes))


# ------------------------------------------------------------------ 3. triggers + honest enforcement report
def audit(**trigger):
    t = tab("audit", [col("id"), col("actor", "TEXT"), col("reason", "TEXT", True)], ["id"])
    t["triggers"] = [dict({"name": "trg_audit_append_only", "kind": "append_only", "description": ""}, **trigger)]
    return t


class TriggerTests(unittest.TestCase):
    def test_append_only_executed(self):
        res = gen(LITE, audit(operations=["UPDATE", "DELETE"]))
        con = connect(res.sql)
        con.execute("INSERT INTO audit VALUES (1, 'lib', NULL)")                      # appending works
        con.execute("INSERT INTO audit VALUES (2, 'lib', 'r')")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 2)  # reading works
        for sql in ("UPDATE audit SET reason = 'x' WHERE id = 1", "UPDATE audit SET actor = actor",
                    "DELETE FROM audit WHERE id = 1", "DELETE FROM audit"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql) as cm:
                con.execute(sql)
            self.assertIn("append-only", str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError):                               # REPLACE cannot rewrite history either
            con.execute("INSERT OR REPLACE INTO audit VALUES (1, 'evil', 'x')")
        self.assertEqual(con.execute("SELECT actor FROM audit WHERE id = 1").fetchone()[0], "lib")
        self.assertIn("PRAGMA recursive_triggers = ON;", res.sql)

    def test_append_only_single_operation(self):
        con = connect(gen(LITE, audit(operations=["DELETE"])).sql)
        con.execute("INSERT INTO audit VALUES (1, 'lib', NULL)")
        con.execute("UPDATE audit SET reason = 'allowed' WHERE id = 1")               # only DELETE is blocked
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("DELETE FROM audit")

    def test_immutable_columns_executed(self):
        t = tab("acct", [col("id"), col("owner", "TEXT"), col("note", "TEXT", True), col("closed", "TEXT", True)], ["id"])
        t["triggers"] = [{"name": "trg_acct_owner_fixed", "kind": "immutable_columns", "columns": ["owner", "closed"],
                          "description": ""}]
        con = connect(gen(LITE, t).sql)
        con.execute("INSERT INTO acct VALUES (1, 'ann', NULL, NULL)")
        con.execute("UPDATE acct SET note = 'free to change' WHERE id = 1")
        con.execute("UPDATE acct SET owner = 'ann' WHERE id = 1")                     # same value: not a change
        refused(self, con, "UPDATE acct SET owner = 'bob' WHERE id = 1")
        refused(self, con, "UPDATE acct SET closed = '2026-01-01' WHERE id = 1")      # NULL -> value is a change
        con.execute("DELETE FROM acct WHERE id = 1")                                  # deleting is not restricted

    def test_unsupported_dialects_are_refused_not_emitted(self):
        for d in (PG, MY, MS):
            with self.assertRaises(SchemaGenerationError, msg=d) as cm:
                gen(d, audit(operations=["UPDATE", "DELETE"]))
            self.assertIn("SQLite only", str(cm.exception))

    def test_design_model_validation(self):
        conc, log = fresh()
        c = dm.validate_conceptual(conc)
        first = log["tables"][0]
        cols = [x["name"] for x in first["columns"]]
        first["triggers"] = [{"name": "trg_first_fixed", "kind": "immutable_columns", "columns": cols[:1]}]
        ok = dm.validate_physical(copy.deepcopy(log), c, "sqlite")
        self.assertEqual(ok["tables"][0]["triggers"][0]["kind"], "immutable_columns")
        self.assertEqual(dm.validate_physical(ok, c, "sqlite")["tables"][0]["triggers"], ok["tables"][0]["triggers"])  # idempotent
        self.assertNotIn("triggers", ok["tables"][1])                                  # key only present when used
        for bad, msg in (({"name": "t1", "kind": "delete_everything"}, "kind"),
                         ({"name": "t1", "kind": "immutable_columns", "columns": ["nope"]}, "unknown columns"),
                         ({"name": "t1", "kind": "immutable_columns"}, "non-empty"),
                         ({"name": "t1", "kind": "append_only", "operations": ["TRUNCATE"]}, "UPDATE and/or DELETE")):
            log2 = copy.deepcopy(log)
            log2["tables"][0]["triggers"] = [bad]
            with self.assertRaises(InvalidResponseError, msg=str(bad)) as cm:
                dm.validate_physical(log2, c, "sqlite")
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(InvalidResponseError) as cm:                           # other DBMS: told to use 'application'
            dm.validate_physical(copy.deepcopy(log), c, "postgresql")
        self.assertIn("application", str(cm.exception))

    def test_enforcement_report_never_claims_unimplemented_rules(self):
        rules = [{"id": "audit_is_append_only", "description": "x",
                  "enforcement": {"mechanism": "trigger", "implemented_by": ["trg_audit_append_only"], "note": "n"}},
                 {"id": "claimed_trigger_but_none", "description": "x",
                  "enforcement": {"mechanism": "trigger", "implemented_by": [], "note": "n"}},
                 {"id": "trigger_names_wrong_object", "description": "x",
                  "enforcement": {"mechanism": "trigger", "implemented_by": ["ck_other"], "note": "n"}},
                 {"id": "needs_app", "description": "x",
                  "enforcement": {"mechanism": "application", "implemented_by": [], "note": "n"}},
                 {"id": "has_constraint", "description": "x",
                  "enforcement": {"mechanism": "schema", "implemented_by": ["pk_audit"], "note": ""}}]
        d = hand(LITE, audit(operations=["UPDATE", "DELETE"]))
        d["business_rules"] = rules
        res = generate_schema_sql(d, "f" * 64, validate=False)
        enforced = [x["rule_id"] for x in res.enforcement["database"]]
        self.assertEqual(enforced, ["audit_is_append_only", "has_constraint"])
        self.assertEqual([x["rule_id"] for x in res.enforcement["application"]],
                         ["claimed_trigger_but_none", "trigger_names_wrong_object", "needs_app"])
        head = res.sql.split("\nPRAGMA foreign_keys = ON;")[0]
        enf_line = next(l for l in head.splitlines() if "ENFORCED by this script" in l and "NOT" not in l)
        app_text = " ".join(head.split("NOT enforced by this script")[1].replace("--", " ").split())   # undo comment wrapping
        self.assertIn("audit_is_append_only", enf_line)
        for rid in ("claimed_trigger_but_none", "trigger_names_wrong_object", "needs_app"):
            self.assertNotIn(rid, enf_line)
            self.assertIn(rid, app_text)
        self.assertIn("declared as trigger, but no trigger was generated", app_text)
        self.assertEqual(res.sql, generate_schema_sql(d, "f" * 64, validate=False).sql)    # deterministic


# ------------------------------------------------------------------ 4. statement splitting with triggers
class SplitTests(unittest.TestCase):
    def test_generated_script_splits_into_whole_statements(self):
        t = audit(operations=["UPDATE", "DELETE"])
        t["triggers"].append({"name": "trg_audit_fixed", "kind": "immutable_columns", "columns": ["actor"], "description": ""})
        stmts = split_statements(gen(LITE, t).sql)
        triggers = [s for s in stmts if s.startswith("CREATE TRIGGER")]
        self.assertEqual(len(triggers), 5)           # update, delete, insert guard + immutable + its insert guard
        for s in triggers:
            self.assertTrue(s.rstrip().endswith("END"), s)                            # body (with inner ';') not cut
            self.assertIn("RAISE(ABORT", s)
        self.assertEqual(stmts[0], "PRAGMA foreign_keys = ON")
        self.assertEqual(sum(s.startswith("CREATE TABLE") for s in stmts), 1)

    def test_trigger_body_edge_cases(self):
        sql = ("CREATE TRIGGER \"t;1\" BEFORE UPDATE ON a WHEN CASE WHEN OLD.x > 1 THEN 1 ELSE 0 END = 1\n"
               "BEGIN\n  SELECT RAISE(ABORT, 'no; end; begin');\n  SELECT CASE WHEN 1 THEN 2 END;\nEND;\n"
               "INSERT INTO a VALUES ('end;');\nBEGIN;\nCOMMIT;\n-- create trigger x begin;\nSELECT 1;")
        out = split_statements(sql)
        self.assertEqual(len(out), 5, out)
        self.assertTrue(out[0].startswith("CREATE TRIGGER") and out[0].endswith("END"))
        self.assertEqual(out[1:], ["INSERT INTO a VALUES ('end;')", "BEGIN", "COMMIT", "SELECT 1"])

    def test_executable_through_the_validator_path(self):
        from dba_agent.sql_validate import _run_sqlite
        con = sqlite3.connect(":memory:", isolation_level=None)
        self.assertIsNone(_run_sqlite(con, gen(LITE, audit(operations=["UPDATE", "DELETE"])).sql, "schema.sql"))
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        self.assertEqual(names, {"trg_audit_append_only_update", "trg_audit_append_only_delete",
                                 "trg_audit_append_only_insert_conflict"})


if __name__ == "__main__":
    unittest.main()
