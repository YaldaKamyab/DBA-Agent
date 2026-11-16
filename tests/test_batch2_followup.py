"""Batch 2 follow-up regressions (offline; only SQLite is EXECUTED - the other dialects are checked statically).

  1. append-only: INSERT OR REPLACE on a NEW connection with recursive_triggers = 0 must not overwrite history
  2. immutable columns: INSERT OR REPLACE must not change them (DELETE + INSERT stays allowed - documented)
  3. enforcement report verified against generated objects (all four dialects, static)
  4. unconditional UNIQUE index whose prose mentions WHERE is NOT refused; structured "conditional" flag
  5. multi-DBMS: triggers still refused outside SQLite, quoting / types / partial-index rendering per dialect
PostgreSQL, MySQL/MariaDB and SQL Server SQL is NEVER executed here: assertions on it are static string checks.
"""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dba_agent import design_model as dm
from dba_agent.errors import InvalidResponseError
from dba_agent.schema_sql import SchemaGenerationError, generate_schema_sql
from dba_agent.sql_validate import split_statements
from tests.design_fixtures import fresh
from tests.test_stage6a_schema import LITE, MS, MY, PG, col, gen, hand, tab

ALL = (PG, MY, LITE, MS)
ACTIVE = [{"column": "status", "operator": "=", "value": "active"}]


def build(path, sql):
    con = sqlite3.connect(path, isolation_level=None)
    for stmt in split_statements(sql):
        con.execute(stmt)
    con.close()


def reopen(path):
    """An ORDINARY new connection: no PRAGMA set by the script survives (recursive_triggers = 0)."""
    con = sqlite3.connect(path, isolation_level=None)
    assert con.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    return con


def audit(operations=("UPDATE", "DELETE"), extra_cols=(), uq=(), ix=()):
    t = tab("audit", [col("id"), col("actor", "TEXT"), col("reason", "TEXT", True), *extra_cols], ["id"], uq=uq, ix=ix)
    t["triggers"] = [{"name": "trg_audit", "kind": "append_only", "description": "", "operations": list(operations)}]
    return t


def acct():
    t = tab("acct", [col("id"), col("owner", "VARCHAR(50)"), col("email", "VARCHAR(50)", True), col("note", "TEXT", True)], ["id"],
            uq=[("uq_acct_email", ["email"])])
    t["triggers"] = [{"name": "trg_acct_owner", "kind": "immutable_columns", "columns": ["owner"], "description": ""}]
    return t


class Persistent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "db.sqlite")
        self.addCleanup(self.tmp.cleanup)

    def denied(self, con, sql, *args):
        with self.assertRaises(sqlite3.IntegrityError, msg=sql):
            con.execute(sql, args)


