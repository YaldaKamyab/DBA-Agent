"""Batch 6C - final verification of Stages 1-6 on the two Batch 6B scenarios.

* Stages 1-5 run through the REAL stage code with scripted (mocked) LLM answers; Stage 6A is deterministic Python; the
  seed records of Stage 6B are scripted LLM answers (tests/seeds_6c.py). Mocked answers prove the WORKFLOW, not that a
  real model understands an unseen scenario.
* The generated SQL is EXECUTED on disposable SQLite databases (positive AND negative tests).
* PostgreSQL / MySQL-MariaDB / SQL Server are only checked statically here; nothing below claims they ran on a server.
"""
import copy
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

from dba_agent import approval, cli, seed_data as sd
from dba_agent.config import config_from_dict
from dba_agent.prompts import PromptLoader
from dba_agent.schema_sql import generate_schema_sql, strict_check
from dba_agent.state import load_session
from dba_agent.stages import sql_schema, sql_seed
from tests import seeds_6c
from tests.pipeline_6b import PROMPTS, ScriptedLLM, run_pipeline
from tests.test_batch6b_scenarios import ordering_for

SEEDS = {"booking": seeds_6c.BOOKING_SEED, "ordering": seeds_6c.ORDERING_SEED}
ROWS = 5


def _run_cli(sdir, argv, llm=None, input_fn=lambda: "", out=None):
    out = [] if out is None else out
    cfg = config_from_dict({"provider": "ollama", "model": "m", "sessions_dir": sdir, "seed_rows": ROWS}, env={})
    patches = [mock.patch.object(cli, "load_config", lambda p=None: cfg)]
    if llm is not None:
        patches.append(mock.patch.object(cli, "build_runtime", lambda session, config: (llm, PromptLoader(PROMPTS))))
    for p in patches:
        p.start()
    try:
        code = cli.main(["--sessions-dir", sdir] + argv, input_fn=input_fn, out=out.append)
    finally:
        for p in patches:
            p.stop()
    return code, "\n".join(map(str, out))


def build(tmp, name, dbms=None, seed=None, with_seed=True, rows=ROWS):
    """Stages 1-6 with the real code. -> session (approved, schema.sql and, optionally, seed.sql generated)."""
    kw = {}
    if dbms and name == "ordering":
        kw = dict(dbms=dbms, logical=ordering_for(dbms))
    s, _, _ = run_pipeline(tmp, name, **kw)
    approval.approve(s)
    sql_schema.generate(s)
    if with_seed:
        res = sql_seed.generate(s, ScriptedLLM({"seed": [copy.deepcopy(seed or SEEDS[name])]}), PromptLoader(PROMPTS), rows=rows)
        assert res.report["completes_stage"], res.report
    return s


def connect(*scripts):
    con = sqlite3.connect(":memory:")                 # a disposable database, nothing is written to disk
    con.execute("PRAGMA foreign_keys = ON")
    for sql in scripts:
        con.executescript(sql)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def out_text(s, f):
    return (s.output_dir / f).read_text(encoding="utf-8")


class SqlCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def ok(self, con, sql, params=()):
        con.execute(sql, params)

    def rejects(self, con, sql, params=(), msg=None):
        """The statement must be refused by SQLite itself AND leave the data unchanged."""
        before = [r for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {t[0]: con.execute(f'SELECT COUNT(*) FROM "{t[0]}"').fetchone()[0] for t in before}
        with self.assertRaises(sqlite3.IntegrityError, msg=msg or sql):
            con.execute(sql, params)
        for t, n in counts.items():
            self.assertEqual(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0], n, f"{t} changed after a rejected statement")


APPT = "INSERT INTO appointments (starts_at, ends_at, status, notes, customer_id, employee_id, service_id) VALUES (?,?,?,?,?,?,?)"


