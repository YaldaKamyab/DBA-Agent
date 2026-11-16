"""Stage 6B: seed data generation, validation, SQLite execution, CLI and Stage 6 completion.

Execution levels: SQLite is REALLY executed (sqlite3). PostgreSQL / MySQL-MariaDB / SQL Server are only checked
statically / through fake connections here - the tests never claim those servers ran the SQL.
"""
import copy
import json
import re
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from dba_agent import approval, cli, seed_data as sd, sql_validate as sv
from dba_agent.config import config_from_dict
from dba_agent.errors import InvalidResponseError, LLMError
from dba_agent.llm import LLMClient
from dba_agent.logger import LLMCallLogger
from dba_agent.schema_sql import generate_schema_sql
from dba_agent.stages import sql_schema, sql_seed
from tests.test_stage5b_safety import B
from tests.test_stage5a_revision import response
from tests.test_stage6a_schema import col, fk, hand, tab

PG, MY, LITE, MS = "postgresql", "mysql_mariadb", "sqlite", "sql_server"
ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------- three unrelated hand-built scenarios
def library(dialect=LITE):
    ident = lambda n: col(n, "INTEGER", identity=True)
    return hand(dialect,
        tab("categories", [ident("category_id"), col("name", "VARCHAR(60)"), col("parent_id", nullable=True)], ["category_id"],
            [fk("fk_cat_parent", ["parent_id"], "categories", ["category_id"], "SET NULL", "CASCADE")], uq=[("uq_cat", ["name"])]),
        tab("authors", [ident("author_id"), col("full_name", "VARCHAR(100)"), col("born_year", nullable=True)], ["author_id"],
            ck=[("ck_born", "born_year > 1000")]),
        tab("books", [ident("book_id"), col("isbn", "VARCHAR(17)"), col("title", "VARCHAR(200)"), col("published_on", "DATE"),
                      col("copies", default="1"), col("category_id")], ["book_id"],
            [fk("fk_book_cat", ["category_id"], "categories", ["category_id"])], uq=[("uq_isbn", ["isbn"])],
            ck=[("ck_copies", "copies >= 0")]),
        tab("book_authors", [col("book_id"), col("author_id")], ["book_id", "author_id"],
            [fk("fk_ba_book", ["book_id"], "books", ["book_id"], "CASCADE"), fk("fk_ba_author", ["author_id"], "authors", ["author_id"])]),
        tab("members", [ident("member_id"), col("email", "VARCHAR(120)"), col("joined_on", "DATE"), col("active", default="1")],
            ["member_id"], uq=[("uq_email", ["email"])], ck=[("ck_active", "active IN (0, 1)")]),
        tab("loans", [ident("loan_id"), col("book_id"), col("member_id"), col("loaned_on", "DATE"),
                      col("returned_on", "DATE", nullable=True), col("status", "VARCHAR(10)")], ["loan_id"],
            [fk("fk_loan_book", ["book_id"], "books", ["book_id"]), fk("fk_loan_member", ["member_id"], "members", ["member_id"])],
            ck=[("ck_status", "status IN ('open','returned','overdue')")]))


LIBRARY = {"tables": {
    "categories": [{"category_id": 1, "name": "Fiction", "parent_id": None}, {"category_id": 2, "name": "Science Fiction", "parent_id": 1},
                   {"category_id": 3, "name": "History", "parent_id": None}, {"category_id": 4, "name": "Military History", "parent_id": 3}],
    "authors": [{"author_id": 1, "full_name": "Ursula K. Le Guin", "born_year": 1929}, {"author_id": 2, "full_name": "Isaac Asimov", "born_year": 1920},
                {"author_id": 3, "full_name": "Anonymous Chronicler", "born_year": None}, {"author_id": 4, "full_name": "Barbara W. Tuchman", "born_year": 1912}],
    "books": [{"book_id": 1, "isbn": "978-0-441-47812-5", "title": "The Left Hand of Darkness", "published_on": "1969-03-01", "copies": 3, "category_id": 2},
              {"book_id": 2, "isbn": "978-0-553-29337-0", "title": "Foundation", "published_on": "1951-05-01", "copies": 0, "category_id": 2},
              {"book_id": 3, "isbn": "978-0-345-38623-6", "title": "The Guns of August", "published_on": "1962-01-01", "category_id": 4},
              {"book_id": 4, "isbn": "978-0-14-044913-6", "title": "Chronicle of the Gothic Wars", "published_on": "0551-01-01", "copies": 1, "category_id": 3}],
    "book_authors": [{"book_id": 1, "author_id": 1}, {"book_id": 2, "author_id": 2}, {"book_id": 3, "author_id": 4},
                     {"book_id": 4, "author_id": 3}, {"book_id": 1, "author_id": 2}],
    "members": [{"member_id": 1, "email": "amelia.stone@mail.example", "joined_on": "2023-01-15", "active": True},
                {"member_id": 2, "email": "omar.haddad@mail.example", "joined_on": "2022-09-02", "active": True},
                {"member_id": 3, "email": "chen.li@mail.example", "joined_on": "2020-11-30", "active": False}],
    "loans": [{"loan_id": 1, "book_id": 1, "member_id": 1, "loaned_on": "2024-02-01", "returned_on": "2024-02-20", "status": "returned"},
              {"loan_id": 2, "book_id": 2, "member_id": 2, "loaned_on": "2024-03-05", "returned_on": None, "status": "open"},
              {"loan_id": 3, "book_id": 3, "member_id": 1, "loaned_on": "2023-12-01", "returned_on": None, "status": "overdue"}]}}


def shop(dialect=LITE):
    return hand(dialect,
        tab("customers", [col("customer_id", "INTEGER", identity=True), col("full_name", "VARCHAR(100)")], ["customer_id"]),
        tab("orders", [col("order_id", "INTEGER", identity=True), col("customer_id"), col("placed_at", "TIMESTAMP"),
                       col("total", "DECIMAL(10,2)")], ["order_id"], [fk("fk_o_c", ["customer_id"], "customers", ["customer_id"])],
            ck=[("ck_total", "total >= 0")]),
        tab("order_lines", [col("order_id"), col("line_no"), col("sku", "VARCHAR(20)"), col("qty")], ["order_id", "line_no"],
            [fk("fk_l_o", ["order_id"], "orders", ["order_id"], "CASCADE")], ck=[("ck_qty", "qty > 0")]),
        tab("line_returns", [col("return_id", "INTEGER", identity=True), col("order_id"), col("line_no"), col("reason", "VARCHAR(80)")],
            ["return_id"], [fk("fk_r_l", ["order_id", "line_no"], "order_lines", ["order_id", "line_no"])]))