# ------------------------------------------------------------------ 1. append-only bypass
class AppendOnlyReplace(Persistent):
    def test_replace_on_new_ordinary_connection(self):
        build(self.path, gen(LITE, audit()).sql)
        con = sqlite3.connect(self.path, isolation_level=None)
        con.execute("INSERT INTO audit VALUES (1, 'lib', 'original')")
        con.close()
        con = reopen(self.path)                                                   # recursive_triggers = 0
        for sql in ("INSERT OR REPLACE INTO audit VALUES (1, 'evil', 'x')", "REPLACE INTO audit VALUES (1, 'evil', 'x')",
                    "INSERT OR REPLACE INTO audit VALUES ('1', 'evil', 'x')",      # text key, integer affinity
                    "INSERT INTO audit VALUES (1, 'evil', 'x') ON CONFLICT(id) DO UPDATE SET actor = 'evil'",
                    "INSERT OR IGNORE INTO audit VALUES (1, 'evil', 'x')"):         # stricter than SQLite: raises
            self.denied(con, sql)
        self.denied(con, "UPDATE audit SET actor = 'evil'")
        self.denied(con, "DELETE FROM audit")
        self.assertEqual(con.execute("SELECT actor, reason FROM audit").fetchall(), [("lib", "original")])
        con.execute("INSERT INTO audit VALUES (2, 'lib', NULL)")                  # appending still works
        self.assertEqual(con.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 2)

    def test_other_keys_unique_constraint_partial_index_and_rowid(self):
        extra = [col("code", "TEXT", True), col("state", "TEXT", True)]
        t = audit(extra_cols=extra, uq=[("uq_audit_code", ["code"])],
                  ix=[("uq_audit_open", ["actor"], True)])
        t["indexes"][0]["where"] = [{"column": "state", "operator": "=", "value": "open"}]
        t["primary_key"] = ["id"]
        build(self.path, gen(LITE, t).sql)
        con = reopen(self.path)
        con.execute("INSERT INTO audit VALUES (1, 'a', 'r', 'C1', 'open')")
        con.execute("INSERT INTO audit VALUES (2, 'b', 'r', NULL, 'closed')")
        con.execute("INSERT INTO audit VALUES (3, 'b', 'r', NULL, 'closed')")      # NULL keys / closed: no conflict, fine
        self.denied(con, "INSERT OR REPLACE INTO audit VALUES (9, 'z', 'r', 'C1', 'closed')")   # replaces row 1 via UNIQUE
        self.denied(con, "INSERT OR REPLACE INTO audit VALUES (9, 'a', 'r', 'C9', 'open')")     # via the partial unique index
        con.execute("INSERT INTO audit VALUES (9, 'a', 'r', 'C9', 'closed')")      # same actor but not 'open': legitimate
        self.assertEqual(con.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 4)
        self.assertEqual(con.execute("SELECT actor FROM audit WHERE id = 1").fetchone()[0], "a")

    def test_rowid_of_table_without_integer_key(self):
        t = tab("evt", [col("code", "TEXT"), col("body", "TEXT", True)], ["code"])
        t["triggers"] = [{"name": "trg_evt", "kind": "append_only", "description": "", "operations": ["UPDATE", "DELETE"]}]
        build(self.path, gen(LITE, t).sql)
        con = reopen(self.path)
        con.execute("INSERT INTO evt(rowid, code, body) VALUES (5, 'a', 'orig')")
        self.denied(con, "INSERT OR REPLACE INTO evt(rowid, code, body) VALUES (5, 'b', 'evil')")
        self.denied(con, "INSERT OR REPLACE INTO evt(code, body) VALUES ('a', 'evil')")
        con.execute("INSERT INTO evt(code, body) VALUES ('b', 'new')")
        self.assertEqual(con.execute("SELECT body FROM evt WHERE code = 'a'").fetchone()[0], "orig")

    def test_delete_only_table_blocks_update_or_replace(self):
        build(self.path, gen(LITE, audit(operations=["DELETE"])).sql)
        con = reopen(self.path)
        con.execute("INSERT INTO audit VALUES (1, 'a', NULL)")
        con.execute("INSERT INTO audit VALUES (2, 'b', NULL)")
        con.execute("UPDATE audit SET reason = 'allowed' WHERE id = 1")           # UPDATE itself is permitted
        self.denied(con, "UPDATE OR REPLACE audit SET id = 2 WHERE id = 1")       # ... but not deleting row 2 through it
        self.denied(con, "INSERT OR REPLACE INTO audit VALUES (2, 'evil', NULL)")
        self.denied(con, "DELETE FROM audit WHERE id = 2")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 2)

    def test_update_only_table_documents_delete_and_reinsert(self):
        res = gen(LITE, audit(operations=["UPDATE"]))
        self.assertNotIn("insert_conflict", res.sql)          # DELETE is intentionally allowed => replacement is too
        self.assertIn("intentionally allows delete-and-reinsert", " ".join(res.notes))

    def test_limits_are_reported_not_hidden(self):
        res = gen(LITE, audit())
        text = " ".join(res.notes)
        for needle in ("does not depend on a connection setting", "DROP TRIGGER", "writable_schema", "replacing the file"):
            self.assertIn(needle, text)


# ------------------------------------------------------------------ 2. immutable columns bypass
class ImmutableReplace(Persistent):
    def test_replace_cannot_change_immutable_column(self):
        res = gen(LITE, acct())
        con = sqlite3.connect(":memory:", isolation_level=None)                    # the ORIGINAL connection
        for stmt in split_statements(res.sql):
            con.execute(stmt)
        con.execute("INSERT INTO acct VALUES (1, 'Alice', 'a@x', NULL)")
        self.denied(con, "INSERT OR REPLACE INTO acct VALUES (1, 'Bob', 'a@x', NULL)")
        self.denied(con, "INSERT OR REPLACE INTO acct VALUES (2, 'Bob', 'a@x', NULL)")      # via the UNIQUE email key
        self.denied(con, "UPDATE acct SET owner = 'Bob'")
        con.execute("INSERT OR REPLACE INTO acct VALUES (1, 'Alice', 'a@x', 'note changed')")  # immutable value kept
        self.assertEqual(con.execute("SELECT owner, note FROM acct").fetchall(), [("Alice", "note changed")])

    def test_same_on_new_connection_and_documented_delete_insert(self):
        build(self.path, gen(LITE, acct()).sql)
        con = reopen(self.path)
        con.execute("INSERT INTO acct VALUES (1, 'Alice', NULL, NULL)")
        self.denied(con, "REPLACE INTO acct VALUES (1, 'Bob', NULL, NULL)")
        con.execute("DELETE FROM acct WHERE id = 1")                               # permitted: limitation, not a promise
        con.execute("INSERT INTO acct VALUES (1, 'Bob', NULL, NULL)")
        self.assertIn("DELETE followed by INSERT", " ".join(gen(LITE, acct()).notes))


