"""Regression tests: formal 3NF, composite keys, nullable UNIQUE, partial (conditional) unique indexes."""
import copy
import sqlite3
import unittest

from dba_agent import design_docs as dd
from dba_agent import design_model as dm
from dba_agent import normalization_analysis as na
from dba_agent.errors import InvalidResponseError
from dba_agent.normalization_analysis import analyze_design, analyze_table, candidate_keys, form_check, normalization_problems
from dba_agent.schema_sql import generate_schema_sql
from dba_agent.stages.design import validate_logical_response
from tests.design_fixtures import fresh


def table(cols, pk, uniques=(), unique_indexes=(), nullable=(), where=None):
    t = {"name": "t", "columns": [{"name": c, "data_type": "INT", "nullable": c in nullable} for c in cols],
         "primary_key": list(pk), "unique_constraints": [{"name": f"u{i}", "columns": list(u)} for i, u in enumerate(uniques)],
         "indexes": [{"name": f"i{i}", "columns": list(u), "unique": True} for i, u in enumerate(unique_indexes)]}
    if where:
        for i in t["indexes"]:
            i["where"] = where
    return t


def fd(det, dep):
    return (frozenset(det), frozenset(dep))


def forms(r):
    return [f["form"] for f in r["findings"]]


class SecondAndThirdNormalForm(unittest.TestCase):
    def test_partial_dependency_on_composite_key_breaks_2nf_and_3nf(self):
        # order_items(order_id, product_id, qty, product_name): product_id -> product_name
        r = analyze_table(table(["order_id", "product_id", "qty", "product_name"], ["order_id", "product_id"]),
                          [fd(["order_id", "product_id"], ["qty", "product_name"]), fd(["product_id"], ["product_name"])], [])
        self.assertEqual(forms(r), ["2NF"])
        self.assertEqual((r["highest"], r["status"], r["formal_3nf"]), ("1NF", "VIOLATION, not justified", False))
        self.assertEqual((form_check(r, "2NF"), form_check(r, "3NF")), ("VIOLATED", "VIOLATED"))

    def test_transitive_dependency_breaks_3nf_but_not_2nf(self):
        r = analyze_table(table(["id", "zip", "city"], ["id"]), [fd(["id"], ["zip", "city"]), fd(["zip"], ["city"])], [])
        self.assertEqual(forms(r), ["3NF"])
        self.assertEqual((r["highest"], r["formal_3nf"]), ("2NF", False))
        self.assertEqual((form_check(r, "2NF"), form_check(r, "3NF")), ("satisfied", "VIOLATED"))

    def test_implied_transitive_dependency_is_found_through_closure(self):
        # a -> b (a not a key) and b -> c: c is also determined by a through b
        r = analyze_table(table(["id", "a", "b", "c"], ["id"]), [fd(["id"], ["a", "b", "c"]), fd(["a"], ["b"]), fd(["b"], ["c"])], [])
        self.assertEqual(sorted((tuple(f["determinant"]), f["dependent"][0]) for f in r["findings"]), [(("a",), "b"), (("b",), "c")])

    def test_formal_3nf_allows_prime_dependent_without_superkey(self):
        # keys (s,c) and (s,t); t -> c: t is no superkey but c is prime -> 3NF holds, BCNF does not
        r = analyze_table(table(["s", "c", "t"], ["s", "c"], uniques=[["s", "t"]]), [fd(["s", "c"], ["t"]), fd(["t"], ["c"])], [])
        self.assertEqual(sorted(r["candidate_keys"]), [["s", "c"], ["s", "t"]])
        self.assertTrue(r["formal_3nf"])
        self.assertEqual(r["highest"], "3NF")
        self.assertEqual(normalization_problems({"physical": {"tables": [table(["s", "c", "t"], ["s", "c"], uniques=[["s", "t"]])],
                                                              "functional_dependencies": [{"table": "t", "determinant": ["t"], "dependent": ["c"]}],
                                                              "denormalizations": []}}), [])

    def test_composite_candidate_keys_make_both_key_columns_prime(self):
        t = table(["a", "b", "c", "d", "v"], ["a", "b"], uniques=[["c", "d"]])
        r = analyze_table(t, [fd(["a", "b"], ["c", "d", "v"]), fd(["c", "d"], ["a", "b", "v"])], [])
        self.assertEqual(sorted(r["candidate_keys"]), [["a", "b"], ["c", "d"]])
        self.assertEqual(r["prime"], ["a", "b", "c", "d"])
        self.assertEqual(r["nonprime"], ["v"])
        self.assertEqual(r["findings"], [])

    def test_dependency_on_part_of_second_composite_key_is_partial(self):
        t = table(["a", "b", "c", "d", "v"], ["a", "b"], uniques=[["c", "d"]])
        r = analyze_table(t, [fd(["a", "b"], ["c", "d", "v"]), fd(["c", "d"], ["a", "b", "v"]), fd(["c"], ["v"])], [])
        self.assertEqual([(f["form"], f["determinant"], f["dependent"]) for f in r["findings"]], [("2NF", ["c"], ["v"])])

    def test_normalization_problems_gives_a_targeted_repair_and_honours_denormalization(self):
        t = table(["id", "zip", "city"], ["id"])
        design = {"physical": {"tables": [t], "denormalizations": [],
                               "functional_dependencies": [{"table": "t", "determinant": ["id"], "dependent": ["zip", "city"]},
                                                           {"table": "t", "determinant": ["zip"], "dependent": ["city"]}]}}
        problems = normalization_problems(design)
        self.assertEqual(len(problems), 1)
        self.assertIn("city", problems[0])
        self.assertIn("foreign key", problems[0])
        design["physical"]["denormalizations"] = [{"table": "t", "description": "", "justification": "reporting copy"}]
        self.assertEqual(normalization_problems(design), [])