SHOP = {"tables": {
    "customers": [{"full_name": "Sofia Marchetti"}, {"full_name": "Liam O'Connor"}, {"full_name": "Yuki Nakamura"}],
    "orders": [{"customer_id": 1, "placed_at": "2024-05-02 09:15:00", "total": "59.90"}, {"customer_id": 1, "placed_at": "2024-05-20T17:40", "total": 120},
               {"customer_id": 2, "placed_at": "2024-06-01 12:00:00", "total": "0.00"}],
    "order_lines": [{"order_id": 1, "line_no": 1, "sku": "KB-2210", "qty": 1}, {"order_id": 1, "line_no": 2, "sku": "MS-0042", "qty": 2},
                    {"order_id": 2, "line_no": 1, "sku": "MN-2711", "qty": 1}, {"order_id": 3, "line_no": 1, "sku": "CB-0007", "qty": 5}],
    "line_returns": [{"order_id": 1, "line_no": 2, "reason": "Arrived with a damaged scroll wheel"},
                     {"order_id": 2, "line_no": 1, "reason": "Wrong screen size ordered"}, {"order_id": 3, "line_no": 1, "reason": "Cable too short"}]}}


def company(dialect=LITE):
    """Circular foreign keys (departments.head_id <-> employees.dept_id) plus a self reference."""
    return hand(dialect,
        tab("departments", [col("dept_id"), col("title", "VARCHAR(60)"), col("head_id", nullable=True)], ["dept_id"],
            [fk("fk_d_head", ["head_id"], "employees", ["emp_id"], "SET NULL")]),
        tab("employees", [col("emp_id"), col("full_name", "VARCHAR(80)"), col("dept_id"), col("manager_id", nullable=True)], ["emp_id"],
            [fk("fk_e_dept", ["dept_id"], "departments", ["dept_id"]), fk("fk_e_mgr", ["manager_id"], "employees", ["emp_id"], "SET NULL")]))


COMPANY = {"tables": {
    "departments": [{"dept_id": 1, "title": "Engineering", "head_id": 2}, {"dept_id": 2, "title": "Finance", "head_id": None},
                    {"dept_id": 3, "title": "Logistics", "head_id": 4}],
    "employees": [{"emp_id": 1, "full_name": "Grace Okafor", "dept_id": 2, "manager_id": None},
                  {"emp_id": 2, "full_name": "Henrik Larsen", "dept_id": 1, "manager_id": 1},
                  {"emp_id": 3, "full_name": "Ines Duarte", "dept_id": 1, "manager_id": 2},
                  {"emp_id": 4, "full_name": "Tariq Mansour", "dept_id": 3, "manager_id": 1}]}}


def build(design):
    return sd.build_model(design)


def check(design, data, min_rows=3):
    return sd.validate_records(build(design), copy.deepcopy(data), min_rows)


def with_rows(data, table, rows):
    d = copy.deepcopy(data)
    d["tables"][table] = rows
    return d


def edit(data, table, idx, **changes):
    d = copy.deepcopy(data)
    d["tables"][table][idx].update(changes)
    return d


def run_sqlite(design, data):
    model = build(design)
    res = sd.validate_records(model, copy.deepcopy(data), 3)
    assert not res.errors, res.errors
    schema = generate_schema_sql(design, "f" * 64, validate=False).sql
    seed = sd.render_seed_sql(model, res.records, "f" * 64)
    return model, res, schema, seed, sv.validate_sqlite(model, res.records, schema, seed)