# ------------------------------------------------------------------ 3. enforcement report
def rule(rid, mech, impl):
    return {"id": rid, "description": "x", "enforcement": {"mechanism": mech, "implemented_by": impl, "note": "n"}}


def report(dialect, rules, *tables):
    d = hand(dialect, *tables)
    d["business_rules"] = rules
    return generate_schema_sql(d, "f" * 64, validate=False)


def base_table():
    t = tab("loan", [col("id"), col("copy_id", "VARCHAR(20)"), col("note", "VARCHAR(20)", True),
                     col("status", "VARCHAR(20)")], ["id"],
            uq=[("uq_loan_copy", ["copy_id"])], ck=[("ck_loan_status", "status IN ('a','b')")],
            ix=[("ix_loan_note", ["note"], False)])
    return t


class EnforcementReport(unittest.TestCase):
    RULES = [rule("r_real_unique", "schema", ["uq_loan_copy"]),
             rule("r_real_check", "schema", ["ck_loan_status"]),
             rule("r_pk", "schema", ["pk_loan"]),
             rule("r_not_null_col", "schema", ["loan.copy_id"]),
             rule("r_ghost", "schema", ["ck_does_not_exist"]),
             rule("r_empty", "schema", []),
             rule("r_plain_index", "schema", ["ix_loan_note"]),
             rule("r_nullable_col", "schema", ["loan.note"]),
             rule("r_mixed", "schema", ["uq_loan_copy", "ck_ghost"]),
             rule("r_trigger_none", "trigger", ["ck_loan_status"]),
             rule("r_app", "application", [])]

    def test_all_dialects(self):
        for d in ALL:
            res = report(d, self.RULES, base_table())
            enf = res.enforcement
            self.assertEqual([x["rule_id"] for x in enf["database"]],
                             ["r_real_unique", "r_real_check", "r_pk", "r_not_null_col"], d)
            self.assertEqual([x["rule_id"] for x in enf["unverified"]],
                             ["r_ghost", "r_empty", "r_plain_index", "r_nullable_col", "r_mixed"], d)
            self.assertEqual([x["rule_id"] for x in enf["application"]], ["r_trigger_none", "r_app"], d)
            reasons = {x["rule_id"]: x["reason"] for x in enf["unverified"]}
            self.assertIn("not found among the generated objects", reasons["r_ghost"])
            self.assertIn("non-unique index", reasons["r_plain_index"])
            self.assertIn("nullable column", reasons["r_nullable_col"])
            for x in enf["database"]:
                self.assertIn("semantic coverage", x["verified"])                  # never claims full meaning
            head = res.sql.split("CREATE TABLE")[0]
            self.assertIn("could NOT be fully verified", head)
            enf_line = next(l for l in head.splitlines() if "ENFORCED by this script" in l and "NOT" not in l)
            self.assertIn("r_real_unique", enf_line)
            self.assertNotIn("r_ghost", enf_line)

    def test_conditional_caveats_per_dialect(self):
        rules = [rule("r_fk", "schema", ["fk_c_p"]), rule("r_ck", "schema", ["ck_c"])]
        for d in ALL:
            parent = tab("p", [col("id")], ["id"])
            child = tab("c", [col("id"), col("p_id")], ["id"], fks=[{"name": "fk_c_p", "columns": ["p_id"], "ref_table": "p",
                        "ref_columns": ["id"], "on_delete": "NO ACTION", "on_update": "NO ACTION", "relationship": None}],
                        ck=[("ck_c", "id > 0")])
            cav = " ".join(c for x in report(d, rules, parent, child).enforcement["database"] for c in x["caveats"])
            self.assertEqual("PRAGMA foreign_keys = ON" in cav, d == LITE, d)
            self.assertEqual("MySQL 8.0.16" in cav, d == MY, d)

    def test_trigger_mapping_checks_operations_and_objects(self):
        rules = [rule("full", "trigger", ["trg_audit"]), rule("by_object_name", "trigger", ["trg_audit_delete"]),
                 rule("ghost_trigger", "trigger", ["trg_nope"])]
        enf = report(LITE, rules, audit()).enforcement
        self.assertEqual([x["rule_id"] for x in enf["database"]], ["full", "by_object_name"])
        self.assertEqual([x["rule_id"] for x in enf["application"]], ["ghost_trigger"])
        part = report(LITE, [rule("only_delete", "trigger", ["trg_audit"])], audit(operations=["DELETE"])).enforcement
        self.assertEqual(part["database"], [])
        self.assertIn("blocks only DELETE", part["unverified"][0]["reason"])
        imm = report(LITE, [rule("imm", "trigger", ["trg_acct_owner"])], acct()).enforcement["database"][0]
        self.assertIn("DELETE followed by INSERT", " ".join(imm["caveats"]))
        for d in (PG, MY, MS):                                                     # never generated => never claimed
            enf = report(d, [rule("t", "trigger", ["trg_x"])], tab("p", [col("id")], ["id"])).enforcement
            self.assertEqual(enf["application"][0]["reason"], "declared as trigger, but no trigger was generated")

    def test_partial_index_and_deterministic(self):
        t = tab("loans", [col("id"), col("copy_id", "VARCHAR(10)"), col("status", "VARCHAR(10)")], ["id"],
                ix=[("uq_loans_active", ["copy_id"], True)])
        t["indexes"][0]["where"] = copy.deepcopy(ACTIVE)
        for d in (PG, LITE, MS):
            res = report(d, [rule("one_active", "schema", ["uq_loans_active"])], t)
            self.assertIn("unique only for rows where status = 'active'", res.enforcement["database"][0]["caveats"][0])
            self.assertEqual(res.sql, report(d, [rule("one_active", "schema", ["uq_loans_active"])], t).sql)