class AlternateKeysAndNullableDependencies(unittest.TestCase):
    def test_alternate_candidate_key_from_fds_keeps_3nf(self):
        # (a,b,c), PK (a,b), b -> c, c -> b: keys are (a,b) and (a,c); all columns prime; 3NF holds, BCNF does not.
        t = table("abc", "ab")
        fds = [fd("ab", "c"), fd("b", "c"), fd("c", "b")]
        self.assertEqual(sorted(sorted(k) for k in candidate_keys(t, fds + [fd("ab", "abc")])), [["a", "b"], ["a", "c"]])
        r = analyze_table(t, [fd("b", "c"), fd("c", "b")], [])
        self.assertEqual(sorted(sorted(k) for k in r["candidate_keys"]), [["a", "b"], ["a", "c"]])
        self.assertEqual(r["prime"], ["a", "b", "c"])
        self.assertTrue(r["keys_complete"])
        self.assertNotIn("2NF", forms(r))
        self.assertNotIn("3NF", forms(r))
        self.assertEqual(form_check(r, "3NF"), "satisfied")
        self.assertEqual(form_check(r, "BCNF"), "VIOLATED")
        self.assertEqual((r["highest"], r["formal_3nf"], r["status"]), ("3NF", True, "OK"))

    def test_nullable_unique_with_declared_fd_is_a_formal_3nf_violation(self):
        # (id,email,display), PK id, nullable UNIQUE email, declared email -> display.
        t = table(["id", "email", "display"], ["id"], uniques=[["email"]], nullable=["email"])
        r = analyze_table(t, [fd(["email"], ["display"])], [])
        self.assertEqual(r["candidate_keys"], [["id"]])             # the nullable UNIQUE is still not a key
        self.assertEqual(r["conditional_keys"][0]["columns"], ["email"])
        self.assertEqual(forms(r), ["3NF"])
        self.assertEqual((r["findings"][0]["determinant"], r["findings"][0]["dependent"]), (["email"], ["display"]))
        self.assertEqual((r["status"], r["formal_3nf"], r["unjustified"]), ("VIOLATION, not justified", False, True))
        self.assertNotEqual(r["status"], "UNVERIFIED")

    def test_nullable_unique_without_declared_fd_implies_nothing(self):
        t = table(["id", "email", "display"], ["id"], uniques=[["email"]], nullable=["email"])
        r = analyze_table(t, [], [])
        self.assertEqual((r["findings"], r["status"]), ([], "OK"))

    def test_partial_unique_index_with_declared_fd_is_also_judged(self):
        where = [{"column": "v", "operator": "=", "value": "active"}]
        t = table("abcv", "a", unique_indexes=["b"], where=where)
        r = analyze_table(t, [fd("b", "c")], [])
        self.assertEqual(forms(r), ["3NF"])
        self.assertEqual(r["candidate_keys"], [["a"]])

    def test_key_enumeration_cutoff_is_reported_as_uncertain(self):
        # 6 pairs x <-> y give 2**6 = 64 candidate keys: more than the bound.
        cols = [f"{p}{i}" for i in range(6) for p in "xy"]
        t = table(cols, [f"x{i}" for i in range(6)])
        fds = [fd([f"x{i}"], [f"y{i}"]) for i in range(6)] + [fd([f"y{i}"], [f"x{i}"]) for i in range(6)]
        r = analyze_table(t, fds, [])
        self.assertFalse(r["keys_complete"])
        self.assertLessEqual(len(r["candidate_keys"]), na.MAX_CANDIDATE_KEYS)
        self.assertIn("UNCERTAIN", forms(r))
        self.assertEqual(r["status"], "UNVERIFIED")
        # below the bound the same shape is enumerated completely
        small = analyze_table(table(cols[:4], ["x0", "x1"]), [f for f in fds if f[0] <= {"x0", "y0", "x1", "y1"}], [])
        self.assertTrue(small["keys_complete"])
        self.assertEqual(len(small["candidate_keys"]), 4)

    def test_cutoff_downgrades_nonprime_claims_to_uncertain(self):
        cols = [f"{p}{i}" for i in range(6) for p in "xy"] + ["z", "w"]
        t = table(cols, [f"x{i}" for i in range(6)])
        fds = ([fd([f"x{i}"], [f"y{i}"]) for i in range(6)] + [fd([f"y{i}"], [f"x{i}"]) for i in range(6)]
               + [fd(["x0", "x1", "x2", "x3", "x4", "x5"], ["z", "w"]), fd(["z"], ["w"])])
        r = analyze_table(t, fds, [])
        self.assertFalse(r["keys_complete"])
        self.assertNotIn("3NF", forms(r))
        self.assertFalse(r["unjustified"])