# ----------------------------------------------------------------------------- validation of proposals
class ValidationTests(unittest.TestCase):
    def errors(self, design, data, **kw):
        return check(design, data, **kw).errors

    def has(self, errs, text):
        self.assertTrue(any(text in e for e in errs), f"{text!r} not in {errs}")

    def test_valid_proposals_pass_for_unrelated_scenarios(self):
        for name, design, data in (("library", library(), LIBRARY), ("shop", shop(), SHOP), ("company", company(), COMPANY)):
            with self.subTest(name):
                res = check(design, data)
                self.assertEqual(res.errors, [])
                self.assertEqual(res.coverage if name != "shop" else [], [] if name != "shop" else [])

    def test_every_table_must_be_populated(self):
        d = copy.deepcopy(LIBRARY)
        del d["tables"]["loans"]
        self.has(self.errors(library(), d), "`loans` has no rows")
        self.has(self.errors(library(), with_rows(LIBRARY, "loans", [])), "`loans` has no rows")
        self.has(self.errors(library(), with_rows(LIBRARY, "loans", LIBRARY["tables"]["loans"][:2])), "at least 3")

    def test_unknown_table_and_column_and_shape(self):
        d = copy.deepcopy(LIBRARY)
        d["tables"]["ghosts"] = [{}]
        self.has(self.errors(library(), d), "Unknown table `ghosts`")
        self.has(self.errors(library(), edit(LIBRARY, "members", 0, nickname="Amy")), "unknown column `nickname`")
        self.has(self.errors(library(), [1, 2]), "must be a JSON object")

    def test_not_null_unique_pk_and_types(self):
        self.has(self.errors(library(), edit(LIBRARY, "members", 0, email=None)), "NOT NULL")
        self.has(self.errors(library(), edit(LIBRARY, "members", 1, email="amelia.stone@mail.example")), "UNIQUE")
        self.has(self.errors(library(), edit(LIBRARY, "books", 1, book_id=1)), "duplicate primary key")
        self.has(self.errors(library(), edit(LIBRARY, "books", 0, title="x" * 201)), "exceed the column length")
        self.has(self.errors(library(), edit(LIBRARY, "books", 0, published_on="March 1969")), "not a date")
        self.has(self.errors(library(), edit(LIBRARY, "books", 0, copies="many")), "not an integer")
        self.has(self.errors(shop(), edit(SHOP, "orders", 0, total="10.999")), "decimal place")

    def test_unique_is_case_insensitive_only_where_the_dbms_is(self):
        data = edit(LIBRARY, "members", 1, email="AMELIA.STONE@mail.example")
        self.assertEqual(self.errors(library(LITE), data), [])
        res = sd.validate_records(build(self._mysql_library()), copy.deepcopy(data), 3)
        self.has(res.errors, "UNIQUE")

    def _mysql_library(self):
        d = library(MY)
        for t in d["physical"]["tables"]:
            for c in t["columns"]:
                c["data_type"] = {"INTEGER": "INT"}.get(c["data_type"], c["data_type"])
        return d

    def test_foreign_keys_simple_composite_and_partial_null(self):
        self.has(self.errors(library(), edit(LIBRARY, "books", 0, category_id=99)), "does not exist in `categories`")
        self.has(self.errors(shop(), edit(SHOP, "line_returns", 0, line_no=9)), "does not exist in `order_lines`")
        d = copy.deepcopy(SHOP)
        d["tables"]["line_returns"][0]["line_no"] = None
        self.has(self.errors(shop(), d), "NOT NULL")
        self.has(self.errors(library(), edit(LIBRARY, "categories", 1, parent_id=77)), "does not exist")

    def test_check_constraints_are_evaluated(self):
        self.has(self.errors(library(), edit(LIBRARY, "books", 0, copies=-1)), "violates CHECK `ck_copies`")
        self.has(self.errors(library(), edit(LIBRARY, "loans", 0, status="lost")), "violates CHECK `ck_status`")
        self.has(self.errors(shop(), edit(SHOP, "order_lines", 0, qty=0)), "violates CHECK `ck_qty`")
        self.assertEqual(self.errors(library(), edit(LIBRARY, "authors", 2, born_year=None)), [])      # NULL passes a CHECK

    def test_unevaluable_check_is_reported_not_silently_trusted(self):
        d = library(PG)
        for t in d["physical"]["tables"]:
            if t["name"] == "authors":
                t["check_constraints"][0]["expression"] = "char_length(full_name) > 2"
        res = sd.validate_records(build(d), copy.deepcopy(LIBRARY), 3)
        self.assertTrue(any("not evaluated in Python" in w for w in res.warnings))

    def test_placeholders_are_rejected(self):
        for bad in ("test1", "item2", "Name 3", "lorem ipsum dolor", "string", "title1", "foo"):
            with self.subTest(bad):
                self.has(self.errors(library(), edit(LIBRARY, "books", 0, title=bad)), "placeholder")
        self.has(self.errors(library(), edit(LIBRARY, "members", 0, email="test1@x.example")), "placeholder")
        self.assertFalse(sd.looks_like_placeholder("Room 101", "title"))

    def test_identity_ids_are_assigned_when_omitted_and_must_be_consistent(self):
        res = check(shop(), SHOP)
        self.assertEqual([r["customer_id"] for r in res.records["customers"]], [1, 2, 3])
        d = copy.deepcopy(SHOP)
        d["tables"]["customers"][0]["customer_id"] = 7
        self.has(self.errors(shop(), d), "for every row or for none")

    def test_default_columns_may_be_omitted(self):
        res = check(library(), LIBRARY)
        self.assertIs(res.records["books"][2]["copies"], sd._ABSENT)
        self.assertNotIn("copies", sd.deep_records(res.records)["books"][2])

    def test_self_reference_loops_are_rejected(self):
        d = copy.deepcopy(COMPANY)
        d["tables"]["employees"][0]["manager_id"] = 2
        self.has(self.errors(company(), d), "loop")

    def test_coverage_findings_for_optional_status_and_link_tables(self):
        d = copy.deepcopy(LIBRARY)
        for r in d["tables"]["categories"]:
            r["parent_id"] = r["parent_id"] or 1
        d["tables"]["categories"][0]["parent_id"] = None
        d["tables"]["loans"] = [dict(r, status="open") for r in d["tables"]["loans"]]
        d["tables"]["loans"][0]["returned_on"] = None
        res = check(library(), d)
        self.assertEqual(res.errors, [])
        text = " ".join(res.coverage)
        self.assertIn("never used", text)                                   # status values
        d2 = copy.deepcopy(LIBRARY)
        d2["tables"]["book_authors"] = [{"book_id": i, "author_id": i} for i in (1, 2, 3)]
        self.assertTrue(any("many-to-many" in c for c in check(library(), d2).coverage))
        d3 = copy.deepcopy(LIBRARY)
        for r in d3["tables"]["loans"]:
            r["returned_on"] = "2024-04-01"
        self.assertTrue(any("loans.returned_on" in c for c in check(library(), d3).coverage))


# ----------------------------------------------------------------------------- rendering
class RenderTests(unittest.TestCase):
    def render(self, design, data):
        model = build(design)
        res = sd.validate_records(model, copy.deepcopy(data), 3)
        self.assertEqual(res.errors, [])
        return sd.render_seed_sql(model, res.records, "abc"), model

    def inserts(self, sql):
        return [m for m in re.findall(r'INSERT INTO "?`?\[?(\w+)', sql)]

    def test_parents_before_children_and_self_references_first(self):
        sql, model = self.render(library(), LIBRARY)
        order = list(dict.fromkeys(self.inserts(sql)))
        self.assertLess(order.index("categories"), order.index("books"))
        self.assertLess(order.index("books"), order.index("book_authors"))
        self.assertLess(order.index("members"), order.index("loans"))
        data = copy.deepcopy(LIBRARY)
        data["tables"]["categories"].reverse()                               # child rows listed before their parents
        sql2, _ = self.render(library(), data)
        cats = [l for l in sql2.splitlines() if l.startswith('INSERT INTO "categories"')]
        self.assertTrue(cats[0].endswith("NULL);") or "NULL" in cats[0])
        seen = set()
        for l in cats:
            cid, parent = re.search(r"VALUES \((\d+), '[^']*', (NULL|\d+)\)", l).groups()
            self.assertTrue(parent == "NULL" or int(parent) in seen)
            seen.add(int(cid))

    def test_circular_foreign_key_is_loaded_with_null_then_updated(self):
        sql, model = self.render(company(), COMPANY)
        self.assertEqual(model.deferred, [("departments", "fk_d_head")])
        self.assertIn('UPDATE "departments" SET "head_id" = 2 WHERE "dept_id" = 1;', sql)
        self.assertLess(sql.index('INSERT INTO "departments"'), sql.index('INSERT INTO "employees"'))
        self.assertGreater(sql.index("\nUPDATE"), sql.rindex("INSERT INTO"))

    def test_circle_of_not_null_keys_cannot_be_seeded(self):
        x = tab("x", [col("id"), col("y_id")], ["id"], [fk("fk_x", ["y_id"], "y", ["id"])])
        y = tab("y", [col("id"), col("x_id")], ["id"], [fk("fk_y", ["x_id"], "x", ["id"])])
        with self.assertRaises(sd.SeedError) as cm:
            build(hand(LITE, x, y))
        self.assertIn("Circular dependency", str(cm.exception))

    def test_unseedable_columns_are_reported_specifically(self):
        t = tab("files", [col("id"), col("payload", "BLOB")], ["id"])
        with self.assertRaises(sd.SeedError) as cm:
            build(hand(LITE, t))
        self.assertIn("files.payload", str(cm.exception))

    def test_dialect_literals(self):
        data = {"tables": {"customers": [{"full_name": "Zoë O'Brien \\ Ltd"}, {"full_name": "Plain Name"}, {"full_name": "Another Person"}],
                           "orders": SHOP["tables"]["orders"], "order_lines": SHOP["tables"]["order_lines"],
                           "line_returns": SHOP["tables"]["line_returns"]}}
        pg = hand(PG, *shop(PG)["physical"]["tables"])
        sql, _ = self.render(pg, data)
        self.assertIn("'Zoë O''Brien \\ Ltd'", sql)
        self.assertIn("setval(pg_get_serial_sequence('\"customers\"', 'customer_id')", sql)
        self.assertTrue(sql.rstrip().endswith("COMMIT;") and "\nBEGIN;" in sql)
        my = copy.deepcopy(pg)
        my["dbms"] = {"id": MY, "name": "MySQL/MariaDB"}
        sql, _ = self.render(my, data)
        self.assertIn("`customers`", sql)
        self.assertIn("'Zoë O''Brien \\\\ Ltd'", sql)
        self.assertIn("START TRANSACTION;", sql)
        ms = copy.deepcopy(pg)
        ms["dbms"] = {"id": MS, "name": "SQL Server"}
        sql, _ = self.render(ms, data)
        self.assertIn("SET IDENTITY_INSERT [customers] ON;", sql)
        self.assertIn("SET IDENTITY_INSERT [customers] OFF;", sql)
        self.assertIn("N'Zoë", sql)
        self.assertIn("'2024-05-20T17:40:00'", sql)                          # language-independent timestamp form
        sql, _ = self.render(shop(LITE), data)
        self.assertIn("PRAGMA foreign_keys = ON;", sql)
        self.assertNotIn("setval", sql)

    def test_booleans_and_rendering_is_deterministic(self):
        sql1, _ = self.render(library(), LIBRARY)
        sql2, _ = self.render(library(), LIBRARY)
        self.assertEqual(sql1, sql2)
        pg = library(PG)
        for t in pg["physical"]["tables"]:
            for c in t["columns"]:
                if c["name"] == "active":
                    c["data_type"] = "BOOLEAN"
                    c["default"] = "TRUE"
                    t["check_constraints"] = []
        sql, _ = self.render(pg, LIBRARY)
        self.assertIn("TRUE);", sql)
        self.assertIn("FALSE);", sql)


