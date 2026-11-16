"""Stage 6A: deterministic schema.sql generation, approval enforcement, regeneration/recovery.

Execution levels (kept separate on purpose):
  * SQLite  - generated SQL is REALLY EXECUTED with sqlite3 (tests marked 'executed').
  * PostgreSQL / MySQL-MariaDB / SQL Server - only STATIC checks (quoting, dialect tokens, ordering). NOT executed.
"""
import copy
import re
import sqlite3
import unittest
from unittest import mock

from dba_agent import approval, revision
from dba_agent.approval import ApprovalError
from dba_agent.schema_sql import (DIALECT_NAMES, DIALECTS, SchemaGenerationError, generate_schema_sql, quote)
from dba_agent.stages import sql_schema
from tests.test_stage5a_revision import add_weight
from tests.test_stage5b_safety import B
from tests.test_stage5a_revision import ScriptedLLM, response

PG, MY, LITE, MS = "postgresql", "mysql_mariadb", "sqlite", "sql_server"


# ----------------------------------------------------------------------------- helpers
def col(name, typ="INTEGER", nullable=False, default=None, identity=False):
    return {"name": name, "data_type": typ, "nullable": nullable, "default": default, "identity": identity,
            "source_attribute": None, "description": ""}


def fk(name, cols, ref, rcols, od="NO ACTION", ou="NO ACTION"):
    return {"name": name, "columns": cols, "ref_table": ref, "ref_columns": rcols, "on_delete": od, "on_update": ou,
            "relationship": None}


def tab(name, cols, pk, fks=(), uq=(), ck=(), ix=(), pkname=None):
    return {"name": name, "description": "", "source": {"type": "entity", "id": name}, "kind": "entity", "columns": cols,
            "primary_key": pk, "primary_key_name": pkname, "foreign_keys": list(fks),
            "unique_constraints": [{"name": n, "columns": c} for n, c in uq],
            "check_constraints": [{"name": n, "expression": e, "description": ""} for n, e in ck],
            "indexes": [{"name": n, "columns": c, "unique": u, "purpose": ""} for n, c, u in ix]}


def hand(dialect, *tables):
    return {"project_name": "Hand built", "dbms": {"id": dialect, "name": DIALECT_NAMES[dialect]},
            "physical": {"tables": list(tables)}}


def gen(dialect, *tables, **kw):
    return generate_schema_sql(hand(dialect, *tables), "f" * 64, validate=False, **kw)


def as_dialect(design, dialect):
    d = copy.deepcopy(design)
    d["dbms"] = {"id": dialect, "name": DIALECT_NAMES[dialect]}
    if dialect == MY:
        # the shared fixture's self-referencing CHECK (supervisor_id <> vet_id) uses an AUTO_INCREMENT column and a SET NULL
        # FK column, both rejected for MySQL since SQL repair B; adapt it for this dialect only
        for t in d["physical"]["tables"]:
            for c in t["check_constraints"]:
                if c["name"] == "ck_vets_not_self":
                    c["expression"] = "supervisor_id IS NULL OR supervisor_id > 0"
            for f in t["foreign_keys"]:
                if f["name"] == "fk_vets_supervisor":
                    f["on_delete"] = f["on_update"] = "RESTRICT"
    if dialect == MS:
        for t in d["physical"]["tables"]:
            for c in t["columns"]:
                c["data_type"] = {"TIMESTAMP": "DATETIME2", "TEXT": "VARCHAR(500)"}.get(c["data_type"], c["data_type"])
            for f in t["foreign_keys"]:
                if f["on_delete"] == "RESTRICT":
                    f["on_delete"] = "NO ACTION"                     # the design validator forbids RESTRICT on SQL Server
                if f["on_update"] == "RESTRICT":
                    f["on_update"] = "NO ACTION"
                if f["ref_table"] == t["name"]:
                    f["on_delete"] = f["on_update"] = "NO ACTION"    # SQL Server: no cascading self-reference
    return d


FORBIDDEN = {PG: ("AUTOINCREMENT", "AUTO_INCREMENT", "IDENTITY(", "`", "["),
             MY: ("GENERATED", "IDENTITY(", "AUTOINCREMENT", '"', "["),
             LITE: ("GENERATED", "IDENTITY(", "AUTO_INCREMENT", "`", "["),
             MS: ("GENERATED", "AUTOINCREMENT", "AUTO_INCREMENT", '"', "`", "ON DELETE RESTRICT", "ON UPDATE RESTRICT")}