# ------------------------------------------------------------------ 4. partial-index detection
def uidx(purpose="", **flags):
    t = tab("loans", [col("id"), col("copy_id", "VARCHAR(20)"), col("status", "VARCHAR(20)")], ["id"],
            ix=[("uq_loans_copy", ["copy_id"], True)])
    t["indexes"][0]["purpose"] = purpose
    t["indexes"][0].update(flags)
    return t


class PartialHint(unittest.TestCase):
    PROSE = "Unique identity index; used in queries WHERE status is active."

    def test_prose_with_where_is_not_proof_of_conditional_uniqueness(self):
        for d in ALL:
            sql = gen(d, uidx(self.PROSE)).sql
            self.assertIn("CREATE UNIQUE INDEX", sql, d)
            self.assertNotIn("WHERE", sql.split("CREATE TABLE")[1].split("CREATE UNIQUE INDEX")[1], d)   # unconditional
        con = sqlite3.connect(":memory:", isolation_level=None)
        for stmt in split_statements(gen(LITE, uidx(self.PROSE)).sql):
            con.execute(stmt)
        con.execute("INSERT INTO loans VALUES (1, 'C1', 'returned')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO loans VALUES (2, 'C1', 'returned')")          # truly unconditional

    def test_known_missing_where_defect_still_refused(self):
        for d in ALL:
            for purpose in ("Partial unique index WHERE status = 'active'; one active loan per copy.", "conditional key",
                            "Filtered unique index for open loans", "unique only when status is active"):
                with self.assertRaises(SchemaGenerationError, msg=f"{d}: {purpose}") as cm:
                    gen(d, uidx(purpose))
                self.assertIn("uq_loans_copy", str(cm.exception))
                self.assertIn("conditional", str(cm.exception))

    def test_structured_flag_decides(self):
        for d in ALL:
            with self.assertRaises(SchemaGenerationError, msg=d):                  # flag true, no where: refused
                gen(d, uidx("plain description", conditional=True))
            gen(d, uidx("Partial unique index (just words)", conditional=False))   # flag false overrides legacy prose
        t = uidx("one active loan per copy", conditional=True)
        t["indexes"][0]["where"] = copy.deepcopy(ACTIVE)
        self.assertIn('WHERE "status" = \'active\';', gen(PG, t).sql)
        self.assertIn('WHERE "status" = \'active\';', gen(LITE, t).sql)
        self.assertIn("WHERE [status] = 'active';", gen(MS, t).sql)
        with self.assertRaises(SchemaGenerationError):                              # MySQL/MariaDB has no partial index
            gen(MY, t)

    def test_design_model_accepts_flag_and_stays_byte_identical_without_it(self):
        conc, log = fresh()
        c = dm.validate_conceptual(conc)
        ok = dm.validate_physical(copy.deepcopy(log), c, "sqlite")
        self.assertTrue(all("conditional" not in i for t in ok["tables"] for i in t["indexes"]))
        first = log["tables"][0]
        first["indexes"] = [{"name": "ix_flag_x", "columns": [first["columns"][0]["name"]], "unique": False,
                             "purpose": "q WHERE x", "conditional": "false"}]
        out = dm.validate_physical(copy.deepcopy(log), c, "sqlite")
        self.assertIs(out["tables"][0]["indexes"][0]["conditional"], False)
        self.assertEqual(dm.validate_physical(copy.deepcopy(out), c, "sqlite")["tables"][0]["indexes"][0]["conditional"], False)
        for flag, where, msg in ((True, None, "has no structured 'where'"), (False, ACTIVE, "conditional\": false"),
                                 ("maybe", None, "true or false")):
            bad = copy.deepcopy(log)
            ix = bad["tables"][0]["indexes"][0]
            ix["conditional"] = flag
            if where:
                ix["where"] = where
            with self.assertRaises(InvalidResponseError) as cm:
                dm.validate_physical(bad, c, "sqlite")
            self.assertIn(msg, str(cm.exception))