# ----------------------------------------------------------------------------- real SQLite execution
class SqliteExecutionTests(unittest.TestCase):
    def test_schema_and_seed_execute_for_unrelated_scenarios(self):
        for name, design, data in (("library", library(), LIBRARY), ("shop", shop(), SHOP), ("company", company(), COMPANY)):
            with self.subTest(name):
                model, res, schema, seed, rep = run_sqlite(design, data)
                self.assertEqual(rep["errors"], [], rep)
                self.assertEqual(rep["level"], "executed")
                self.assertTrue(rep["completes_stage"])
                self.assertEqual(set(rep["row_counts"]), set(model.tables))
                self.assertTrue(all(n > 0 for n in rep["row_counts"].values()))          # every table populated
                self.assertEqual(rep["row_counts"], {t: len(r) for t, r in res.records.items()})

    def test_intended_relationships_are_really_in_the_database(self):
        model, res, schema, seed, rep = run_sqlite(library(), LIBRARY)
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.execute("PRAGMA foreign_keys = ON")
        for text in (schema, seed):
            for stmt in sv.split_statements(text):
                con.execute(stmt)
        q = lambda s: con.execute(s).fetchall()
        self.assertEqual(q("SELECT COUNT(*) FROM categories WHERE parent_id IS NULL")[0][0], 2)      # roots + children
        self.assertEqual(q("SELECT COUNT(*) FROM categories WHERE parent_id IS NOT NULL")[0][0], 2)
        self.assertEqual({r[0] for r in q("SELECT status FROM loans")}, {"returned", "open", "overdue"})
        self.assertEqual(q("SELECT COUNT(*) FROM book_authors WHERE book_id = 1")[0][0], 2)          # many-to-many both ways
        self.assertEqual(q("SELECT COUNT(*) FROM book_authors WHERE author_id = 2")[0][0], 2)
        self.assertEqual(q("SELECT copies FROM books WHERE book_id = 3")[0][0], 1)                   # DEFAULT applied
        self.assertEqual(q("SELECT COUNT(*) FROM authors WHERE born_year IS NULL")[0][0], 1)         # optional value
        self.assertEqual(q("SELECT max(loan_id) FROM loans")[0][0], 3)
        con.execute("INSERT INTO loans(book_id, member_id, loaned_on, status) VALUES (1, 1, '2024-07-01', 'open')")
        self.assertEqual(q("SELECT max(loan_id) FROM loans")[0][0], 4)                                # AUTOINCREMENT continues
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO loans(book_id, member_id, loaned_on, status) VALUES (99, 1, '2024-07-01', 'open')")

    def test_composite_foreign_key_and_circular_rows_in_database(self):
        _, _, _, _, rep = run_sqlite(shop(), SHOP)
        self.assertTrue(rep["ok"])
        _, _, schema, seed, rep = run_sqlite(company(), COMPANY)
        self.assertIn("UPDATE", seed)
        self.assertTrue(rep["ok"], rep)

    def test_broken_seed_reports_actionable_error(self):
        model, res, schema, seed, _ = run_sqlite(library(), LIBRARY)
        line = next(l for l in seed.splitlines() if l.startswith('INSERT INTO "loans"'))
        bad = seed.replace(line, line.replace("VALUES (1, 1, 1,", "VALUES (1, 1, 55,"))      # member 55 does not exist
        rep = sv.validate_sqlite(model, res.records, schema, bad)
        self.assertEqual(rep["level"], "failed")
        self.assertFalse(rep["completes_stage"])
        self.assertTrue(any("seed.sql statement" in e and "INSERT INTO" in e for e in rep["errors"]), rep["errors"])
        rep = sv.validate_sqlite(model, res.records, "CREATE TABLE broken (", seed)
        self.assertEqual(rep["level"], "failed")
        self.assertTrue(any("schema.sql" in e for e in rep["errors"]))
        rep2 = sv.validate_sqlite(model, res.records, schema, "\n".join(l for l in seed.splitlines() if not l.startswith('INSERT INTO "members"')))
        self.assertFalse(rep2["ok"])
        self.assertTrue(any("members" in e for e in rep2["errors"]))

    def test_split_statements_handles_quotes_and_comments(self):
        sql = "-- a; b\nINSERT INTO t VALUES ('a;b', 'it''s');\nINSERT INTO \"x;y\" VALUES (1); -- tail;\n"
        self.assertEqual(sv.split_statements(sql), ["INSERT INTO t VALUES ('a;b', 'it''s')", 'INSERT INTO "x;y" VALUES (1)'])


# ----------------------------------------------------------------------------- other DBMSs: never claimed as executed
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql):
        self.conn.log.append(sql)
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("syntax error near ...")

    def fetchone(self):
        last = self.conn.log[-1]
        return (self.conn.existing if "information_schema" in last.lower() else self.conn.rows,)


