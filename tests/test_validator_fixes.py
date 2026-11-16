"""Regression tests: FK type compatibility and exact referenced keys (bugs found after Stage 4A)."""
import unittest

from dba_agent import design_model as dm
from dba_agent.errors import InvalidResponseError
from dba_agent.sql_types import canonical, fk_type_problem
from tests.design_fixtures import fresh


def table(l, name):
    return next(t for t in l["tables"] if t["name"] == name)


def col(t, name):
    return next(c for c in t["columns"] if c["name"] == name)


class TypeCompatibility(unittest.TestCase):
    OK = {
        "postgresql": [("INTEGER", "INT"), ("INT4", "integer"), ("INTEGER", "SERIAL"), ("BIGINT", "BIGSERIAL"),
                       ("VARCHAR(20)", "character varying(50)"), ("VARCHAR(20)", "TEXT"), ("NUMERIC(10,2)", "DECIMAL(10,2)"),
                       ("BOOLEAN", "bool"), ("TIMESTAMP", "timestamp without time zone"),
                       ("TIMESTAMPTZ", "timestamp with time zone"), ("UUID", "uuid"), ("smallint", "INT2")],
        "mysql_mariadb": [("INT", "INTEGER"), ("INT UNSIGNED", "int unsigned"), ("BIGINT", "bigint(20)"),
                          ("VARCHAR(20)", "VARCHAR(40)"), ("DECIMAL(8,2)", "NUMERIC(8,2)"), ("BOOLEAN", "TINYINT(1)"),
                          ("DATETIME", "datetime")],
        "sqlite": [("INTEGER", "INT"), ("INTEGER", "BIGINT"), ("TEXT", "VARCHAR(30)"), ("REAL", "FLOAT"),
                   ("NUMERIC", "NUMERIC"), ("DATE", "DATE")],
        "sql_server": [("INT", "INTEGER"), ("NVARCHAR(50)", "NVARCHAR(100)"), ("DECIMAL(9,2)", "NUMERIC(9,2)"),
                       ("BIT", "BIT"), ("DATETIME2(3)", "datetime2"), ("UNIQUEIDENTIFIER", "UNIQUEIDENTIFIER"),
                       ("BIGINT", "BIGINT")],
    }
    BAD = {
        "postgresql": [("VARCHAR(20)", "INTEGER"), ("INTEGER", "BIGINT"), ("INTEGER", "VARCHAR(20)"), ("NUMERIC(10,2)", "NUMERIC(12,2)"),
                       ("DATE", "TIMESTAMP"), ("TIMESTAMP", "TIMESTAMPTZ"), ("UUID", "TEXT"), ("BOOLEAN", "INTEGER"),
                       ("CHAR(5)", "VARCHAR(5)")],
        "mysql_mariadb": [("INT", "INT UNSIGNED"), ("INT", "BIGINT"), ("VARCHAR(20)", "INT"), ("VARCHAR(20)", "TEXT"),
                          ("DATETIME", "TIMESTAMP")],
        "sqlite": [("TEXT", "INTEGER"), ("INTEGER", "REAL"), ("BOOLEAN", "DATETIME"), ("BLOB", "TEXT")],
        "sql_server": [("VARCHAR(20)", "NVARCHAR(20)"), ("INT", "BIGINT"), ("INT", "VARCHAR(10)"),
                       ("DATETIME", "DATETIME2"), ("BIT", "INT")],
    }

    def test_compatible_pairs_are_accepted(self):
        for dbms, pairs in self.OK.items():
            for a, b in pairs:
                self.assertIsNone(fk_type_problem(a, b, dbms), f"{dbms}: {a} -> {b}")
                self.assertIsNone(fk_type_problem(b, a, dbms), f"{dbms}: {b} -> {a}")

    def test_incompatible_pairs_are_rejected_with_a_reason(self):
        for dbms, pairs in self.BAD.items():
            for a, b in pairs:
                msg = fk_type_problem(a, b, dbms)
                self.assertTrue(msg, f"{dbms}: {a} -> {b} should be rejected")
                self.assertIn("same type", msg)

    def test_unknown_types_are_not_guessed(self):
        self.assertIsNone(canonical("my_custom_enum", "postgresql"))
        self.assertIsNone(fk_type_problem("my_enum", "MY_ENUM", "postgresql"))          # identical text: fine
        self.assertIn("cannot verify", fk_type_problem("my_enum", "other_enum", "postgresql"))
        self.assertIn("cannot verify", fk_type_problem("geometry", "INTEGER", "postgresql"))