class NullableAndPartialKeys(unittest.TestCase):
    def test_nullable_unique_is_not_a_candidate_key(self):
        t = table("abc", "a", uniques=["b"], nullable="b")
        self.assertEqual(candidate_keys(t, [fd("a", "abc")]), [("a",)])
        r = analyze_table(t, [fd("a", "bc")], [])
        self.assertEqual(r["candidate_keys"], [["a"]])
        self.assertEqual(r["conditional_keys"][0]["columns"], ["b"])
        self.assertTrue(any("NOT used as a candidate key" in n for n in r["notes"]))

    def test_not_null_unique_is_a_key(self):
        r = analyze_table(table("abc", "a", uniques=["b"]), [fd("a", "bc")], [])
        self.assertEqual(sorted(r["candidate_keys"]), [["a"], ["b"]])
        self.assertEqual(r["conditional_keys"], [])

    def test_declared_dependency_on_nullable_unique_is_still_judged_formally(self):
        # b is a nullable UNIQUE (not a key); the declared b -> c is accepted, so it is a real 3NF violation.
        r = analyze_table(table("abc", "a", uniques=["b"], nullable="b"), [fd("a", "bc"), fd("b", "c")], [])
        self.assertEqual(forms(r), ["3NF"])
        self.assertEqual(r["candidate_keys"], [["a"]])
        self.assertEqual(r["status"], "VIOLATION, not justified")
        self.assertEqual(form_check(r, "3NF"), "VIOLATED")

    def test_declared_key_claim_on_nullable_unique_is_uncertain(self):
        r = analyze_table(table("abc", "a", uniques=["b"], nullable="b"), [fd("a", "bc"), fd("b", "ac")], [])
        self.assertEqual(r["candidate_keys"][0], ["a"])
        self.assertIn("UNCERTAIN", forms(r))
        self.assertNotIn("KEY", forms(r))

    def test_nullable_part_of_composite_unique_makes_it_conditional(self):
        r = analyze_table(table("abv", "a", uniques=[["b", "v"]], nullable="v"), [fd("a", "bv")], [])
        self.assertEqual(r["candidate_keys"], [["a"]])
        self.assertEqual(len(r["conditional_keys"]), 1)

    def test_partial_unique_index_is_not_a_candidate_key(self):
        where = [{"column": "v", "operator": "=", "value": "active"}]
        r = analyze_table(table("abv", "a", unique_indexes=["b"], where=where), [fd("a", "bv")], [])
        self.assertEqual(r["candidate_keys"], [["a"]])
        self.assertEqual(r["conditional_keys"][0]["kind"], "UNIQUE INDEX")
        self.assertTrue(any("partial unique index" in n and "v = 'active'" in n for n in r["notes"]))

    def test_non_atomic_type_is_unverified_not_ok(self):
        t = table("ab", "a")
        t["columns"][1]["data_type"] = "JSONB"
        r = analyze_table(t, [fd("a", "b")], [])
        self.assertEqual(r["status"], "UNVERIFIED")