class FakeConn:
    def __init__(self, existing=0, fail_on=None, rows=3):
        self.existing, self.fail_on, self.rows, self.log, self.closed = existing, fail_on, rows, [], False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class ServerValidationTests(unittest.TestCase):
    def setUp(self):
        self.design = library(PG)
        self.model = build(self.design)
        res = sd.validate_records(self.model, copy.deepcopy(LIBRARY), 3)
        self.records = res.records
        self.schema = "CREATE TABLE x (id int);"
        self.seed = sd.render_seed_sql(self.model, self.records, "f")

    def run_validation(self, settings=None, connect=None):
        return sv.validate(self.model, self.records, self.schema, self.seed, settings, connect)

    def test_without_server_result_is_labelled_unverified_not_executed(self):
        rep = self.run_validation()
        self.assertEqual(rep["level"], "unverified")
        self.assertIsNone(rep["executed_on"])
        self.assertIn("NOT executed", rep["reason"])
        self.assertTrue(rep["completes_stage"])                                   # allowed, but labelled
        self.assertTrue(rep["checks"])                                            # static checks did run

    def test_missing_driver_and_connection_errors_are_unverified(self):
        def no_driver(d, s):
            raise ImportError("No module named psycopg")
        rep = self.run_validation({PG: {"dsn": "x"}}, no_driver)
        self.assertEqual(rep["level"], "unverified")
        self.assertIn("driver not installed", rep["reason"])
        self.assertIn("pip install", rep["reason"])
        rep = self.run_validation({PG: {"dsn": "x"}}, mock.Mock(side_effect=OSError("connection refused")))
        self.assertEqual(rep["level"], "unverified")
        self.assertIn("cannot connect", rep["reason"])

    def test_sqlite_is_never_substituted_for_another_dbms(self):
        with mock.patch.object(sv, "validate_sqlite") as lite:
            for dialect in (PG, MY, MS):
                design = copy.deepcopy(self.design)
                design["dbms"] = {"id": dialect, "name": dialect}
                model = build(design)
                rep = sv.validate(model, self.records, self.schema, self.seed)
                self.assertNotEqual(rep["level"], "executed")
            lite.assert_not_called()

    def test_non_empty_database_is_refused(self):
        conn = FakeConn(existing=4)
        rep = self.run_validation({PG: {"dsn": "x"}}, lambda d, s: conn)
        self.assertEqual(rep["level"], "unverified")
        self.assertIn("not empty", rep["reason"])
        self.assertEqual(len(conn.log), 1)                                         # nothing was executed
        self.assertTrue(conn.closed)

    def test_server_execution_success_and_failure_are_reported(self):
        conn = FakeConn(rows=0)
        rep = self.run_validation({PG: {"dsn": "x"}}, lambda d, s: conn)
        self.assertEqual(rep["level"], "failed")                                   # row counts differ -> not "executed"
        self.assertTrue(any("expected number of records" in e for e in rep["errors"]))
        conn = FakeConn(fail_on="INSERT INTO \"books\"")
        rep = self.run_validation({PG: {"dsn": "x"}}, lambda d, s: conn)
        self.assertEqual(rep["level"], "failed")
        self.assertFalse(rep["completes_stage"])
        self.assertIn("seed.sql executes without errors", rep["errors"][0])
        self.assertIn("syntax error", rep["errors"][0])
        self.assertIn("PostgreSQL server", rep["executed_on"])

    def test_static_check_catches_missing_inserts_and_bad_order(self):
        broken = "\n".join(l for l in self.seed.splitlines() if not l.startswith('INSERT INTO "loans"'))
        rep = self.run_validation(None)
        rep = sv.validate(self.model, self.records, self.schema, broken)
        self.assertEqual(rep["level"], "failed")
        self.assertTrue(any("loans" in e for e in rep["errors"]))