def static_check(test, sql, dialect, tables):
    """Static (NOT executed) sanity checks of generated SQL."""
    code = re.sub(r"--[^\n]*", "", sql)
    for tok in FORBIDDEN[dialect]:
        test.assertNotIn(tok, code, f"{dialect}: forbidden token {tok!r}")
    test.assertEqual(code.count("("), code.count(")"))
    created = re.findall(r"CREATE TABLE (\S+) \(", code)
    test.assertEqual(len(created), len(tables))
    test.assertTrue(all(s.strip().endswith(";") for s in re.split(r"(?<=;)\n\n", code.strip()) if s.strip()))
    seen = set()
    for stmt in re.split(r"\n\n", code):
        m = re.match(r"CREATE TABLE (\S+) \(", stmt.strip())
        if m:
            own = m.group(1)
            for ref in re.findall(r"REFERENCES (\S+) ", stmt):
                test.assertTrue(ref == own or ref in seen, f"{ref} referenced before creation in {own}")
            seen.add(own)


# ----------------------------------------------------------------------------- generator, fixture design
class FixtureGenerationTests(B):
    def design(self, dialect):
        return as_dialect(self.s.data["design"], dialect)

    def test_all_four_dialects_generate_clean_static_sql(self):
        for dialect in DIALECTS:
            with self.subTest(dialect=dialect):
                res = generate_schema_sql(self.design(dialect), "a" * 64)
                static_check(self, res.sql, dialect, self.design(dialect)["physical"]["tables"])
                self.assertIn("a" * 64, res.sql)
                self.assertEqual(res.dialect, dialect)

    def test_generation_is_deterministic(self):
        for dialect in DIALECTS:
            a = generate_schema_sql(self.design(dialect), "x").sql
            b = generate_schema_sql(self.design(dialect), "x").sql
            self.assertEqual(a, b)

    def test_dependency_order_parents_first(self):
        res = generate_schema_sql(self.design(PG))
        o = res.table_order
        for child, parent in (("pets", "owners"), ("microchips", "pets"), ("appointments", "vets"),
                              ("pet_vaccinations", "vaccines"), ("pet_vaccinations", "pets")):
            self.assertLess(o.index(parent), o.index(child))
        self.assertEqual(res.deferred_foreign_keys, [])             # self-reference (vets) needs no ALTER

    def test_identity_syntax_per_dialect(self):
        sql = {d: generate_schema_sql(self.design(d)).sql for d in DIALECTS}
        self.assertIn('"owner_id" INTEGER GENERATED BY DEFAULT AS IDENTITY NOT NULL', sql[PG])
        self.assertIn("`owner_id` INTEGER NOT NULL AUTO_INCREMENT", sql[MY])
        self.assertIn('"owner_id" INTEGER NOT NULL CONSTRAINT "pk_owners" PRIMARY KEY AUTOINCREMENT', sql[LITE])
        self.assertIn("[owner_id] INTEGER IDENTITY(1,1) NOT NULL", sql[MS])

    def test_self_reference_nullable_set_null_and_actions(self):
        sql = generate_schema_sql(self.design(PG)).sql
        self.assertIn('    "supervisor_id" INTEGER,\n', sql)                                  # optional -> no NOT NULL
        self.assertIn('CONSTRAINT "fk_vets_supervisor" FOREIGN KEY ("supervisor_id") REFERENCES "vets" ("vet_id") '
                      'ON DELETE SET NULL ON UPDATE CASCADE', sql)
        self.assertIn('ON DELETE CASCADE ON UPDATE CASCADE', sql)                           # microchips -> pets

    def test_composite_pk_junction_checks_unique_indexes_present(self):
        sql = generate_schema_sql(self.design(PG)).sql
        self.assertIn('CONSTRAINT "pk_pet_vaccinations" PRIMARY KEY ("pet_id", "vaccine_id", "administered_on")', sql)
        self.assertIn("CHECK (species IN ('dog','cat','other'))", sql)
        self.assertIn('CONSTRAINT "uq_owners_email" UNIQUE ("email")', sql)
        self.assertIn('CREATE INDEX "ix_pets_owner" ON "pets" ("owner_id");', sql)

    def test_sqlserver_fixture_has_no_restrict(self):
        res = generate_schema_sql(self.design(MS))
        self.assertNotIn("RESTRICT", res.sql)
        self.assertIn("[full_name] VARCHAR(120) NOT NULL", res.sql)
        self.assertIn("[phone] VARCHAR(30) NULL", res.sql)

    def test_executed_sqlite_fixture_schema(self):
        """EXECUTED on sqlite3."""
        sql = generate_schema_sql(self.design(LITE)).sql
        con = sqlite3.connect(":memory:")
        con.executescript(sql)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"owners", "pets", "microchips", "vets", "appointments", "vaccines", "pet_vaccinations"} <= names)
        self.assertEqual([r[1] for r in con.execute("PRAGMA table_info(pet_vaccinations)") if r[5]],
                         ["pet_id", "vaccine_id", "administered_on"])
        self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        idx = {r[1] for r in con.execute("PRAGMA index_list(pets)")}
        self.assertIn("ix_pets_owner", idx)
        con.execute("INSERT INTO owners(full_name,email) VALUES('A','a@x')")          # autoincrement id
        con.execute("INSERT INTO pets(name,species,owner_id) VALUES('Rex','dog',1)")
        with self.assertRaises(sqlite3.IntegrityError):                               # CHECK
            con.execute("INSERT INTO pets(name,species,owner_id) VALUES('X','fish',1)")
        with self.assertRaises(sqlite3.IntegrityError):                               # FK
            con.execute("INSERT INTO pets(name,species,owner_id) VALUES('Y','cat',99)")
        with self.assertRaises(sqlite3.IntegrityError):                               # UNIQUE
            con.execute("INSERT INTO owners(full_name,email) VALUES('B','a@x')")
        con.execute("INSERT INTO vets(full_name,license_no) VALUES('V','L1')")        # nullable self reference
        con.execute("INSERT INTO vets(full_name,license_no,supervisor_id) VALUES('W','L2',1)")
        con.execute("DELETE FROM vets WHERE vet_id=1")                                # SET NULL
        self.assertIsNone(con.execute("SELECT supervisor_id FROM vets WHERE license_no='L2'").fetchone()[0])


