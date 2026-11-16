"""SQL repair B: MySQL/MariaDB compatibility (static checks only; not executed against a server)."""
import unittest

from dba_agent.schema_sql import SchemaGenerationError, check_unknown_columns, check_type, generate_schema_sql
from tests.test_stage6a_schema import MY, PG, LITE, col, fk, gen, tab


def parent():
    return tab("p", [col("id")], ["id"])


class MySQLRepairB(unittest.TestCase):
    def fails(self, *tables, text=""):
        with self.assertRaises(SchemaGenerationError) as cm:
            gen(MY, *tables)
        self.assertIn(text, str(cm.exception))

    def test_check_on_auto_increment_rejected(self):
        self.fails(tab("t", [col("id", identity=True), col("n")], ["id"], ck=[("ck", "id > 0")]), text="AUTO_INCREMENT")

    def test_check_on_other_column_with_auto_increment_present_ok(self):
        gen(MY, tab("t", [col("id", identity=True), col("n")], ["id"], ck=[("ck", "n > 0")]))

    def test_check_with_fk_on_update_cascade_rejected(self):
        t = tab("c", [col("id"), col("pid")], ["id"], fks=[fk("f", ["pid"], "p", ["id"], ou="CASCADE")],
                ck=[("ck", "pid > 0")])
        self.fails(parent(), t, text="3823")

    def test_check_with_fk_on_delete_set_null_rejected(self):
        t = tab("c", [col("id"), col("pid", nullable=True)], ["id"],
                fks=[fk("f", ["pid"], "p", ["id"], od="SET NULL")], ck=[("ck", "pid > 0")])
        self.fails(parent(), t, text="SET NULL")

    def test_check_with_fk_no_action_or_restrict_ok(self):
        t = tab("c", [col("id"), col("pid")], ["id"], fks=[fk("f", ["pid"], "p", ["id"], od="RESTRICT")],
                ck=[("ck", "pid > 0")])
        self.assertIn("CHECK", gen(MY, parent(), t).sql)

    def test_fk_conflict_is_mysql_only(self):
        t = tab("c", [col("id"), col("pid")], ["id"], fks=[fk("f", ["pid"], "p", ["id"], ou="CASCADE")],
                ck=[("ck", "pid > 0")])
        gen(PG, parent(), t)

    def test_nondeterministic_check_functions_rejected(self):
        for e in ("created < NOW()", "n > RAND()", "created < CURRENT_TIMESTAMP", "n > UUID_SHORT()"):
            with self.subTest(e=e):
                self.fails(tab("t", [col("id"), col("n"), col("created", "DATETIME")], ["id"], ck=[("ck", e)]),
                           text="nondeterministic")

    def test_nondeterministic_check_still_allowed_elsewhere_and_columns_named_user_ok(self):
        gen(PG, tab("t", [col("id"), col("d", "TIMESTAMP")], ["id"], ck=[("ck", "d <= NOW()")]))
        gen(MY, tab("t", [col("id"), col("user", "VARCHAR(20)")], ["id"], ck=[("ck", "LENGTH(`user`) > 0")]))

    def test_required_type_parameters(self):
        for typ in ("VARCHAR", "VARBINARY", "ENUM", "SET", "varchar()"):
            with self.subTest(typ=typ):
                with self.assertRaises(SchemaGenerationError):
                    check_type(typ, MY, "t.c")
        for typ in ("VARCHAR(20)", "VARBINARY(16)", "ENUM('a','b')", "SET('x')", "TEXT", "INT"):
            check_type(typ, MY, "t.c")
        check_type("VARCHAR", LITE, "t.c")                                   # other dialects unchanged

    def test_varchar_length_limit_utf8mb4(self):
        check_type("VARCHAR(16383)", MY, "t.c")
        with self.assertRaises(SchemaGenerationError) as cm:
            check_type("VARCHAR(16384)", MY, "t.c")
        self.assertIn("utf8mb4", str(cm.exception))
        check_type("VARCHAR(20000)", PG, "t.c")

    def test_row_size_limit(self):
        self.fails(tab("t", [col("id"), col("a", "VARCHAR(10000)"), col("b", "VARCHAR(10000)")], ["id"]),
                   text="row size too large")
        gen(MY, tab("t", [col("id"), col("a", "VARCHAR(10000)"), col("b", "TEXT")], ["id"]))

    def test_timestampdiff_unit_not_unknown_column(self):
        cols = ["start_at", "end_at"]
        self.assertEqual(check_unknown_columns("TIMESTAMPDIFF(DAY, start_at, end_at) >= 0", cols, MY, "t"), [])
        self.assertEqual(check_unknown_columns("end_at < start_at + INTERVAL 1 DAY", cols, MY, "t"), [])
        self.assertEqual(check_unknown_columns("TIMESTAMPDIFF(DAY, start_at, nope) >= 0", cols, MY, "t"), ["nope"])


if __name__ == "__main__":
    unittest.main()