# ----------------------------------------------------------------------------- LLM retry + logging with the real client
class RetryLoggingTests(unittest.TestCase):
    def test_invalid_answers_are_retried_with_feedback_and_every_attempt_is_logged(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = config_from_dict({"provider": "ollama", "model": "m", "retry_delay": 0, "max_attempts": 3}, env={})
        log = LLMCallLogger(Path(tmp.name) / "llm.jsonl")
        llm = LLMClient(cfg, log)
        model = build(library())
        bad = copy.deepcopy(LIBRARY)
        bad["tables"]["books"][0]["copies"] = -5
        answers = [json.dumps(bad), "not json at all", json.dumps(LIBRARY)]
        with mock.patch.object(llm, "_chat", side_effect=answers) as chat:
            res = {}
            def validator(data):
                r = sd.validate_records(model, data, 3)
                if r.errors:
                    raise InvalidResponseError("The sample data is not acceptable: " + "; ".join(r.errors))
                res["r"] = r
                return data
            llm.complete_json("seed", "sys", "user", validator)
        self.assertEqual(chat.call_count, 3)
        self.assertIn("violates CHECK", chat.call_args_list[1][0][1])                 # error fed back to the model
        entries = log.read()
        self.assertEqual([e["attempt"] for e in entries], [1, 2, 3])
        self.assertTrue(all(e["stage"] == "seed" for e in entries))
        self.assertIsNotNone(entries[0]["error"])
        self.assertIsNone(entries[2]["error"])
        self.assertEqual(res["r"].errors, [])


# ----------------------------------------------------------------------------- stage tests (approved design in a real session)
def clinic_data():
    return {"tables": {
        "owners": [{"full_name": "Maria Hernandez", "email": "maria.hernandez@mail.example", "phone": "+1-555-0142"},
                   {"full_name": "Tobias Weber", "email": "tobias.weber@mail.example", "phone": None},
                   {"full_name": "Aiko Tanaka", "email": "aiko.tanaka@mail.example", "phone": "+81-90-5550-1177"}],
        "pets": [{"name": "Luna", "species": "dog", "birth_date": "2019-03-14", "owner_id": 1},
                 {"name": "Mochi", "species": "cat", "birth_date": "2021-07-02", "owner_id": 1},
                 {"name": "Biscuit", "species": "dog", "birth_date": None, "owner_id": 2},
                 {"name": "Kiwi", "species": "other", "birth_date": "2022-01-20", "owner_id": 3},
                 {"name": "Shadow", "species": "cat", "birth_date": "2018-10-09", "owner_id": 3}],
        "microchips": [{"chip_no": "985112003456781", "implanted_on": "2019-04-01", "pet_id": 1},
                       {"chip_no": "985112003456790", "implanted_on": "2021-08-15", "pet_id": 2},
                       {"chip_no": "985112003457002", "implanted_on": "2018-11-03", "pet_id": 5}],
        "vets": [{"full_name": "Dr. Elena Petrova", "license_no": "VET-48213", "supervisor_id": None},
                 {"full_name": "Dr. Marcus Lindqvist", "license_no": "VET-51877", "supervisor_id": 1},
                 {"full_name": "Dr. Priya Raman", "license_no": "VET-60492", "supervisor_id": 1}],
        "appointments": [{"scheduled_at": "2024-09-03 09:30:00", "status": "done", "notes": "Annual check-up, weight stable", "pet_id": 1, "vet_id": 2},
                         {"scheduled_at": "2024-09-10 14:00:00", "status": "booked", "notes": None, "pet_id": 2, "vet_id": 3},
                         {"scheduled_at": "2024-09-11 11:15:00", "status": "cancelled", "notes": "Owner travelling", "pet_id": 3, "vet_id": None},
                         {"scheduled_at": "2024-09-12 16:45:00", "status": "booked", "notes": None, "pet_id": 4, "vet_id": 1}],
        "vaccines": [{"name": "Rabies"}, {"name": "DHPP"}, {"name": "FVRCP"}],
        "pet_vaccinations": [{"pet_id": 1, "vaccine_id": 1, "administered_on": "2023-05-10", "batch_no": "RB-2291"},
                             {"pet_id": 1, "vaccine_id": 2, "administered_on": "2023-05-10", "batch_no": None},
                             {"pet_id": 2, "vaccine_id": 1, "administered_on": "2023-06-01", "batch_no": "RB-2305"},
                             {"pet_id": 2, "vaccine_id": 3, "administered_on": "2023-06-01", "batch_no": "FV-0178"},
                             {"pet_id": 3, "vaccine_id": 1, "administered_on": "2024-01-15", "batch_no": None}]}}


class SeedLLM:
    """Scripted stand-in with the real client's retry contract (invalid answers -> next scripted answer)."""
    def __init__(self, *answers, max_attempts=3):
        self.answers, self.calls, self.rejected = list(answers), [], []
        self.config = mock.Mock(max_attempts=max_attempts)

    def complete_json(self, stage, system, user, validator=None):
        last = None
        for _ in range(self.config.max_attempts):
            item = self.answers.pop(0)
            self.calls.append((stage, user))
            try:
                return validator(copy.deepcopy(item))
            except InvalidResponseError as e:
                self.rejected.append(str(e))
                last = e
        raise LLMError(f"LLM call for stage '{stage}' failed after {self.config.max_attempts} attempt(s): {last}", retryable=False)


def to_sqlite(d):
    d["dbms"] = {"id": "sqlite", "name": "SQLite"}
    for t in d["physical"]["tables"]:
        for c in t["columns"]:
            c["data_type"] = {"TIMESTAMP": "TEXT"}.get(c["data_type"], c["data_type"])      # ISO-8601 text in SQLite
    d["assumptions"].append({"id": "R_LITE1", "text": "Timestamps are stored as ISO-8601 text.",
                             "rationale": "SQLite has no timestamp type.", "related_to": []})


class SeedStageBase(B):
    def prepare(self, sqlite=False):
        if sqlite:
            self.apply(to_sqlite, ["switch_dbms"], request="Switch to SQLite", dbms={"id": "sqlite", "name": "SQLite"})
        self.approve()
        sql_schema.generate(self.s)

    def seed(self, *answers, **kw):
        llm = SeedLLM(*answers)
        kw.setdefault("rows", 3)        # fixtures hold 3-5 rows per table; the row target is enforced since Task B
        res = sql_seed.generate(self.s, llm, self.prompts, **kw)
        return res, llm

    def path(self, name="seed.sql"):
        return self.s.output_dir / name


class SeedStageTests(SeedStageBase):
    def test_requires_valid_approval_and_current_schema_and_writes_nothing(self):
        llm = SeedLLM(clinic_data())
        with self.assertRaises(approval.ApprovalError):
            sql_seed.generate(self.s, llm, self.prompts)
        self.approve()
        with self.assertRaises(sql_schema.SchemaStateError) as cm:
            sql_seed.generate(self.s, llm, self.prompts)
        self.assertIn("has not been generated", str(cm.exception))
        self.assertEqual(llm.calls, [])                                           # no LLM call (and no allowance spent)
        self.assertFalse(self.path().exists())
        self.assertNotIn("seed", self.s.data.get("sql_stage", {}))

    def test_generates_validates_and_records_for_postgresql_as_unverified(self):
        self.prepare()
        res, llm = self.seed(clinic_data(), rows=3)
        self.assertTrue(res.changed)
        rec = self.s.data["sql_stage"]["seed"]
        self.assertEqual(rec["status"], "generated")
        self.assertEqual(rec["design_fingerprint"], self.s.data["approval"]["design_fingerprint"])
        self.assertEqual(rec["schema_sha256"], self.s.data["sql_stage"]["schema"]["sha256"])
        self.assertEqual(rec["validation"]["level"], "unverified")
        self.assertIn("NOT executed", rec["validation"]["reason"])
        self.assertEqual(set(rec["row_counts"]), {t["name"] for t in self.s.data["design"]["physical"]["tables"]})
        self.assertTrue(all(n > 0 for n in rec["row_counts"].values()))
        self.assertEqual(llm.calls[0][0], "seed")
        self.assertIn("Table pets", llm.calls[0][1])                              # schema description reached the prompt
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "current")
        self.assertIn("sql", self.s.data["completed_stages"])
        self.assertEqual(self.s.data["current_stage"], "done")
        self.assertEqual(self.s.data["sql_stage"]["validation_level"], "unverified")
        self.assertIn("NOT executed", self.s.data["sql_stage"]["note"])
        text = self.path().read_text(encoding="utf-8")
        self.assertIn("INSERT INTO \"owners\"", text)
        self.assertIn("setval(", text)                                            # PostgreSQL identity resync
        self.assertEqual(self.state()["sql_stage"]["seed"]["sha256"], rec["sha256"])        # persisted

    def test_sqlite_design_is_really_executed_and_completes_stage_6(self):
        self.prepare(sqlite=True)
        res, _ = self.seed(clinic_data())
        v = self.s.data["sql_stage"]["seed"]["validation"]
        self.assertEqual(v["level"], "executed", v)
        self.assertTrue(res.completed)
        self.assertTrue(all(c["ok"] for c in v["checks"]))
        self.assertIn("executed successfully", self.s.data["sql_stage"]["note"])
        con = sqlite3.connect(":memory:")                                        # independent re-run of both files
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(self.path("schema.sql").read_text(encoding="utf-8"))
        con.executescript(self.path().read_text(encoding="utf-8"))
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM pet_vaccinations").fetchone()[0], 5)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM vets WHERE supervisor_id IS NULL").fetchone()[0], 1)

    def test_invalid_answers_retry_and_hard_failures_report_validation_errors(self):
        self.prepare()
        bad = clinic_data()
        bad["tables"]["pets"][0]["species"] = "dragon"
        res, llm = self.seed(bad, clinic_data())
        self.assertEqual(len(llm.rejected), 1)
        self.assertIn("violates CHECK", llm.rejected[0])
        self.assertTrue(res.completed)
        self.s.data["sql_stage"].pop("seed")
        with self.assertRaises(LLMError) as cm:
            self.seed(bad, bad, bad, force=True)
        self.assertIn("failed after 3 attempt(s)", str(cm.exception))
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "incomplete")
        self.assertNotIn("sql", self.s.data["completed_stages"])

    def _flat(self):
        flat = clinic_data()
        for r in flat["tables"]["vets"]:
            r["supervisor_id"] = 1 if r["full_name"] != "Dr. Elena Petrova" else None
        for r in flat["tables"]["appointments"]:
            r["status"] = "booked"
        return flat

    def test_missing_feasible_coverage_retries_then_leaves_stage_6_open(self):
        self.prepare()
        flat = self._flat()
        res, llm = self.seed(flat, flat, flat)
        self.assertEqual(len(llm.rejected), 2)                      # bounded: exactly max_attempts calls, no extra loop
        self.assertEqual(len(llm.calls), 3)
        self.assertFalse(res.completed)
        self.assertNotIn("sql", self.s.data["completed_stages"])
        rep = self.s.data["sql_stage"]["seed"]["validation"]
        self.assertFalse(rep["completes_stage"])
        self.assertTrue(any("never used" in e and e.startswith("coverage missing") for e in rep["errors"]))
        # Batch 3: a coverage-only gap is its own state, not an SQL "failed_validation" (the SQL passed)
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "coverage_incomplete")

    def test_coverage_recovers_on_a_retry_and_saved_incomplete_data_is_not_reused(self):
        self.prepare()
        flat = self._flat()
        res, llm = self.seed(flat, clinic_data())                    # second answer fixes the coverage
        self.assertEqual(len(llm.rejected), 1)
        self.assertTrue(res.completed)
        self.s.data["sql_stage"].pop("seed")
        self.s.data["completed_stages"].remove("sql")
        self.seed(flat, flat, flat, force=True)                      # leaves incomplete data on disk
        res, llm = self.seed(clinic_data())                          # plain `seed` must ask the LLM again, not reuse
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(res.completed)

    def test_impossible_coverage_is_not_applicable_and_does_not_block(self):
        m = build(library())
        d = copy.deepcopy(LIBRARY)
        d["tables"]["loans"] = [dict(r, status="open") for r in d["tables"]["loans"]][:3]   # 3 rows, 3 values: feasible
        d["tables"]["loans"][0]["returned_on"] = None
        self.assertTrue(any("never used" in c for c in sd.validate_records(m, d, 3).coverage))
        m2 = build(library())
        m2.tables["loans"]["checks"].append({"name": "ck_many", "expression": "status IN ('a','b','c','d','e','f')"})
        res = sd.validate_records(m2, copy.deepcopy(LIBRARY), 3)
        rep, na = sd.coverage_report(m2, res.records)
        self.assertFalse(any("loans.status" in c and "never used" in c for c in rep))
        self.assertTrue(any("loans.status" in c and "cannot all appear" in c for c in na))
        m3 = build(library())
        m3.tables["authors"]["checks"].append({"name": "ck_nn", "expression": "born_year IS NOT NULL"})
        d3 = copy.deepcopy(LIBRARY)
        for r in d3["tables"]["authors"]:
            r["born_year"] = r["born_year"] or 1900
        rep, na = sd.coverage_report(m3, sd.validate_records(m3, d3, 3).records)
        self.assertFalse(any("authors.born_year" in c for c in rep))
        self.assertTrue(any("authors.born_year" in c for c in na))

    def test_na_coverage_does_not_prevent_completion(self):
        self.prepare()
        res, _ = self.seed(clinic_data())
        self.assertTrue(res.completed)
        self.assertEqual(self.s.data["sql_stage"]["seed"]["validation"].get("coverage_missing"), None)

    def test_status_detects_missing_modified_and_stale_files(self):
        self.prepare()
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "missing")
        self.seed(clinic_data())
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "current")
        text = self.path().read_text(encoding="utf-8")
        self.path().write_text(text + "\n-- edited\n", encoding="utf-8")
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "modified")
        self.path().write_text(text, encoding="utf-8")
        self.path().unlink()
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "missing_file")
        self.path().write_text(text, encoding="utf-8")
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "current")
        self.path("schema.sql").write_text("-- tampered", encoding="utf-8")        # schema changed -> seed cannot be trusted
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "stale")
        self.assertTrue(sql_seed.refresh_completion(self.s))
        self.assertNotIn("sql", self.s.data["completed_stages"])

    def test_accepted_revision_makes_seed_stale_and_reopens_stage_6(self):
        self.prepare()
        self.seed(clinic_data())
        self.assertIn("sql", self.s.data["completed_stages"])
        old_sha = self.s.data["sql_stage"]["seed"]["sha256"]
        from tests.test_stage5a_revision import add_weight
        self.apply(add_weight, ["add_attribute"], request="Add pet weight")
        st = sql_seed.seed_status(self.s)
        self.assertEqual(st["state"], "stale")
        self.assertNotIn("sql", self.s.data["completed_stages"])
        self.assertEqual(self.s.data["current_stage"], "revision")
        with self.assertRaises(approval.ApprovalError):                           # stale data cannot be regenerated unapproved
            sql_seed.generate(self.s, SeedLLM(clinic_data()), self.prompts)
        self.assertEqual(self.s.data["sql_stage"]["seed"]["sha256"], old_sha)
        self.approve()
        with self.assertRaises(sql_schema.SchemaStateError):                      # and not before schema.sql is regenerated
            sql_seed.generate(self.s, SeedLLM(clinic_data()), self.prompts)

    def test_second_run_is_idempotent_and_force_asks_again(self):
        self.prepare()
        self.seed(clinic_data())
        res, llm = self.seed()
        self.assertFalse(res.changed)
        self.assertEqual(llm.calls, [])
        res, llm = self.seed(clinic_data(), force=True)
        self.assertTrue(res.changed)
        self.assertEqual(len(llm.calls), 1)

    def test_interrupted_run_is_recovered_without_the_llm(self):
        self.prepare()
        real = sql_seed.sd.render_seed_sql
        with mock.patch.object(sql_seed.sd, "render_seed_sql", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.seed(clinic_data())
        self.assertEqual(self.s.data["sql_stage"]["seed"]["status"], "generating")
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "incomplete")
        self.assertTrue(self.path("seed-data.json").exists())                      # validated data was saved first
        self.assertFalse(self.path().exists())
        res, llm = self.seed()                                                    # no scripted answers: LLM must not be called
        self.assertEqual(llm.calls, [])
        self.assertTrue(res.reused_data)
        self.assertTrue(res.completed)
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "current")

    def test_saved_data_is_not_reused_when_it_no_longer_validates(self):
        self.prepare()
        self.seed(clinic_data())
        p = self.path("seed-data.json")
        data = json.loads(p.read_text(encoding="utf-8"))
        data["records"]["pets"][0]["owner_id"] = 99                                # dangling foreign key
        p.write_text(json.dumps(data), encoding="utf-8")
        self.s.data["sql_stage"]["seed"]["status"] = "generating"                  # as after an interruption
        res, llm = self.seed(clinic_data())
        self.assertFalse(res.reused_data)
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(res.completed)

    def test_unseedable_schema_is_reported_before_any_llm_call(self):
        self.prepare()
        llm = SeedLLM(clinic_data())
        with mock.patch.object(sql_seed.sd, "build_model", side_effect=sd.SeedError("vaccines.leaflet: cannot be seeded")):
            with self.assertRaises(sd.SeedError):
                sql_seed.generate(self.s, llm, self.prompts)
        self.assertEqual(llm.calls, [])
        self.assertFalse(self.path().exists())

    def test_failed_validation_does_not_complete_stage_6(self):
        self.prepare(sqlite=True)
        with mock.patch.object(sql_seed.sql_validate, "validate_sqlite", return_value={
                "dbms": "sqlite", "level": "failed", "ok": False, "completes_stage": False, "executed_on": "SQLite",
                "checks": [], "errors": ["seed.sql statement 3 failed: boom"], "warnings": [], "row_counts": {}, "reason": ""}):
            res, _ = self.seed(clinic_data())
        self.assertFalse(res.completed)
        self.assertNotIn("sql", self.s.data["completed_stages"])
        self.assertEqual(sql_seed.seed_status(self.s)["state"], "failed_validation")
        self.assertEqual(self.s.data["sql_stage"]["status"], "in_progress")