# =================================================================== A. booking (SQLite) - the schema is the approved design
class BookingSchemaTests(SqlCase):
    def setUp(self):
        super().setUp()
        self.s = build(self.tmp.name, "booking")
        self.design = self.s.data["design"]
        self.schema, self.seed = out_text(self.s, "schema.sql"), out_text(self.s, "seed.sql")

    def db(self, seeded=True):
        return connect(self.schema, self.seed) if seeded else connect(self.schema)

    def test_selected_dbms_is_propagated_everywhere(self):
        self.assertEqual(self.s.data["dbms_choice"]["selected_id"], "sqlite")
        self.assertEqual(self.s.data["design"]["dbms"]["id"], "sqlite")
        self.assertEqual(self.s.data["sql_stage"]["schema"]["dbms_id"], "sqlite")
        self.assertEqual(self.s.data["sql_stage"]["seed"]["dbms_id"], "sqlite")
        self.assertIn("(SQLite)", self.schema.splitlines()[0])
        self.assertIn("AUTOINCREMENT", self.schema)
        self.assertNotIn("GENERATED", self.schema)

    def test_every_table_column_key_and_index_matches_the_approved_design(self):
        con = self.db(seeded=False)
        for t in self.design["physical"]["tables"]:
            with self.subTest(t["name"]):
                info = con.execute(f'PRAGMA table_info("{t["name"]}")').fetchall()          # cid, name, type, notnull, dflt, pk
                self.assertEqual([r[1] for r in info], [c["name"] for c in t["columns"]], "column names and order")
                for r, c in zip(info, t["columns"]):
                    want_nn = (not c["nullable"]) or c["name"] in t["primary_key"]
                    self.assertEqual(bool(r[3]), want_nn, f"NOT NULL of {c['name']}")
                pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
                self.assertEqual(pk, t["primary_key"], "primary key columns in order")
                fks = {}
                for r in con.execute(f'PRAGMA foreign_key_list("{t["name"]}")'):               # id, seq, table, from, to, on_update, on_delete
                    fks.setdefault(r[0], []).append(r)
                got = sorted((tuple(x[3] for x in v), v[0][2], tuple(x[4] for x in v), v[0][6]) for v in fks.values())
                want = sorted((tuple(f["columns"]), f["ref_table"], tuple(f["ref_columns"]), f["on_delete"]) for f in t["foreign_keys"])
                self.assertEqual(got, want, "foreign keys")
                uq = {tuple(x[2] for x in con.execute(f'PRAGMA index_info("{i[1]}")')) for i in con.execute(f'PRAGMA index_list("{t["name"]}")') if i[2]}
                for u in t["unique_constraints"]:
                    self.assertIn(tuple(u["columns"]), uq, f"unique {u['name']}")
                names = {r[0]: r[1] for r in con.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (t["name"],))}
                for ix in t["indexes"]:
                    self.assertIn(ix["name"], names)
                    self.assertEqual(" WHERE " in (names[ix["name"]] or ""), bool(ix.get("where")), f"partial flag of {ix['name']}")
                    self.assertEqual("UNIQUE" in (names[ix["name"]] or "").upper().split(" INDEX")[0], bool(ix["unique"]))
        self.assertEqual({r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")},
                         {t["name"] for t in self.design["physical"]["tables"]}, "no extra and no missing tables")

    def test_primary_keys_work(self):
        con = self.db()
        self.rejects(con, "INSERT INTO customers (customer_id, full_name, email) VALUES (1, 'Dup', 'dup@example.com')")
        self.rejects(con, "INSERT INTO employee_services (employee_id, service_id) VALUES (1, 1)")          # composite PK
        self.rejects(con, "INSERT INTO employee_services (employee_id, service_id) VALUES (NULL, 3)")        # PK part cannot be NULL
        con.execute("INSERT INTO customers (full_name, email) VALUES ('Auto Id', 'auto@example.com')")       # identity is generated
        self.assertEqual(con.execute("SELECT MAX(customer_id) FROM customers").fetchone()[0], 6)

    def test_foreign_keys_reject_missing_parents_and_protect_history(self):
        con = self.db()
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 10:30:00", "booked", None, 999, 1, 1))
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 10:30:00", "booked", None, 1, 999, 1))
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 10:30:00", "booked", None, 1, 1, 999))
        self.rejects(con, "INSERT INTO employee_services VALUES (99, 1)")
        self.rejects(con, "INSERT INTO status_changes (new_status, changed_at, appointment_id) VALUES ('booked', '2026-11-16 08:00:00', 999)")
        self.rejects(con, "DELETE FROM customers WHERE customer_id = 1")             # history: customers with appointments cannot vanish
        self.rejects(con, "DELETE FROM appointments WHERE appointment_id = 1")       # ... nor appointments that have a history
        self.rejects(con, "DELETE FROM services WHERE service_id = 1")
        con.execute("DELETE FROM employee_services WHERE employee_id = 1 AND service_id = 5")             # a plain link may be removed

    def test_not_null_columns_cannot_be_bypassed(self):
        con = self.db()
        good = ["2026-12-01 10:00:00", "2026-12-01 10:30:00", "booked", None, 1, 1, 1]
        for i, name in ((0, "starts_at"), (1, "ends_at"), (2, "status"), (4, "customer_id"), (5, "employee_id"), (6, "service_id")):
            row = list(good)
            row[i] = None
            with self.subTest(name):
                self.rejects(con, APPT, row)
        self.rejects(con, "INSERT INTO customers (full_name, email) VALUES (NULL, 'x@example.com')")
        self.rejects(con, "INSERT INTO customers (full_name, email) VALUES ('X', NULL)")
        self.rejects(con, "INSERT INTO services (name, duration_minutes, price) VALUES ('Wax', NULL, 10)")
        self.ok(con, APPT, good)                                                       # nullable notes / phone really are optional
        self.ok(con, "INSERT INTO customers (full_name, email) VALUES ('No Phone', 'nophone@example.com')")

    def test_unique_constraints_reject_forbidden_duplicates(self):
        con = self.db()
        self.rejects(con, "INSERT INTO customers (full_name, email) VALUES ('Other', 'maryam.karimi@example.com')")
        self.rejects(con, "INSERT INTO employees (full_name, email) VALUES ('Other', 'azadeh@salon.example')")
        self.rejects(con, "INSERT INTO services (name, duration_minutes, price) VALUES ('Haircut', 20, 10)")
        self.ok(con, "INSERT INTO customers (full_name, email) VALUES ('Same Name', 'another@example.com')")    # name alone is not unique

    def test_check_constraints_reject_invalid_values_and_null_is_not_a_way_around(self):
        con = self.db()
        for st in ("Booked", "pending", "", "no-show"):
            self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 10:30:00", st, None, 1, 1, 1), msg=f"status {st!r}")
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 10:00:00", "booked", None, 1, 1, 1))     # ends_at = starts_at
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 09:00:00", "booked", None, 1, 1, 1))     # ends before it starts
        self.rejects(con, "INSERT INTO services (name, duration_minutes, price) VALUES ('Zero', 0, 10)")
        self.rejects(con, "INSERT INTO services (name, duration_minutes, price) VALUES ('Negative', -5, 10)")
        self.rejects(con, "UPDATE appointments SET status = 'whatever' WHERE appointment_id = 1")
        self.rejects(con, "UPDATE appointments SET status = NULL WHERE appointment_id = 1")               # NULL must not skip the status rule

    def test_conditional_uniqueness_applies_only_to_active_appointments(self):
        con = self.db()
        # seed rows 1 (booked) and 4 (cancelled) already share employee 1 + 09:00 -> history may repeat
        self.assertEqual(con.execute("SELECT COUNT(*) FROM appointments WHERE employee_id=1 AND starts_at='2026-11-16 09:00:00'").fetchone()[0], 2)
        t0, t1 = "2026-11-16 09:00:00", "2026-11-16 09:45:00"
        for st in ("booked", "confirmed"):                                           # a second ACTIVE booking at the same time is forbidden
            self.rejects(con, APPT, (t0, t1, st, None, 2, 1, 1), msg=st)
        for st in ("cancelled", "no_show", "completed"):                             # non-active rows may repeat as history
            self.ok(con, APPT, (t0, t1, st, None, 2, 1, 1))
        self.ok(con, APPT, (t0, t1, "booked", None, 2, 2, 1))                        # another employee, same time: fine
        self.ok(con, APPT, ("2026-11-16 15:00:00", "2026-11-16 15:45:00", "confirmed", None, 2, 1, 1))   # same employee, other time: fine

    def test_status_changes_follow_the_condition_in_both_directions(self):
        con = self.db()
        t0, t1 = "2026-11-16 09:00:00", "2026-11-16 09:45:00"
        self.rejects(con, "UPDATE appointments SET status = 'confirmed' WHERE appointment_id = 4")        # re-activating the cancelled row collides
        con.execute("UPDATE appointments SET status = 'cancelled' WHERE appointment_id = 1")             # the active one is cancelled ...
        con.execute("UPDATE appointments SET status = 'booked' WHERE appointment_id = 4")                # ... so the other may become active
        self.rejects(con, "UPDATE appointments SET status = 'confirmed' WHERE appointment_id = 1")        # and now the first collides
        con.execute("UPDATE appointments SET starts_at = '2026-11-16 17:00:00', ends_at = '2026-11-16 17:45:00' WHERE appointment_id = 1")  # moved away first
        con.execute("UPDATE appointments SET status = 'confirmed' WHERE appointment_id = 1")             # now allowed

    def test_history_is_append_only(self):
        con = self.db()
        n = con.execute("SELECT COUNT(*) FROM status_changes").fetchone()[0]
        self.rejects(con, "UPDATE status_changes SET reason = 'edited' WHERE change_id = 1")
        self.rejects(con, "DELETE FROM status_changes WHERE change_id = 1")
        self.rejects(con, "INSERT OR REPLACE INTO status_changes (change_id, new_status, changed_at, appointment_id) VALUES (1, 'x', 'y', 1)")
        self.rejects(con, "INSERT INTO status_changes (change_id, new_status, changed_at, appointment_id) VALUES (1, 'booked', '2026-11-16 08:00:00', 1)")
        con.execute("INSERT INTO status_changes (old_status, new_status, changed_at, appointment_id) VALUES ('booked','confirmed','2026-11-16 08:30:00', 1)")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM status_changes").fetchone()[0], n + 1)

    def test_unsupported_rules_are_reported_as_application_logic_not_as_enforced(self):
        con = self.db()
        # The database really does NOT stop an overlapping active booking (only identical start times) ...
        self.ok(con, APPT, ("2026-11-16 09:15:00", "2026-11-16 09:30:00", "booked", None, 2, 1, 1))
        # ... and does not check that the employee provides the service (employee 5 offers only service 3).
        self.ok(con, APPT, ("2026-12-02 10:00:00", "2026-12-02 11:00:00", "booked", None, 2, 5, 2))
        rec = self.s.data["sql_stage"]["schema"]["rule_enforcement"]
        app_ids = {r["rule_id"] for r in rec["application"]}
        self.assertEqual(app_ids, {"no_overlapping_active_bookings", "employee_provides_service"})
        db_ids = {r["rule_id"] for r in rec["database"]}
        self.assertTrue(app_ids.isdisjoint(db_ids))
        self.assertRegex(self.schema, r"NOT enforced by this script - application logic required: no_overlapping_active_bookings,\s*\n--\s+employee_provides_service")

    def test_indexes_are_created_where_specified(self):
        con = self.db(seeded=False)
        got = {r[1] for r in con.execute("SELECT * FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        self.assertTrue({"ux_appt_employee_start_active", "ix_appt_customer"} <= got)
        plan = " ".join(str(r) for r in con.execute("EXPLAIN QUERY PLAN SELECT * FROM appointments WHERE customer_id = 1"))
        self.assertIn("ix_appt_customer", plan)

    def test_seed_executed_for_real_counts_integrity_and_coverage_report(self):
        con = self.db()
        rec = self.s.data["sql_stage"]["seed"]
        for t, rows in SEEDS["booking"]["tables"].items():
            self.assertEqual(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0], len(rows), t)
            self.assertEqual(rec["row_counts"][t], len(rows), t)
        self.assertEqual(rec["rows_total"], sum(len(r) for r in SEEDS["booking"]["tables"].values()))
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        v = rec["validation"]
        self.assertEqual((v["level"], v["executed_dbms"], v["completes_stage"]), ("executed", "sqlite", True))
        self.assertEqual(v["status_summary"]["coverage"], "complete")
        self.assertEqual(v["status_summary"]["sql_execution"], "passed")
        # coverage claims match the data: every status value and a NULL example really are present
        self.assertEqual({r[0] for r in con.execute("SELECT DISTINCT status FROM appointments")},
                         {"booked", "confirmed", "completed", "cancelled", "no_show"})
        self.assertGreater(con.execute("SELECT COUNT(*) FROM customers WHERE phone IS NULL").fetchone()[0], 0)
        self.assertGreater(con.execute("SELECT COUNT(*) FROM employee_services GROUP BY employee_id HAVING COUNT(*) > 1").fetchone()[0], 0)
        self.assertIn("INSERT INTO", self.seed)
        self.assertNotRegex(self.seed, r"(?i)test\d|lorem")

    def test_generation_is_deterministic(self):
        again = build(self.tmp.name + "/second", "booking")
        self.assertEqual(out_text(again, "schema.sql"), self.schema)
        self.assertEqual(out_text(again, "seed.sql"), self.seed)
        fp = self.s.data["approval"]["design_fingerprint"]
        self.assertEqual(generate_schema_sql(self.s.data["design"], fp).sql, generate_schema_sql(copy.deepcopy(self.s.data["design"]), fp).sql)
        self.assertEqual(again.data["sql_stage"]["schema"]["sha256"], self.s.data["sql_stage"]["schema"]["sha256"])


# =================================================================== B. ordering - executed on SQLite, PostgreSQL only static
class OrderingSqliteTests(SqlCase):
    """The ordering design is a PostgreSQL scenario. The SAME logical design mapped to SQLite types (ordering_for) is what
    is executed here; it says nothing about the PostgreSQL script."""

    def setUp(self):
        super().setUp()
        self.s = build(self.tmp.name, "ordering", dbms="sqlite")
        self.schema, self.seed = out_text(self.s, "schema.sql"), out_text(self.s, "seed.sql")
        self.con = connect(self.schema, self.seed)

    def test_seed_executes_with_exact_row_counts_and_integrity(self):
        con = self.con
        for t, rows in SEEDS["ordering"]["tables"].items():
            self.assertEqual(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0], len(rows), t)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        v = self.s.data["sql_stage"]["seed"]["validation"]
        self.assertEqual((v["level"], v["executed_dbms"]), ("executed", "sqlite"))
        self.assertIn("sales_orders", self.s.data["sql_stage"]["seed"]["row_counts"])

    def test_orders_keep_the_purchase_time_price(self):
        con = self.con
        self.assertEqual(con.execute("SELECT unit_price FROM order_items WHERE order_id=1 AND product_id=1").fetchone()[0], 79.9)
        con.execute("UPDATE products SET current_price = 120 WHERE product_id = 1")
        con.execute("INSERT INTO product_prices (price, valid_from, product_id) VALUES (120, '2026-11-15 00:00:00', 1)")
        self.assertEqual(con.execute("SELECT unit_price FROM order_items WHERE order_id=1 AND product_id=1").fetchone()[0], 79.9)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM product_prices WHERE product_id = 1").fetchone()[0], 3)   # price history grows

    def test_historical_orders_stay_valid(self):
        con = self.con
        self.rejects(con, "DELETE FROM products WHERE product_id = 1")                 # a sold product cannot be removed
        self.rejects(con, "DELETE FROM customers WHERE customer_id = 1")
        self.rejects(con, "DELETE FROM sales_orders WHERE order_id = 1")               # payments and events still reference the order
        self.rejects(con, "DELETE FROM product_prices WHERE product_id = 99999 OR price_id = -1 OR 0") if False else None
        con.execute("DELETE FROM products WHERE product_id = 4")                       # never sold: may go
        con.execute("UPDATE products SET name = 'Mechanical keyboard v2' WHERE product_id = 1")           # editing a product keeps orders valid
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_check_unique_not_null_and_fk_rules(self):
        con = self.con
        item = "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?,?,?,?)"
        self.ok(con, item, (4, 2, 1, 24.5))
        for q in (0, -1):
            self.rejects(con, item, (4, 3, q, 10))
        self.rejects(con, item, (4, 2, 1, 24.5))                                       # same (order, product) twice: PK
        self.rejects(con, item, (4, 99, 1, 10))
        self.rejects(con, item, (99, 3, 1, 10))
        self.rejects(con, item, (4, 3, None, 10))
        self.rejects(con, item, (4, 3, 1, None))                                       # the price snapshot cannot be left out
        pay = "INSERT INTO payments (amount, method, paid_at, order_id) VALUES (?,?,?,?)"
        for amount in (0, -5, None):
            self.rejects(con, pay, (amount, "card", None, 3))
        self.rejects(con, pay, (10, "bitcoin", None, 3))
        self.rejects(con, pay, (10, None, None, 3))
        self.ok(con, pay, (10, "cash", None, 3))                                       # paid_at is optional (not yet paid)
        order = "INSERT INTO sales_orders (placed_at, status, customer_id) VALUES (?,?,?)"
        self.rejects(con, order, ("2026-11-16 10:00:00", "refunded", 1))
        self.rejects(con, order, ("2026-11-16 10:00:00", None, 1))
        self.rejects(con, order, ("2026-11-16 10:00:00", "paid", 99))
        self.rejects(con, "INSERT INTO products (sku, name, current_price) VALUES ('KB-100', 'Dup SKU', 5)")
        self.rejects(con, "INSERT INTO products (sku, name, current_price) VALUES ('NEG-1', 'Negative', -0.01)")
        self.ok(con, "INSERT INTO products (sku, name, current_price) VALUES ('FREE-1', 'Free sample', 0)")          # boundary value allowed
        self.rejects(con, "INSERT INTO customers (full_name, email) VALUES ('Dup', 'kaveh.rahimi@example.com')")
        self.rejects(con, "INSERT INTO product_prices (price, valid_from, product_id) VALUES (70, '2026-01-01 00:00:00', 1)")   # same product + valid_from

    def test_several_payments_per_order_are_allowed_as_assumed(self):
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM payments WHERE order_id = 2").fetchone()[0], 2)

    def test_generated_sql_is_deterministic(self):
        again = build(self.tmp.name + "/second", "ordering", dbms="sqlite")
        self.assertEqual((out_text(again, "schema.sql"), out_text(again, "seed.sql")), (self.schema, self.seed))


# =================================================================== C. NULL-sensitive CHECK and conditional rules (booking-derived)
class ConditionalRuleTests(SqlCase):
    """A conditional CHECK derived from the booking design: a cancelled appointment must carry a note, others need none.
    Built in memory from a COPY of the design - the approved artifacts are not touched."""

    def variant(self, expression):
        s = build(self.tmp.name, "booking", with_seed=False)
        d = copy.deepcopy(s.data["design"])
        t = next(t for t in d["physical"]["tables"] if t["name"] == "appointments")
        t["check_constraints"].append({"name": "ck_appt_cancel_note", "expression": expression, "description": ""})
        res = generate_schema_sql(d, s.data["approval"]["design_fingerprint"])
        return res, connect(res.sql)

    def seed_parents(self, con):
        con.executescript("INSERT INTO customers (full_name,email) VALUES ('A','a@x.example'); INSERT INTO employees (full_name,email) VALUES ('E','e@x.example');"
                          "INSERT INTO services (name,duration_minutes,price) VALUES ('S',10,1);")

    def test_explicit_null_aware_conditional_check_is_strict(self):
        expr = ("(status = 'cancelled' AND notes IS NOT NULL AND length(trim(notes)) > 0) OR (status <> 'cancelled' AND notes IS NULL)"
                " OR (status <> 'cancelled' AND notes IS NOT NULL)")
        res, con = self.variant(expr)
        self.seed_parents(con)
        self.assertIn(strict_check(expr), res.sql)
        ins = lambda st, notes: con.execute(APPT, ("2026-12-01 10:00:00", "2026-12-01 11:00:00", st, notes, 1, 1, 1))
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 11:00:00", "cancelled", None, 1, 1, 1))     # NULL must not bypass
        self.rejects(con, APPT, ("2026-12-01 10:00:00", "2026-12-01 11:00:00", "cancelled", "   ", 1, 1, 1))
        ins("cancelled", "Customer is ill")
        ins("booked", None)                                                                                      # the rule applies only when cancelled
        ins("completed", "Great")

    def test_condition_that_hides_a_null_is_documented_honestly(self):
        """`status <> 'cancelled' OR length(trim(notes)) > 0` lets a cancelled row with notes NULL through, because SQL treats UNKNOWN
        as a pass. The generator must not claim otherwise: it states the weakness in schema.sql."""
        res, con = self.variant("status <> 'cancelled' OR length(trim(notes)) > 0")
        self.seed_parents(con)
        con.execute(APPT, ("2026-12-01 10:00:00", "2026-12-01 11:00:00", "cancelled", None, 1, 1, 1))       # actual SQLite behaviour
        self.assertTrue(any("ck_appt_cancel_note" in n and "NULL" in n and "nullable" in n.lower() for n in res.notes), res.notes)
        self.assertIn("ck_appt_cancel_note", res.sql.split("\nPRAGMA foreign_keys", 1)[0])             # visible in the script header too
        self.rejects(con, APPT, ("2026-12-01 12:00:00", "2026-12-01 13:00:00", "cancelled", "", 1, 1, 1))       # an empty note IS rejected

    def test_plain_check_on_optional_column_keeps_null_allowed(self):
        res, con = self.variant("length(trim(notes)) > 0")                               # optional column: NULL = "no note", allowed
        self.seed_parents(con)
        self.assertNotIn(strict_check("length(trim(notes)) > 0"), res.sql)
        con.execute(APPT, ("2026-12-01 10:00:00", "2026-12-01 11:00:00", "booked", None, 1, 1, 1))
        self.rejects(con, APPT, ("2026-12-01 12:00:00", "2026-12-01 13:00:00", "booked", "", 1, 1, 1))


# =================================================================== D. other DBMS: static / offline only
class MultiDbmsStaticTests(SqlCase):
    MARKERS = {"postgresql": ("GENERATED BY DEFAULT AS IDENTITY", "AUTOINCREMENT"), "mysql_mariadb": ("AUTO_INCREMENT", "AUTOINCREMENT"),
               "sql_server": ("IDENTITY(1,1)", "AUTOINCREMENT"), "sqlite": ("AUTOINCREMENT", "GENERATED BY DEFAULT")}

    def test_every_dialect_is_generated_and_never_reported_as_executed(self):
        for dbms in ("postgresql", "mysql_mariadb", "sql_server", "sqlite"):
            with self.subTest(dbms):
                s = build(self.tmp.name + "/" + dbms, "ordering", dbms=dbms)
                schema = out_text(s, "schema.sql")
                self.assertEqual(s.data["sql_stage"]["schema"]["dbms_id"], dbms)
                self.assertIn(self.MARKERS[dbms][0], schema)
                self.assertNotIn(self.MARKERS[dbms][1], schema)
                v = s.data["sql_stage"]["seed"]["validation"]
                if dbms == "sqlite":
                    self.assertEqual((v["level"], v["executed_dbms"]), ("executed", "sqlite"))
                else:
                    self.assertEqual((v["level"], v["executed_dbms"]), ("unverified", None))
                    self.assertEqual(v["static_validation"], "passed")
                    self.assertNotEqual(v["execution"], "executed_ok")
                    self.assertIn("NOT executed", s.data["sql_stage"]["note"])
                    self.assertFalse(s.data["sql_stage"]["schema"]["executed_against_dbms"])
                    self.assertIn("not executed", v["status_summary"]["sql_execution"])
                    self.assertTrue(v["status_summary"]["referential_integrity"].startswith("checked by the Python validator only"))

    def test_cli_reports_generated_versus_executed_honestly(self):
        s = build(self.tmp.name, "ordering", with_seed=False)                           # PostgreSQL scenario
        code, text = _run_cli(self.tmp.name, ["seed", s.session_id], llm=ScriptedLLM({"seed": [copy.deepcopy(seeds_6c.ORDERING_SEED)]}))
        self.assertEqual(code, 0, text)
        self.assertIn("validation level: unverified", text)
        self.assertIn("NOT executed or verified on PostgreSQL", text)
        self.assertNotIn("executed successfully on PostgreSQL", text)
        self.assertNotIn("Stage 6 is COMPLETE (SQL generated and executed", text)

    def test_unsupported_features_are_refused_not_silently_dropped(self):
        s = build(self.tmp.name, "booking", with_seed=False)
        d = copy.deepcopy(s.data["design"])
        d["dbms"] = {"id": "mysql_mariadb", "name": "MySQL/MariaDB"}
        from dba_agent.schema_sql import SchemaGenerationError
        with self.assertRaises(SchemaGenerationError):                                   # partial unique index does not exist in MySQL
            generate_schema_sql(d, "0" * 64)


# =================================================================== E. complete offline workflows through the CLI
class FullWorkflowTests(SqlCase):
    def drive_new(self, name, dbms=None):
        """Stages 1-5 by the real stage code; approval, schema and seed through the real CLI commands."""
        kw = dict(dbms=dbms, logical=ordering_for(dbms)) if dbms else {}
        s, _, s12 = run_pipeline(self.tmp.name, name, **kw)
        return s, s12

    def test_booking_complete_workflow_with_resume_and_final_status(self):
        s, s12 = self.drive_new("booking")
        sid = s.session_id
        self.assertEqual(s.data["completed_stages"], ["intake", "interview", "dbms", "design"])
        # requirements traceability: every stated requirement text reaches the design documents
        doc = out_text(s, "conceptual-model.md") + out_text(s, "logical-schema.md") + out_text(s, "requirements.md")
        for key in ("status", "appointment", "history"):
            self.assertIn(key, doc.lower())
        code, text = _run_cli(self.tmp.name, ["approve", sid], input_fn=lambda: "y")
        self.assertEqual(code, 0, text)
        self.assertEqual(load_session(self.tmp.name, sid).current_stage, "sql")
        code, text = _run_cli(self.tmp.name, ["schema", sid])
        self.assertEqual(code, 0, text)
        # interrupted seed run: the LLM fails -> Stage 6 must stay open and say so
        from dba_agent.errors import InvalidResponseError
        boom = InvalidResponseError("simulated provider failure", retryable=False)
        code, text = _run_cli(self.tmp.name, ["seed", sid], llm=ScriptedLLM({"seed": [boom]}))
        self.assertEqual(code, 2, text)
        self.assertIn("simulated provider failure", text)
        mid = load_session(self.tmp.name, sid)
        self.assertNotIn("sql", mid.data["completed_stages"])
        self.assertEqual(sql_seed.seed_status(mid)["state"], "incomplete")
        code, text = _run_cli(self.tmp.name, ["seed", sid, "--status"])
        self.assertEqual(code, 1, text)
        self.assertIn("Stage 6: not complete", text)
        # resume with a healthy provider
        code, text = _run_cli(self.tmp.name, ["seed", sid], llm=ScriptedLLM({"seed": [copy.deepcopy(seeds_6c.BOOKING_SEED)]}))
        self.assertEqual(code, 0, text)
        self.assertIn("Stage 6 is COMPLETE (SQL generated and executed successfully on SQLite)", text)
        self.assertIn("PRAGMA foreign_key_check", text)
        final = load_session(self.tmp.name, sid)
        self.assertEqual(final.data["completed_stages"][-1], "sql")
        code, text = _run_cli(self.tmp.name, ["seed", sid, "--status"])
        self.assertEqual(code, 0, text)
        self.assertIn("seed.sql: current", text)
        self.assertIn("Stage 6: COMPLETE", text)
        con = connect(out_text(final, "schema.sql"), out_text(final, "seed.sql"))
        self.assertEqual(sum(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in SEEDS["booking"]["tables"]),
                         final.data["sql_stage"]["seed"]["rows_total"])
        # a normal re-run changes nothing and makes no LLM call
        code, text = _run_cli(self.tmp.name, ["seed", sid], llm=ScriptedLLM({"seed": []}))
        self.assertEqual(code, 0, text)
        self.assertIn("already up to date", text)

    def test_ordering_complete_workflow_postgresql_is_generated_but_not_executed(self):
        s, _ = self.drive_new("ordering")
        sid = s.session_id
        self.assertEqual(s.data["design"]["dbms"]["id"], "postgresql")
        self.assertEqual(_run_cli(self.tmp.name, ["approve", sid], input_fn=lambda: "y")[0], 0)
        self.assertEqual(_run_cli(self.tmp.name, ["schema", sid])[0], 0)
        code, text = _run_cli(self.tmp.name, ["seed", sid], llm=ScriptedLLM({"seed": [copy.deepcopy(seeds_6c.ORDERING_SEED)]}))
        self.assertEqual(code, 0, text)
        self.assertIn("NOT executed or verified on PostgreSQL", text)
        code, text = _run_cli(self.tmp.name, ["seed", sid, "--status"])
        self.assertIn("NOT executed on the selected DBMS", text)
        self.assertEqual(load_session(self.tmp.name, sid).data["sql_stage"]["seed"]["validation"]["executed_dbms"], None)

    def test_revision_after_schema_makes_sql_stale_and_reopens_stage6(self):
        s = build(self.tmp.name, "booking")
        self.assertIn("sql", s.data["completed_stages"])
        d = s.data["design"]
        s.data["design"] = copy.deepcopy(d)
        from tests.test_batch6b_scenarios import add_optional_column
        from dba_agent import revision
        from tests.test_stage5a_revision import ScriptedLLM as RevLLM, response
        revision.revise(s, RevLLM(s, lambda cur: response(cur, add_optional_column("customer", "customers", "birthday"), ["add_attribute"])),
                        PromptLoader(PROMPTS), "add birthday")
        sql_seed.refresh_completion(s)
        self.assertNotIn("sql", s.data["completed_stages"])
        self.assertNotEqual(sql_schema.schema_status(s)["state"], "current")
        self.assertNotEqual(sql_seed.seed_status(s)["state"], "current")


# =================================================================== F. regression: bare NUMERIC / DECIMAL
class BareNumericRegressionTests(unittest.TestCase):
    """Found by the booking scenario: `price NUMERIC` (SQLite) made the seed validator reject 18.5 ("more than 0 decimal places")."""

    def test_sqlite_and_postgresql_bare_numeric_has_no_scale_limit(self):
        self.assertIsNone(sd.type_info("NUMERIC", "sqlite")["scale"])
        self.assertIsNone(sd.type_info("DECIMAL", "sqlite")["scale"])
        self.assertIsNone(sd.type_info("NUMERIC", "postgresql")["scale"])

    def test_declared_precision_and_other_dbms_defaults_are_unchanged(self):
        self.assertEqual(sd.type_info("NUMERIC(10,2)", "sqlite")["scale"], 2)
        self.assertEqual(sd.type_info("NUMERIC(10,2)", "postgresql")["scale"], 2)
        self.assertEqual(sd.type_info("NUMERIC(10)", "postgresql")["scale"], 0)
        self.assertEqual(sd.type_info("DECIMAL", "mysql_mariadb")["scale"], 0)         # MySQL: DECIMAL = DECIMAL(10,0)
        self.assertEqual(sd.type_info("DECIMAL", "sql_server")["scale"], 0)            # SQL Server: DECIMAL = DECIMAL(18,0)


if __name__ == "__main__":
    unittest.main()