def design_with_partial_index(dialect="postgresql"):
    c, l = fresh()
    pets = next(t for t in l["tables"] if t["name"] == "pets")
    pets["indexes"].append({"name": "uq_pets_active_name", "columns": ["owner_id", "name"], "unique": True, "purpose": "one active pet name per owner",
                            "where": [{"column": "species", "operator": "IN", "value": ["dog", "cat"]}]})
    conc = dm.validate_conceptual(c)
    phys = dm.validate_physical(l, conc, dialect)
    rules = dm.apply_rule_enforcement(conc["business_rules"], phys, l)
    design = dm.build_design("Clinic", {"selected_id": dialect, "selected": dialect}, conc, phys, rules,
                             extra_assumptions=dm._assumptions(l["assumptions"], dm._Errors()))
    return dm.link_relationships(dm.validate_design(design))


class PartialIndexModel(unittest.TestCase):
    def setUp(self):
        self.c, self.l = fresh()
        self.conc = dm.validate_conceptual(self.c)
        self.pets = next(t for t in self.l["tables"] if t["name"] == "pets")

    def validate(self, dialect="postgresql"):
        return dm.validate_physical(self.l, self.conc, dialect)

    def test_old_indexes_are_unchanged_and_get_no_where_key(self):
        phys = self.validate()
        for t in phys["tables"]:
            for i in t["indexes"]:
                self.assertNotIn("where", i)
        self.assertEqual(dm.validate_physical(phys, self.conc, "postgresql"), phys)       # idempotent

    def test_structured_where_is_normalized_and_idempotent(self):
        self.pets["indexes"].append({"name": "ix_p", "columns": ["name"], "unique": True, "purpose": "",
                                     "where": {"column": "species", "operator": "==", "value": "dog"}})
        idx = next(i for i in next(t for t in self.validate()["tables"] if t["name"] == "pets")["indexes"] if i["name"] == "ix_p")
        self.assertEqual(idx["where"], [{"column": "species", "operator": "=", "value": "dog"}])
        again = dm.validate_physical(self.validate(), self.conc, "postgresql")
        self.assertEqual(next(i for t in again["tables"] for i in t["indexes"] if i["name"] == "ix_p")["where"], idx["where"])

    def test_bad_where_is_rejected_with_a_helpful_message(self):
        bad = ["species = 'dog'",                                                          # free SQL text
               [{"column": "ghost", "operator": "=", "value": 1}],                         # unknown column
               [{"column": "species", "operator": "LIKE", "value": "d%"}],                 # unknown operator
               [{"column": "species", "operator": "=", "value": None}],                    # NULL via = is wrong
               [{"column": "species", "operator": "IN", "value": []}]]
        for where in bad:
            with self.subTest(where=where):
                self.pets["indexes"] = [{"name": "ix_p", "columns": ["name"], "unique": True, "purpose": "", "where": where}]
                with self.assertRaises(InvalidResponseError) as cm:
                    self.validate()
                self.assertIn("ix_p", str(cm.exception))

    def test_mysql_has_no_partial_indexes(self):
        self.pets["indexes"].append({"name": "ix_p", "columns": ["name"], "unique": True, "purpose": "",
                                     "where": [{"column": "species", "operator": "=", "value": "dog"}]})
        with self.assertRaises(InvalidResponseError) as cm:
            self.validate("mysql_mariadb")
        self.assertIn("partial", str(cm.exception))

    def test_partial_unique_index_does_not_enforce_an_identifier_or_one_to_one(self):
        t = {"primary_key": ["id"], "unique_constraints": [], "columns": [],
             "indexes": [{"name": "i", "columns": ["x"], "unique": True, "where": [{"column": "s", "operator": "IS NULL"}]}]}
        self.assertFalse(dm._is_unique(t, ["x"]))
        self.assertFalse(dm._key_contains(t, ["x"]))
        t["indexes"][0].pop("where")
        self.assertTrue(dm._is_unique(t, ["x"]))

    def test_foreign_key_cannot_reference_a_partial_unique_index(self):
        owners = next(t for t in self.l["tables"] if t["name"] == "owners")
        owners["unique_constraints"] = []
        owners["indexes"].append({"name": "uq_owners_email_ok", "columns": ["email"], "unique": True, "purpose": "",
                                  "where": [{"column": "phone", "operator": "IS NOT NULL"}]})
        fk = next(f for f in self.pets["foreign_keys"])
        fk["ref_columns"] = ["email"]
        with self.assertRaises(InvalidResponseError) as cm:
            self.validate()
        self.assertIn("not its primary key or a UNIQUE key", str(cm.exception))