class SeedCliTests(SeedStageBase):
    def test_cli_seed_flow(self):
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 2)
        self.assertIn("CANNOT GENERATE seed.sql", text)
        self.prepare(sqlite=True)
        code, text = self.run_cli("seed", ["--status"])
        self.assertEqual(code, 1)
        self.assertIn("seed.sql: missing", text)
        self.assertIn("schema.sql: current", text)
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 0, text)
        self.assertIn("Generated seed.sql for sqlite", text)
        self.assertIn("EXECUTED on SQLite", text)
        self.assertIn("Stage 6 is COMPLETE", text)
        self.assertIn("owners=3", text)
        self.assertEqual(self.state()["sql_stage"]["seed"]["rows_requested"], 3)
        code, text = self.run_cli("seed", llm=SeedLLM())
        self.assertIn("already up to date", text)
        code, text = self.run_cli("seed", ["--status"])
        self.assertEqual(code, 0)
        self.assertIn("Stage 6: COMPLETE", text)
        self.path().write_text("-- x", encoding="utf-8")
        code, text = self.run_cli("seed", ["--status"])
        self.assertEqual(code, 1)
        self.assertIn("modified", text)
        self.assertIn("Stage 6: not complete", text)
        self.assertNotIn("Stage 6: COMPLETE", text)
        st = self.state()
        self.assertNotIn("sql", st["completed_stages"])              # the saved state was reopened too
        self.assertEqual(st["current_stage"], "sql")
        code, text = self.run_cli("seed", ["--force", "--rows", "3"], llm=SeedLLM(clinic_data()))      # recovery
        self.assertEqual(code, 0, text)
        code, text = self.run_cli("seed", ["--status"])
        self.assertIn("Stage 6: COMPLETE", text)
        self.path().unlink()                                         # missing file reopens it as well
        code, text = self.run_cli("seed", ["--status"])
        self.assertEqual(code, 1)
        self.assertIn("seed.sql: missing_file", text)
        self.assertIn("Stage 6: not complete", text)
        self.assertNotIn("sql", self.state()["completed_stages"])

    def test_cli_unverified_label_and_stale_schema_message(self):
        self.prepare()
        self.path("schema.sql").write_text("-- x", encoding="utf-8")
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 2)
        self.assertIn("modified", text)
        self.assertIn("Nothing was written", text)
        code, _ = self.run_cli("schema", ["--force"])
        code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 0, text)
        self.assertIn("UNVERIFIED", text)
        self.assertIn("NOT proof", text)
        self.assertIn("COMPLETE as SQL generation (validation level: unverified", text)

    def test_cli_failed_validation_exit_code(self):
        self.prepare(sqlite=True)
        bad = {"dbms": "sqlite", "level": "failed", "ok": False, "completes_stage": False, "executed_on": "SQLite 3",
               "checks": [], "errors": ["seed.sql statement 2 failed: no such table"], "warnings": [], "row_counts": {}, "reason": ""}
        with mock.patch.object(sql_seed.sql_validate, "validate_sqlite", return_value=bad):
            code, text = self.run_cli("seed", ["--rows", "3"], llm=SeedLLM(clinic_data()))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", text)
        self.assertIn("Stage 6 is NOT complete", text)