# ----------------------------------------------------------------------------- generator, hand-built designs
class HandBuiltTests(unittest.TestCase):
    def composite(self, dialect):
        parent = tab("parent", [col("a"), col("b"), col("note", "VARCHAR(20)", True)], ["a", "b"])
        child = tab("child", [col("id"), col("pa"), col("pb", nullable=True)], ["id"],
                    [fk("fk_child_parent", ["pa", "pb"], "parent", ["a", "b"], "CASCADE", "NO ACTION")])
        child["columns"][2]["nullable"] = False
        return gen(dialect, child, parent)                                   # child listed first on purpose

    def test_composite_pk_and_fk_all_dialects_static(self):
        for d in DIALECTS:
            with self.subTest(dialect=d):
                res = self.composite(d)
                q = lambda n: quote(d, n)
                self.assertIn(f"FOREIGN KEY ({q('pa')}, {q('pb')}) REFERENCES {q('parent')} ({q('a')}, {q('b')})", res.sql)
                self.assertIn(f"PRIMARY KEY ({q('a')}, {q('b')})", res.sql)
                self.assertEqual(res.table_order, ["parent", "child"])

    def test_composite_fk_executed_sqlite(self):
        con = sqlite3.connect(":memory:")
        con.executescript(self.composite(LITE).sql)
        con.execute("INSERT INTO parent(a,b) VALUES (1,2)")
        con.execute("INSERT INTO child(id,pa,pb) VALUES (1,1,2)")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO child(id,pa,pb) VALUES (2,1,3)")
        con.execute("DELETE FROM parent")                                    # CASCADE both columns
        self.assertEqual(con.execute("SELECT COUNT(*) FROM child").fetchone()[0], 0)

    def circular(self, dialect):
        x = tab("x", [col("id"), col("y_id", nullable=True)], ["id"], [fk("fk_x_y", ["y_id"], "y", ["id"])])
        y = tab("y", [col("id"), col("x_id", nullable=True)], ["id"], [fk("fk_y_x", ["x_id"], "x", ["id"])])
        return gen(dialect, x, y)

    def test_circular_dependency_uses_alter_table_except_sqlite(self):
        for d in (PG, MY, MS):
            with self.subTest(dialect=d):
                res = self.circular(d)
                self.assertEqual(len(res.deferred_foreign_keys), 1)
                alter = res.sql.index("\nALTER TABLE ")
                self.assertGreater(alter, res.sql.rindex("CREATE TABLE"))
                self.assertEqual(res.sql.count("FOREIGN KEY"), 2)
        res = self.circular(LITE)
        self.assertNotIn("\nALTER TABLE ", res.sql)
        con = sqlite3.connect(":memory:")                                    # EXECUTED
        con.executescript(res.sql)
        con.execute("INSERT INTO x(id) VALUES (1)")
        con.execute("INSERT INTO y(id,x_id) VALUES (1,1)")
        con.execute("UPDATE x SET y_id=1")

    def test_reserved_words_are_quoted_and_executable(self):
        t = tab("order", [col("group", "INTEGER"), col("select", "VARCHAR(10)", True), col("n")], ["group"],
                ck=[("ck_order_n", "n > 0 AND \"group\" > 0")], ix=[("ix_order_sel", ["select"], False)])
        t2 = tab("user", [col("id"), col("order")], ["id"], [fk("fk_user_order", ["order"], "order", ["group"])])
        res = gen(LITE, t2, t)
        con = sqlite3.connect(":memory:")
        con.executescript(res.sql)
        con.execute('INSERT INTO "order"("group", n) VALUES (1, 1)')
        for d in (PG, MY, MS):
            sql = gen(d, t2, t).sql
            self.assertIn(quote(d, "order"), sql)
            self.assertNotIn(" TABLE order ", sql)

    def test_check_with_reserved_column_is_auto_quoted(self):
        t = tab("t", [col("id"), col("order", "INTEGER")], ["id"], ck=[("ck_t", "order > 0 AND 'order' <> 'x'")])
        self.assertIn('CHECK ("order" > 0 AND \'order\' <> \'x\')', gen(PG, t).sql)
        self.assertIn("CHECK (`order` > 0", gen(MY, t).sql)

    def test_defaults_per_dialect(self):
        t = tab("t", [col("id"), col("flag", "BOOLEAN", default="true"), col("c", "VARCHAR(5)", default="'x'"),
                      col("n", "INTEGER", default="0"), col("ts", "TIMESTAMP", default="now()")], ["id"])
        self.assertIn("DEFAULT TRUE", gen(PG, t).sql)
        self.assertIn("DEFAULT 1", gen(LITE, t).sql)
        tm = copy.deepcopy(t)
        tm["columns"][1]["data_type"], tm["columns"][4]["data_type"] = "BIT", "DATETIME2"
        self.assertIn("DEFAULT 1", gen(MS, tm).sql)
        self.assertIn("DEFAULT CURRENT_TIMESTAMP", gen(MY, t).sql)
        con = sqlite3.connect(":memory:")
        con.executescript(gen(LITE, t).sql)
        con.execute("INSERT INTO t(id) VALUES (1)")
        self.assertEqual(con.execute("SELECT flag,c,n FROM t").fetchone(), (1, "x", 0))
        text = tab("t", [col("id"), col("d", "TEXT", True, default="'hi'")], ["id"])
        self.assertIn("DEFAULT ('hi')", gen(MY, text).sql)

    def test_unsupported_defaults_raise(self):
        for expr in ("random()", "1 + 1", "nextval('s')"):
            with self.assertRaises(SchemaGenerationError):
                gen(PG, tab("t", [col("id"), col("x", default=expr)], ["id"]))
        with self.assertRaises(SchemaGenerationError):
            gen(LITE, tab("t", [col("id"), col("u", "TEXT", default="uuid()")], ["id"]))

    def test_identity_rules(self):
        with self.assertRaises(SchemaGenerationError):
            gen(PG, tab("t", [col("id", "VARCHAR(5)", identity=True)], ["id"]))
        with self.assertRaises(SchemaGenerationError):
            gen(LITE, tab("t", [col("id", "BIGINT", identity=True)], ["id"]))
        with self.assertRaises(SchemaGenerationError):
            gen(MY, tab("t", [col("id", identity=True), col("o")], ["o"]))
        with self.assertRaises(SchemaGenerationError):
            gen(PG, tab("t", [col("id", identity=True, default="1")], ["id"]))
        self.assertIn("BIGSERIAL", gen(PG, tab("t", [col("id", "BIGSERIAL", identity=True)], ["id"])).sql)
        self.assertNotIn("GENERATED", gen(PG, tab("t", [col("id", "BIGSERIAL", identity=True)], ["id"])).sql)

    def test_fk_action_rules(self):
        p = tab("p", [col("id", default="1")], ["id"])
        with self.assertRaises(SchemaGenerationError):                       # InnoDB has no SET DEFAULT
            gen(MY, p, tab("c", [col("id"), col("p_id", default="1")], ["id"], [fk("f", ["p_id"], "p", ["id"], "SET DEFAULT")]))
        with self.assertRaises(SchemaGenerationError):                       # SET NULL on NOT NULL column
            gen(PG, p, tab("c", [col("id"), col("p_id")], ["id"], [fk("f", ["p_id"], "p", ["id"], "SET NULL")]))
        with self.assertRaises(SchemaGenerationError):                       # SET DEFAULT without DEFAULT
            gen(PG, p, tab("c", [col("id"), col("p_id")], ["id"], [fk("f", ["p_id"], "p", ["id"], "SET DEFAULT")]))
        ok = gen(PG, p, tab("c", [col("id"), col("p_id", default="1")], ["id"], [fk("f", ["p_id"], "p", ["id"], "SET DEFAULT", "CASCADE")]))
        self.assertIn("ON DELETE SET DEFAULT ON UPDATE CASCADE", ok.sql)
        res = gen(MS, p, tab("c", [col("id"), col("p_id")], ["id"], [fk("f", ["p_id"], "p", ["id"], "RESTRICT")]))
        self.assertIn("ON DELETE NO ACTION", res.sql)
        self.assertTrue(any("RESTRICT" in n for n in res.notes))

    def test_sqlserver_cascade_rules(self):
        p = tab("p", [col("id")], ["id"])
        with self.assertRaises(SchemaGenerationError):                       # self reference with CASCADE
            gen(MS, tab("t", [col("id"), col("parent", nullable=True)], ["id"], [fk("f", ["parent"], "t", ["id"], "CASCADE")]))
        two = tab("c", [col("id"), col("a"), col("b")], ["id"],
                  [fk("f1", ["a"], "p", ["id"], "CASCADE"), fk("f2", ["b"], "p", ["id"], "CASCADE")])
        with self.assertRaises(SchemaGenerationError):                       # two cascade paths
            gen(MS, p, two)
        gen(PG, p, two)                                                      # fine elsewhere

    def test_sqlserver_nullable_unique_becomes_filtered_index(self):
        t = tab("t", [col("id"), col("email", "VARCHAR(50)", True)], ["id"], uq=[("uq_t_email", ["email"])])
        res = gen(MS, t)
        self.assertIn("CREATE UNIQUE INDEX [uq_t_email] ON [t] ([email]) WHERE [email] IS NOT NULL;", res.sql)
        self.assertNotIn("UNIQUE ([email])", res.sql)
        self.assertIn('UNIQUE ("email")', gen(PG, t).sql)

    def test_check_expression_rules(self):
        bad = ["x IN (SELECT 1)", "x > 0; DROP TABLE t", "x > 0 -- c", "(x > 0"]
        for e in bad:
            with self.assertRaises(SchemaGenerationError, msg=e):
                gen(PG, tab("t", [col("id"), col("x")], ["id"], ck=[("ck", e)]))
        for d, e in ((MY, "x::int > 0"), (MS, "LENGTH(n) > 1"), (MS, "flag = TRUE"), (PG, "LEN(n) > 1"), (LITE, "n ILIKE 'a%'")):
            with self.assertRaises(SchemaGenerationError, msg=f"{d}: {e}"):
                gen(d, tab("t", [col("id"), col("x"), col("n", "VARCHAR(5)"), col("flag", "INTEGER")], ["id"], ck=[("ck", e)]))
        gen(PG, tab("t", [col("id"), col("n", "VARCHAR(5)")], ["id"], ck=[("ck", "n <> 'a;b'")]))   # ';' inside a literal is fine

    def test_wrong_dialect_types_raise(self):
        for d, typ in ((MY, "SERIAL"), (PG, "NVARCHAR(10)"), (PG, "INTEGER UNSIGNED"), (MS, "BOOLEAN"), (MS, "TIMESTAMP"),
                       (PG, "DATETIME"), (MY, "VARCHAR(10); DROP"), (PG, "VARCHAR(abc)")):
            with self.assertRaises(SchemaGenerationError, msg=f"{d} {typ}"):
                gen(d, tab("t", [col("id"), col("x", typ)], ["id"]))
        gen(MS, tab("t", [col("id"), col("x", "VARCHAR(MAX)")], ["id"]))
        gen(MY, tab("t", [col("id", "INT UNSIGNED"), col("e", "ENUM('a','b')")], ["id"]))

    def test_key_column_type_limits(self):
        with self.assertRaises(SchemaGenerationError):
            gen(MY, tab("t", [col("id"), col("x", "TEXT")], ["id"], uq=[("u", ["x"])]))
        with self.assertRaises(SchemaGenerationError):
            gen(MY, tab("t", [col("id"), col("x", "VARCHAR(1000)")], ["id"], ix=[("i", ["x"], False)]))
        with self.assertRaises(SchemaGenerationError):
            gen(MS, tab("t", [col("id"), col("x", "VARCHAR(MAX)")], ["id"], ix=[("i", ["x"], False)]))
        gen(PG, tab("t", [col("id"), col("x", "TEXT")], ["id"], uq=[("u", ["x"])]))

    def test_name_collisions(self):
        a = tab("a", [col("id")], ["id"], ck=[("ck_same", "id > 0")])
        b = tab("b", [col("id")], ["id"], ck=[("ck_same", "id > 0")])
        for d in (MS, MY):
            with self.assertRaises(SchemaGenerationError):
                gen(d, a, b)
        gen(PG, a, b)                                                        # PostgreSQL: check names are per table
        with self.assertRaises(SchemaGenerationError):                       # index named like a table
            gen(PG, tab("a", [col("id")], ["id"], ix=[("b", ["id"], False)]), tab("b", [col("id")], ["id"]))

    def test_invalid_designs_are_refused(self):
        with self.assertRaises(SchemaGenerationError):
            generate_schema_sql({"project_name": "x", "dbms": {"id": "oracle", "name": "Oracle"}, "physical": {"tables": []}},
                                validate=False)
        with self.assertRaises(SchemaGenerationError):
            generate_schema_sql({"project_name": "x", "dbms": {"id": PG, "name": "P"}, "physical": {"tables": []}})
        with self.assertRaises(SchemaGenerationError):
            gen(PG)