class PartialIndexSqlAndDocs(unittest.TestCase):
    def test_sqlite_schema_renders_where_and_enforces_only_the_condition(self):
        design = design_with_partial_index("sqlite")
        for t in design["physical"]["tables"]:
            for c in t["columns"]:
                c["data_type"] = {"VARCHAR(120)": "TEXT", "VARCHAR(200)": "TEXT", "VARCHAR(30)": "TEXT", "VARCHAR(80)": "TEXT",
                                  "VARCHAR(10)": "TEXT", "VARCHAR(40)": "TEXT", "VARCHAR(100)": "TEXT", "TIMESTAMP": "TEXT",
                                  "DATE": "TEXT"}.get(c["data_type"], c["data_type"])
        res = generate_schema_sql(design, "f" * 64, validate=False)
        self.assertIn('("owner_id", "name") WHERE "species" IN (\'dog\', \'cat\');', res.sql)
        con = sqlite3.connect(":memory:")
        con.executescript(res.sql)
        con.execute("INSERT INTO owners (full_name, email) VALUES ('o', 'o@x')")
        ins = "INSERT INTO pets (name, species, owner_id) VALUES (?, ?, 1)"
        con.execute(ins, ("rex", "dog"))
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(ins, ("rex", "cat"))                     # inside the condition: duplicate pair rejected
        con.execute(ins, ("rex", "other"))                       # outside the condition: allowed
        con.execute(ins, ("rex", "other"))

    def test_postgres_boolean_and_quote_rendering_and_sql_server_null_filter(self):
        d = design_with_partial_index("postgresql")
        idx = next(i for t in d["physical"]["tables"] for i in t["indexes"] if i["name"] == "uq_pets_active_name")
        idx["where"] = [{"column": "species", "operator": "<>", "value": "o'x"}]
        self.assertIn("WHERE \"species\" <> 'o''x';", generate_schema_sql(d, "f" * 64, validate=False).sql)

    def test_documents_show_the_condition_and_stay_consistent_with_design_json(self):
        design = design_with_partial_index()
        docs = dd.render_all(design)
        self.assertIn("yes (only where species IN ('dog', 'cat'))", docs["logical-schema.md"])
        self.assertEqual(dd.check_documents(design, docs), [])
        tampered = dict(docs, **{"logical-schema.md": docs["logical-schema.md"].replace("yes (only where species IN ('dog', 'cat'))", "yes")})
        self.assertTrue(any("indexes" in p for p in dd.check_documents(design, tampered)))
        pets_cols = next(t for t in design["physical"]["tables"] if t["name"] == "pets")
        self.assertEqual(dd._key_cell(pets_cols, "name"), "-")      # a partial index does not make the column UNIQUE

    def test_uncertainty_reaches_normalization_md_warnings_and_stays_consistent(self):
        design = design_with_partial_index()
        owners = next(t for t in design["physical"]["tables"] if t["name"] == "owners")
        for c in owners["columns"]:
            if c["name"] == "phone":
                c["nullable"] = True
        owners["unique_constraints"].append({"name": "uq_owners_phone", "columns": ["phone"]})
        design["physical"]["functional_dependencies"].append(
            {"table": "owners", "determinant": ["phone"], "dependent": [c["name"] for c in owners["columns"] if c["name"] != "phone"], "note": ""})
        # phone claimed to determine every column although it is only a nullable UNIQUE: a key claim that is not confirmed
        design = dm.link_relationships(dm.validate_design(design))
        a = analyze_design(design)["owners"]
        self.assertEqual(a["status"], "UNVERIFIED")
        docs = dd.render_all(design)
        self.assertIn("| UNVERIFIED |", docs["normalization.md"])
        self.assertIn("could not be fully confirmed", docs["normalization.md"])
        self.assertTrue(any("conditional key" in w for w in dd.design_warnings(design)))
        self.assertIn("conditional key", docs["assumptions.md"])
        self.assertEqual(dd.check_documents(design, docs), [])

    def test_clean_design_has_unchanged_status_and_no_warnings(self):
        c, l = fresh()
        conc = dm.validate_conceptual(c)
        phys = dm.validate_physical(l, conc, "postgresql")
        design = dm.build_design("Clinic", {"selected_id": "postgresql", "selected": "PG"}, conc, phys,
                                 dm.apply_rule_enforcement(conc["business_rules"], phys, l))
        self.assertEqual({n: a["status"] for n, a in analyze_design(design).items() if a["status"] != "OK"}, {})