# ------------------------------------------------------------------ 5. multi-DBMS compatibility (static)
class MultiDbms(unittest.TestCase):
    def test_triggers_refused_outside_sqlite_with_application_guidance(self):
        for d in (PG, MY, MS):
            for t in (audit(), acct()):
                with self.assertRaises(SchemaGenerationError, msg=d) as cm:
                    gen(d, t)
                self.assertIn("SQLite only", str(cm.exception))
                self.assertIn("application", str(cm.exception))
        conc, log = fresh()
        c = dm.validate_conceptual(conc)
        log["tables"][0]["triggers"] = [{"name": "trg_t", "kind": "append_only"}]
        dm.validate_physical(copy.deepcopy(log), c, "sqlite")
        for d in ("postgresql", "mysql_mariadb", "sql_server"):
            with self.assertRaises(InvalidResponseError, msg=d) as cm:
                dm.validate_physical(copy.deepcopy(log), c, d)
            self.assertIn("application", str(cm.exception))

    def test_identifier_quoting_and_types_per_dialect(self):
        t = tab("order", [col("id", "INTEGER", identity=True), col("group", "VARCHAR(20)"), col("total", "DECIMAL(10,2)", True)],
                ["id"], ix=[("ix_order_group", ["group"], False)])
        quote = {PG: '"order"', MY: "`order`", LITE: '"order"', MS: "[order]"}
        ident = {PG: "GENERATED BY DEFAULT AS IDENTITY", MY: "AUTO_INCREMENT", LITE: "AUTOINCREMENT", MS: "IDENTITY(1,1)"}
        for d in ALL:
            tt = copy.deepcopy(t)
            if d != LITE:
                tt["columns"][0]["data_type"] = "BIGINT" if d != MY else "INT"
            sql = gen(d, tt).sql
            self.assertIn("CREATE TABLE " + quote[d], sql, d)
            self.assertIn(ident[d], sql, d)
            self.assertIn("DECIMAL(10,2)", sql, d)
            self.assertNotIn("recursive_triggers", sql, d)                          # SQLite-only pragma never leaks
            self.assertEqual("PRAGMA" in sql, d == LITE, d)
        with self.assertRaises(SchemaGenerationError):
            gen(PG, tab("t", [col("id", "TINYINT")], ["id"]))                       # type mapping stays dialect-specific

    def test_non_trigger_output_has_no_sqlite_guards(self):
        for d in (PG, MY, MS):
            sql = gen(d, base_table()).sql
            self.assertNotIn("CREATE TRIGGER", sql)
            self.assertNotIn("rowid", sql)


if __name__ == "__main__":
    unittest.main()