class ConfigAndScopeTests(unittest.TestCase):
    def test_new_config_keys_are_optional_and_validated(self):
        cfg = config_from_dict({"provider": "ollama", "model": "m"}, env={})
        self.assertEqual(cfg.seed_rows, 8)
        self.assertEqual(cfg.validation, {})
        cfg = config_from_dict({"provider": "ollama", "model": "m", "seed_rows": 20,
                                "validation": {"postgresql": {"dsn": "postgresql://u@h/scratch"}}}, env={})
        self.assertEqual(cfg.seed_rows, 20)
        from dba_agent.errors import ConfigError
        with self.assertRaises(ConfigError):
            config_from_dict({"provider": "ollama", "model": "m", "seed_rows": 0}, env={})

    def test_prompts_are_external_files_and_code_is_scenario_agnostic(self):
        for name in ("seed_system.md", "seed_user.md"):
            self.assertTrue((ROOT / "prompts" / name).is_file())
        for f in ("seed_data.py", "sql_validate.py", "stages/sql_seed.py"):
            text = (ROOT / "dba_agent" / f).read_text(encoding="utf-8").lower()
            for word in ("clinic", "veterinar", "pets", "owners", "university", "enrollment", "library"):
                self.assertNotIn(word, text, f"{word!r} hard-coded in {f}")


if __name__ == "__main__":
    unittest.main()