# ----------------------------------------------------------------------------- stage: approval, files, recovery
class StageTests(B):
    def path(self):
        return self.s.output_dir / "schema.sql"

    def approve_and_generate(self):
        self.approve()
        return sql_schema.generate(self.s)

    def test_unapproved_design_is_rejected_and_nothing_written(self):
        with self.assertRaises(ApprovalError):
            sql_schema.generate(self.s)
        self.assertFalse(self.path().exists())
        self.assertNotIn("sql_stage", self.s.data)

    def test_approved_design_generates_and_records_fingerprint(self):
        design_before = copy.deepcopy(self.s.data["design"])
        res = self.approve_and_generate()
        self.assertTrue(res["changed"])
        rec = self.s.data["sql_stage"]["schema"]
        self.assertEqual(rec["design_fingerprint"], approval.design_fingerprint(self.s.data["design"]))
        self.assertEqual(rec["status"], "generated")
        self.assertIn(rec["design_fingerprint"], self.path().read_text())
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "current")
        self.assertEqual(self.s.data["design"], design_before)                       # the approved design is untouched
        self.assertNotIn("sql", self.s.data["completed_stages"])                     # Stage 6 stays incomplete
        self.assertFalse((self.s.output_dir / "seed.sql").exists())
        self.assertFalse(rec["executed_against_dbms"])
        self.assertEqual(self.state()["sql_stage"]["schema"]["sha256"], rec["sha256"])    # persisted

    def test_second_run_is_idempotent_and_force_rewrites(self):
        self.approve_and_generate()
        text = self.path().read_text()
        self.assertFalse(sql_schema.generate(self.s)["changed"])
        self.assertTrue(sql_schema.generate(self.s, force=True)["changed"])
        self.assertEqual(self.path().read_text(), text)

    def test_tampered_artifacts_block_generation(self):
        self.approve()
        for name in ("design.json", "logical-schema.md", "conceptual-model.md", "normalization.md", "assumptions.md",
                     "dbms-choice.md"):
            with self.subTest(file=name):
                p = self.s.output_dir / name
                original = p.read_text(encoding="utf-8")
                p.write_text(original + "\n tampered \n", encoding="utf-8")
                with self.assertRaises(ApprovalError):
                    sql_schema.generate(self.s)
                self.assertFalse(self.path().exists())
                p.write_text(original, encoding="utf-8")
        sql_schema.generate(self.s)                                                  # restored -> works

    def test_pending_revision_and_dbms_mismatch_block(self):
        self.approve()
        self.s.data["revision_pending"] = {"candidate": {}, "record": {}}
        with self.assertRaises(ApprovalError):
            sql_schema.generate(self.s)
        del self.s.data["revision_pending"]
        self.s.data["dbms_choice"]["selected_id"] = "sqlite"
        with self.assertRaises(ApprovalError):
            sql_schema.generate(self.s)

    def test_sql_comes_from_verified_design_not_markdown(self):
        self.approve()
        p = self.s.output_dir / "logical-schema.md"
        p.write_text("| fake | table |", encoding="utf-8")                           # markdown lies; generation must refuse
        with self.assertRaises(ApprovalError):
            sql_schema.generate(self.s)

    def test_revision_makes_existing_sql_stale_until_regenerated(self):
        self.approve_and_generate()
        old_fp = self.s.data["sql_stage"]["schema"]["design_fingerprint"]
        self.apply(add_weight, ["add_attribute"], request="add weight")
        st = sql_schema.schema_status(self.s)
        self.assertEqual(st["state"], "stale")
        with self.assertRaises(sql_schema.SchemaStateError):
            sql_schema.require_current_schema(self.s)
        with self.assertRaises(ApprovalError):
            sql_schema.generate(self.s)                                              # approval invalidated
        self.approve()
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "stale")         # approved again, SQL still old
        sql_schema.generate(self.s)
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "current")
        self.assertNotEqual(self.s.data["sql_stage"]["schema"]["design_fingerprint"], old_fp)
        self.assertIn("weight_kg", self.path().read_text())

    def test_modified_schema_file_is_detected_and_recoverable(self):
        self.approve_and_generate()
        self.path().write_text("-- hacked", encoding="utf-8")
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "modified")
        sql_schema.generate(self.s, force=True)
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "current")
        self.path().unlink()
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "missing_file")
        sql_schema.generate(self.s)
        self.assertEqual(sql_schema.schema_status(self.s)["state"], "current")

    def test_interrupted_generation_is_recovered(self):
        self.approve()
        with mock.patch.object(sql_schema, "_atomic_write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sql_schema.generate(self.s)
        reloaded = self.state()
        self.assertEqual(reloaded["sql_stage"]["schema"]["status"], "generating")
        self.assertFalse(self.path().exists())
        from dba_agent.state import load_session
        s2 = load_session(self.tmp.name, self.s.session_id)
        self.assertEqual(sql_schema.schema_status(s2)["state"], "incomplete")
        res = sql_schema.generate(s2)
        self.assertTrue(res["changed"])
        self.assertEqual(sql_schema.schema_status(s2)["state"], "current")
        self.assertFalse(list(self.s.output_dir.glob("*.tmp")))

    def test_translation_error_writes_nothing(self):
        self.approve()
        with mock.patch.object(sql_schema, "generate_schema_sql", side_effect=SchemaGenerationError("nope")):
            with self.assertRaises(SchemaGenerationError):
                sql_schema.generate(self.s)
        self.assertFalse(self.path().exists())
        self.assertNotIn("sql_stage", self.s.data)
        self.assertEqual(self.s.data["approval"]["status"], "approved")

    def test_cli_schema_flow(self):
        code, text = self.run_cli("schema")
        self.assertEqual(code, 2)
        self.assertIn("CANNOT GENERATE SQL", text)
        self.assertFalse(self.path().exists())
        self.approve()
        code, text = self.run_cli("schema", ["--status"])
        self.assertEqual(code, 1)
        code, text = self.run_cli("schema")
        self.assertEqual(code, 0)
        self.assertIn("Generated schema.sql for postgresql", text)
        self.assertIn("Stage 6 is NOT complete yet", text)          # Stage 6B: seed.sql still to do
        self.assertIn("Not verified", text)
        code, text = self.run_cli("schema")
        self.assertIn("already up to date", text)
        code, text = self.run_cli("schema", ["--status"])
        self.assertEqual(code, 0)
        self.path().write_text("x", encoding="utf-8")
        code, text = self.run_cli("schema", ["--status"])
        self.assertIn("modified", text)
        (self.s.output_dir / "assumptions.md").write_text("tampered", encoding="utf-8")
        code, text = self.run_cli("schema", ["--force"])
        self.assertEqual(code, 2)
        self.assertIn("assumptions.md", text)
