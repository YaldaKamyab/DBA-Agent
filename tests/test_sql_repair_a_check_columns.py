"""SQL repair A: CHECK expressions may only reference columns of their own table (all four dialects).

SQLite cases are really executed with sqlite3; PostgreSQL / MySQL-MariaDB / SQL Server cases are static only.
"""
import sqlite3
import unittest

from dba_agent.schema_sql import SchemaGenerationError, check_unknown_columns, render_check
from tests.test_stage6a_schema import PG, MY, LITE, MS, col, gen, hand, tab

ALL = (PG, MY, LITE, MS)
COLS = ["id", "price", "qty", "status", "name", "created"]


def table(ck):
    return tab("items", [col("id"), col("price", "INTEGER"), col("qty"), col("status", "TEXT"), col("name", "TEXT"),
                         col("created", "TEXT")], ["id"], ck=ck)


class Positive(unittest.TestCase):
    VALID = {
        "simple": "price >= 0",
        "and_or": "price >= 0 AND (qty > 0 OR status = 'x')",
        "in_list": "status IN ('a', 'b')",
        "between": "qty BETWEEN 1 AND 10",
        "is_null": "name IS NOT NULL",
        "literal_word": "status <> 'unknown_col'",
        "case": "CASE WHEN qty > 0 THEN 1 ELSE 0 END = 1",
        "func_length": "LENGTH(name) > 0",
        "func_abs": "ABS(price) < 1000",
        "func_nested": "COALESCE(qty, 0) >= 0 AND LENGTH(TRIM(name)) > 0",
        "like": "name LIKE 'a%'",
        "qualified": "items.price > 0",
        "cast": "CAST(qty AS INTEGER) > 0",
        "current_ts": "created <= CURRENT_TIMESTAMP",
        "case_insensitive": "PRICE >= 0",
    }

    def test_valid_expressions_pass_validation_on_every_dialect(self):
        for d in ALL:
            for label, e in self.VALID.items():
                if d == MS and "LENGTH" in e:
                    continue                      # LENGTH does not exist in SQL Server (existing dialect rule)
                if d == MY and "CURRENT_TIMESTAMP" in e:
                    continue                      # nondeterministic: rejected for MySQL/MariaDB since SQL repair B
                with self.subTest(dialect=d, case=label):
                    self.assertEqual(check_unknown_columns(e, COLS, d, "items"), [])
                    render_check(e, COLS, d, "items: check c", table="items")

    def test_dialect_specific_functions_preserved(self):
        self.assertEqual(check_unknown_columns("LEN(name) > 0", COLS, MS, "items"), [])
        self.assertEqual(check_unknown_columns("char_length(name) > 0", COLS, PG, "items"), [])
        self.assertEqual(check_unknown_columns("name ~ '^a' AND qty::int > 0", COLS, PG, "items"), [])
        self.assertEqual(check_unknown_columns("EXTRACT(year FROM created) > 2000", COLS, PG, "items"), [])
        self.assertEqual(check_unknown_columns("name REGEXP '^a'", COLS, MY, "items"), [])
        self.assertEqual(check_unknown_columns("`name` <> ''", COLS, MY, "items"), [])
        self.assertEqual(check_unknown_columns("[name] <> ''", COLS, MS, "items"), [])
        self.assertEqual(check_unknown_columns('"name" <> \'\'', COLS, PG, "items"), [])
        self.assertEqual(check_unknown_columns("name GLOB 'a*' AND json_valid(name)", COLS, LITE, "items"), [])

    def test_reserved_word_columns_still_quoted(self):
        sql = gen(PG, tab("t", [col("id"), col("order")], ["id"], ck=[("ck_o", '"order" > 0 AND id > 0')])).sql
        self.assertIn('"order"', sql)
        sql = gen(PG, tab("t", [col("id"), col("order")], ["id"], ck=[("ck_o", "order > 0")])).sql
        self.assertIn('CHECK ("order" > 0)', sql)

    def test_generation_succeeds_and_executes_on_sqlite(self):
        sql = gen(LITE, table([("ck_price", "price >= 0"), ("ck_len", "LENGTH(TRIM(name)) > 0 AND qty >= 0"),
                               ("ck_status", "status IN ('a', 'b')")])).sql
        con = sqlite3.connect(":memory:")
        con.executescript(sql)
        con.execute("INSERT INTO items VALUES (1, 5, 1, 'a', 'n', 't')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO items VALUES (2, -5, 1, 'a', 'n', 't')")


class Negative(unittest.TestCase):
    def test_unknown_column_detected_on_every_dialect(self):
        for d in ALL:
            with self.subTest(dialect=d):
                self.assertEqual(check_unknown_columns("pric >= 0", COLS, d, "items"), ["pric"])
                self.assertEqual(check_unknown_columns("price >= 0 AND missing_col IS NOT NULL", COLS, d, "items"),
                                 ["missing_col"])
                self.assertEqual(check_unknown_columns("COALESCE(ghost, 0) > 0", COLS, d, "items"), ["ghost"])
                self.assertEqual(check_unknown_columns("CASE WHEN ghost > 0 THEN 1 ELSE 0 END = 1", COLS, d, "items"),
                                 ["ghost"])

    def test_column_of_another_table_rejected(self):
        for d in ALL:
            with self.subTest(dialect=d):
                parent = tab("owners", [col("id"), col("phone", "TEXT")], ["id"])
                child = tab("pets", [col("id"), col("owner_id")], ["id"], ck=[("ck_x", "phone IS NOT NULL")])
                with self.assertRaises(SchemaGenerationError) as cm:
                    gen(d, parent, child)
                self.assertIn("phone", str(cm.exception))
                self.assertIn("pets", str(cm.exception))

    def test_wrong_qualifier_rejected(self):
        self.assertEqual(check_unknown_columns("other.price > 0", COLS, PG, "items"), ["other.price"])
        self.assertEqual(check_unknown_columns("items.nope > 0", COLS, PG, "items"), ["nope"])

    def test_quoted_unknown_column_rejected(self):
        self.assertEqual(check_unknown_columns('"nope" > 0', COLS, PG, "items"), ["nope"])
        self.assertEqual(check_unknown_columns("`nope` > 0", COLS, MY, "items"), ["nope"])
        self.assertEqual(check_unknown_columns("[nope] > 0", COLS, MS, "items"), ["nope"])

    def test_mysql_double_quotes_are_strings_not_columns(self):
        self.assertEqual(check_unknown_columns('status = "nope"', COLS, MY, "items"), [])

    def test_error_raised_before_any_sql_is_produced(self):
        for d in ALL:
            with self.subTest(dialect=d):
                with self.assertRaises(SchemaGenerationError) as cm:
                    gen(d, table([("ck_bad", "prise >= 0")]))
                self.assertIn("unknown column", str(cm.exception))
                self.assertIn("prise", str(cm.exception))

    def test_sqlite_does_not_fall_back_to_string_literal(self):
        # SQLite would silently treat "nope" as a string; the generator must reject it instead.
        with self.assertRaises(SchemaGenerationError):
            gen(LITE, table([("ck_bad", '"nope" > 0')]))


if __name__ == "__main__":
    unittest.main()