class FkTypesInDesign(unittest.TestCase):
    def check(self, mutate, dbms="postgresql"):
        c, l = fresh()
        conc = dm.validate_conceptual(c)
        mutate(l)
        return dm.validate_physical(l, conc, dbms)

    def test_reported_bug_integer_pk_varchar_fk_is_rejected(self):
        def mutate(l):
            col(table(l, "pets"), "owner_id")["data_type"] = "VARCHAR(20)"
        with self.assertRaises(InvalidResponseError) as cm:
            self.check(mutate)
        msg = str(cm.exception)
        self.assertIn("pets.owner_id", msg)
        self.assertIn("owners.owner_id", msg)
        self.assertIn("not compatible", msg)

    def test_valid_alias_pair_is_accepted_in_a_design(self):
        def mutate(l):
            col(table(l, "owners"), "owner_id")["data_type"] = "INT"
            col(table(l, "pets"), "owner_id")["data_type"] = "INTEGER"
        self.check(mutate)

    def test_each_dbms_rejects_the_mismatch(self):
        for dbms, bad in (("mysql_mariadb", "VARCHAR(20)"), ("sqlite", "TEXT"), ("sql_server", "NVARCHAR(20)")):
            def mutate(l, bad=bad):
                col(table(l, "pets"), "owner_id")["data_type"] = bad
            with self.assertRaises(InvalidResponseError, msg=dbms):
                self.check(mutate, dbms)

    def test_composite_fk_types_are_checked_pairwise(self):
        def mutate(l):                       # pet_vaccinations (pet_id, vaccine_id) -> swap one type
            col(table(l, "pet_vaccinations"), "vaccine_id")["data_type"] = "DATE"
        with self.assertRaises(InvalidResponseError) as cm:
            self.check(mutate)
        self.assertIn("pet_vaccinations.vaccine_id", str(cm.exception))


class ReferencedKeys(unittest.TestCase):
    def check(self, mutate, dbms="postgresql"):
        c, l = fresh()
        conc = dm.validate_conceptual(c)
        mutate(l)
        return dm.validate_physical(l, conc, dbms)

    def test_pk_plus_separate_unique_column_is_not_a_key(self):
        """owners PK(owner_id) + UNIQUE(email): FK (owner_id, email) must be rejected."""
        def mutate(l):
            pets = table(l, "pets")
            pets["columns"].append({"name": "owner_email", "data_type": "VARCHAR(200)", "nullable": False, "identity": False,
                                    "source_attribute": None, "default": None, "description": ""})
            fk = pets["foreign_keys"][0]
            fk.update(columns=["owner_id", "owner_email"], ref_columns=["owner_id", "email"])
        with self.assertRaises(InvalidResponseError) as cm:
            self.check(mutate)
        self.assertIn("EXACTLY", str(cm.exception))
        self.assertIn("PRIMARY KEY (owner_id)", str(cm.exception))

    def test_superset_of_a_key_is_not_a_key(self):
        def mutate(l):
            pets = table(l, "pets")
            pets["columns"].append({"name": "owner_name", "data_type": "VARCHAR(120)", "nullable": False, "identity": False,
                                    "source_attribute": None, "default": None, "description": ""})
            pets["foreign_keys"][0].update(columns=["owner_id", "owner_name"], ref_columns=["owner_id", "full_name"])
        with self.assertRaises(InvalidResponseError):
            self.check(mutate)

    def test_valid_single_unique_column_reference(self):
        def mutate(l):                       # reference owners.email (a declared UNIQUE) instead of the PK
            pets = table(l, "pets")
            col(pets, "owner_id")["data_type"] = "VARCHAR(200)"
            pets["foreign_keys"][0].update(ref_columns=["email"])
        self.check(mutate)

    def test_valid_composite_unique_key_in_either_order(self):
        def mutate(l):
            owners = table(l, "owners")
            owners["unique_constraints"].append({"name": "uq_owners_id_email", "columns": ["owner_id", "email"]})
            pets = table(l, "pets")
            pets["columns"].append({"name": "owner_email", "data_type": "VARCHAR(200)", "nullable": False, "identity": False,
                                    "source_attribute": None, "default": None, "description": ""})
            pets["foreign_keys"][0].update(columns=["owner_email", "owner_id"], ref_columns=["email", "owner_id"])
        self.check(mutate)                   # order differs from the declared key: fine outside MySQL
        with self.assertRaises(InvalidResponseError):
            self.check(mutate, "mysql_mariadb")   # MySQL/MariaDB wants the key's own column order

    def test_unique_index_counts_as_a_key(self):
        def mutate(l):
            owners = table(l, "owners")
            owners["unique_constraints"] = []
            owners["indexes"].append({"name": "ux_owners_email", "columns": ["email"], "unique": True, "purpose": "lookup"})
            pets = table(l, "pets")
            col(pets, "owner_id")["data_type"] = "VARCHAR(200)"
            pets["foreign_keys"][0].update(ref_columns=["email"])
        self.check(mutate)

    def test_non_unique_index_is_not_a_key(self):
        def mutate(l):
            owners = table(l, "owners")
            owners["unique_constraints"] = []
            owners["indexes"].append({"name": "ix_owners_email", "columns": ["email"], "unique": False, "purpose": "x"})
            pets = table(l, "pets")
            col(pets, "owner_id")["data_type"] = "VARCHAR(200)"
            pets["foreign_keys"][0].update(ref_columns=["email"])
        with self.assertRaises(InvalidResponseError):
            self.check(mutate)

    def test_duplicate_referenced_column_rejected(self):
        def mutate(l):
            pets = table(l, "pets")
            pets["foreign_keys"][0].update(columns=["owner_id", "owner_id"], ref_columns=["owner_id", "owner_id"])
        with self.assertRaises(InvalidResponseError):
            self.check(mutate)

    def test_junction_composite_pk_and_self_reference_still_work(self):
        c, l = fresh()
        out = dm.validate_physical(l, dm.validate_conceptual(c), "postgresql")
        names = {t["name"] for t in out["tables"]}
        self.assertIn("pet_vaccinations", names)           # composite PK junction
        self.assertTrue(any(fk["ref_table"] == "vets" for t in out["tables"] if t["name"] == "vets"
                            for fk in t["foreign_keys"]))   # self reference


if __name__ == "__main__":
    unittest.main()