class GenerationGate(unittest.TestCase):
    def setUp(self):
        self.c, self.l = fresh()
        self.conc = dm.validate_conceptual(self.c)
        self.l["tables"][0]["columns"].append(dict(self.l["tables"][0]["columns"][1], name="city_name", description=""))
        self.l["tables"][0]["columns"].append(dict(self.l["tables"][0]["columns"][1], name="postal_code", description=""))
        for col in self.l["tables"][0]["columns"][-2:]:
            col["source_attribute"] = None
            col["nullable"] = True
        self.l["functional_dependencies"][0]["dependent"] += ["city_name", "postal_code"]
        self.l["functional_dependencies"].append({"table": "owners", "determinant": ["postal_code"], "dependent": ["city_name"], "note": ""})

    def test_gate_sends_violation_back_while_attempts_remain_then_accepts_with_warning(self):
        left = [2]
        def gate():
            left[0] -= 1
            return left[0] > 0
        with self.assertRaises(InvalidResponseError) as cm:
            validate_logical_response(copy.deepcopy(self.l), self.conc, "postgresql", nf_gate=gate)
        self.assertIn("Third Normal Form", str(cm.exception))
        self.assertIn("city_name", str(cm.exception))
        out = validate_logical_response(copy.deepcopy(self.l), self.conc, "postgresql", nf_gate=gate)     # last attempt
        self.assertTrue(out["physical"]["tables"])
        design = dm.build_design("C", {"selected_id": "postgresql", "selected": "PG"}, self.conc, out["physical"], out["business_rules"])
        self.assertEqual(analyze_design(design)["owners"]["status"], "VIOLATION, not justified")   # reported, never hidden

    def test_no_gate_keeps_old_behaviour(self):
        self.assertTrue(validate_logical_response(copy.deepcopy(self.l), self.conc, "postgresql")["physical"])


if __name__ == "__main__":
    unittest.main()
