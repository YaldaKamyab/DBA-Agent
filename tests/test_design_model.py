"""Stage 4A: canonical design validation, cardinalities, M:N, Mermaid, markdown."""
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from dba_agent import design_model as dm
from dba_agent.design_render import check_mermaid_syntax, mermaid_er, render_conceptual_model
from dba_agent.errors import InvalidResponseError
from tests.design_fixtures import fresh


def build(dbms="postgresql"):
    c, l = fresh()
    conc = dm.validate_conceptual(c)
    phys = dm.validate_physical(l, conc, dbms)
    rules = dm.apply_rule_enforcement(conc["business_rules"], phys, l)
    design = dm.build_design("Clinic", {"selected_id": dbms, "selected": dbms}, conc, phys, rules,
                             extra_assumptions=dm._assumptions(l["assumptions"], dm._Errors()),
                             requirement_gaps=["Opening hours unknown"])
    return dm.link_relationships(dm.validate_design(design))


class Cardinality(unittest.TestCase):
    def test_formats(self):
        P = dm.parse_cardinality
        self.assertEqual(P({"min": 0, "max": "N"}), {"min": 0, "max": "N"})
        self.assertEqual(P("0..1"), {"min": 0, "max": 1})
        self.assertEqual(P("1..*"), {"min": 1, "max": "N"})
        self.assertEqual(P("exactly_one"), {"min": 1, "max": 1})
        self.assertEqual(P("zero or many"), {"min": 0, "max": "N"})
        self.assertEqual(P({"min": "1", "max": 5}), {"min": 1, "max": "N"})

    def test_invalid(self):
        for bad in ("lots", {"min": 2, "max": 1}, {"min": 0}, None, "2..N"):
            with self.assertRaises(ValueError, msg=str(bad)):
                dm.parse_cardinality(bad)

    def test_classification(self):
        d = build()
        types = {r["id"]: r for r in d["conceptual"]["relationships"]}
        self.assertEqual(types["owner_pet"]["type"], "one_to_many")
        self.assertEqual((types["owner_pet"]["one_side"], types["owner_pet"]["many_side"]), ("owner", "pet"))
        self.assertEqual(types["pet_microchip"]["type"], "one_to_one")
        self.assertEqual(types["pet_vaccination"]["type"], "many_to_many")
        self.assertEqual(types["vet_supervision"]["one_side"], "vet")       # self-reference, roles differ

    def test_one_side_detected_regardless_of_participant_order(self):
        c, _ = fresh()
        r = next(r for r in c["relationships"] if r["id"] == "owner_pet")
        r["participants"].reverse()
        out = dm.validate_conceptual(c)
        rel = next(x for x in out["relationships"] if x["id"] == "owner_pet")
        self.assertEqual((rel["one_side"], rel["many_side"]), ("owner", "pet"))


class ConceptualValidation(unittest.TestCase):
    def fails(self, mutate, fragment):
        c, _ = fresh()
        mutate(c)
        with self.assertRaises(InvalidResponseError) as cm:
            dm.validate_conceptual(c)
        self.assertIn(fragment, str(cm.exception))

    def test_ok(self):
        c, _ = fresh()
        out = dm.validate_conceptual(c)
        self.assertEqual(len(out["entities"]), 6)

    def test_errors(self):
        self.fails(lambda c: c["relationships"][0]["participants"][0].update(entity="ghost"), "does not exist")
        self.fails(lambda c: c["entities"][0]["identifiers"].append({"attributes": ["owner_id"], "primary": True}), "exactly one identifier")
        self.fails(lambda c: c["entities"][0]["identifiers"][0].update(attributes=["nope"]), "unknown attributes")
        self.fails(lambda c: c["relationships"][2]["participants"][1].update(role="supervisor"), "different 'role'")
        self.fails(lambda c: c["relationships"][0]["attributes"].append({"name": "since", "type": "date"}), "only allowed on many-to-many")
        self.fails(lambda c: c["entities"][0].update(id="Order Item"), "snake_case")
        self.fails(lambda c: c["entities"][0]["attributes"][1].update(name="order"), "reserved SQL word")
        self.fails(lambda c: c["entities"][0]["attributes"][1].update(type="blob"), "must be one of")
        self.fails(lambda c: c["entities"][1]["attributes"][2].pop("allowed_values"), "allowed_values")
        self.fails(lambda c: c["entities"][0].update(kind="weak"), "identifying_relationship")
        self.fails(lambda c: c["relationships"][0]["participants"].pop(), "exactly 2 participants")
        self.fails(lambda c: c["entities"].append(copy.deepcopy(c["entities"][0])), "Duplicate")
        self.fails(lambda c: c["business_rules"][0].update(applies_to=["zzz"]), "unknown entity/relationship")

    def test_all_errors_reported_together(self):
        c, _ = fresh()
        c["entities"][0]["attributes"][1]["name"] = "group"
        c["relationships"][0]["participants"][0]["entity"] = "ghost"
        with self.assertRaises(InvalidResponseError) as cm:
            dm.validate_conceptual(c)
        self.assertIn("reserved", str(cm.exception))
        self.assertIn("ghost", str(cm.exception))

    def test_weak_entity_must_be_identified_through_its_relationship(self):
        c, _ = fresh()
        c["entities"][4].update(kind="weak", identifying_relationship="pet_appointment")
        out = dm.validate_conceptual(c)
        self.assertTrue(next(r for r in out["relationships"] if r["id"] == "pet_appointment")["identifying"])
        c["entities"][4]["identifying_relationship"] = "owner_pet"      # not its relationship
        with self.assertRaises(InvalidResponseError):
            dm.validate_conceptual(c)

    def test_assumptions_and_questions_normalised(self):
        c, _ = fresh()
        c["assumptions"] = ["plain string assumption", {"text": "with fields", "rationale": "r"}]
        out = dm.validate_conceptual(c)
        self.assertEqual([a["id"] for a in out["assumptions"]], ["A1", "A2"])
        c["assumptions"] = [{"text": ""}]
        with self.assertRaises(InvalidResponseError):
            dm.validate_conceptual(c)


class PhysicalValidation(unittest.TestCase):
    def fails(self, mutate, fragment, dbms=None):
        c, l = fresh()
        conc = dm.validate_conceptual(c)
        mutate(l)
        with self.assertRaises(InvalidResponseError) as cm:
            dm.validate_physical(l, conc, dbms)
        self.assertIn(fragment, str(cm.exception))

    def table(self, l, name):
        return next(t for t in l["tables"] if t["name"] == name)

    def test_valid_design_maps_every_layer(self):
        d = build()
        self.assertEqual({t["name"] for t in d["physical"]["tables"]},
                         {"owners", "pets", "microchips", "vets", "appointments", "vaccines", "pet_vaccinations"})
        self.assertEqual(next(t for t in d["physical"]["tables"] if t["name"] == "pet_vaccinations")["kind"], "junction")
        self.assertEqual(len(d["conceptual"]["entities"]), 6)       # junction table is NOT a conceptual entity
        self.assertNotIn("pet_vaccinations", [e["id"] for e in d["conceptual"]["entities"]])

    def test_link_relationships(self):
        d = build()
        rels = {r["id"]: r["implemented_by"] for r in d["conceptual"]["relationships"]}
        self.assertEqual(rels["owner_pet"], {"kind": "foreign_key", "table": "pets", "foreign_keys": ["fk_pets_owners"], "columns": ["owner_id"]})
        self.assertEqual(rels["pet_vaccination"]["kind"], "junction_table")
        self.assertEqual(rels["pet_vaccination"]["table"], "pet_vaccinations")
        self.assertEqual(rels["vet_supervision"]["table"], "vets")

    def test_missing_entity_table(self):
        self.fails(lambda l: l["tables"].pop(0), "entity 'owner' has no table")

    def test_missing_attribute_column(self):
        self.fails(lambda l: self.table(l, "owners")["columns"].pop(1), "no column with source_attribute 'full_name'")

    def test_required_attribute_vs_nullable_column(self):
        self.fails(lambda l: self.table(l, "owners")["columns"][1].update(nullable=True), "required but the column is nullable")

    def test_mandatory_relationship_requires_not_null_fk(self):
        self.fails(lambda l: self.table(l, "pets")["columns"][4].update(nullable=True), "must be NOT NULL")

    def test_optional_relationship_requires_nullable_fk(self):
        self.fails(lambda l: self.table(l, "appointments")["columns"][5].update(nullable=False), "must be nullable")

    def test_missing_fk_for_one_to_many(self):
        self.fails(lambda l: self.table(l, "pets").update(foreign_keys=[]), "owner_pet")

    def test_fk_on_wrong_side_rejected(self):
        def flip(l):
            self.table(l, "pets")["foreign_keys"][0].update(relationship="nonexistent")
        self.fails(flip, "owner_pet")

    def test_one_to_one_requires_unique_fk(self):
        self.fails(lambda l: self.table(l, "microchips").update(unique_constraints=[]), "one-to-one")

    def test_many_to_many_requires_junction_with_both_fks(self):
        self.fails(lambda l: l["tables"].pop(), "junction table")
        def drop_fk(l):
            self.table(l, "pet_vaccinations")["foreign_keys"].pop()
        self.fails(drop_fk, "needs one FK")

    def test_junction_without_pair_key_rejected(self):
        def surrogate_only(l):
            j = self.table(l, "pet_vaccinations")
            j["columns"].insert(0, {"name": "id", "data_type": "INTEGER", "nullable": False, "identity": True,
                                    "source_attribute": None, "default": None, "description": ""})
            j["primary_key"] = ["id"]
            j["unique_constraints"] = []
        self.fails(surrogate_only, "composite PRIMARY KEY or UNIQUE")

    def test_relationship_attribute_needs_column(self):
        def drop(l):
            j = self.table(l, "pet_vaccinations")
            j["columns"] = [c for c in j["columns"] if c["name"] != "batch_no"]
        self.fails(drop, "batch_no")

    def test_identifier_must_stay_enforced_with_surrogate_key(self):
        self.fails(lambda l: self.table(l, "owners").update(unique_constraints=[]), "not enforced")

    def test_fk_integrity_checks(self):
        self.fails(lambda l: self.table(l, "pets")["foreign_keys"][0].update(ref_table="ghost"), "unknown table")
        self.fails(lambda l: self.table(l, "pets")["foreign_keys"][0].update(ref_columns=["ghost"]), "must exist in")
        self.fails(lambda l: self.table(l, "pets")["foreign_keys"][0].update(on_delete="SET NULL"), "SET NULL cannot")
        self.fails(lambda l: self.table(l, "pets")["foreign_keys"][0].update(on_delete="EXPLODE"), "must be one of")
        self.fails(lambda l: self.table(l, "pets")["foreign_keys"][0].update(ref_table="vets", ref_columns=["full_name"]),
                   "not its primary key or a UNIQUE")

    def test_sql_server_rejects_restrict(self):
        self.fails(lambda l: None, "RESTRICT, which SQL Server does not support", dbms="sql_server")

    def test_duplicate_and_reserved_names(self):
        self.fails(lambda l: self.table(l, "pets")["indexes"][0].update(name="pk_pets"), "Duplicate constraint/index name")
        self.fails(lambda l: self.table(l, "owners").update(name="user"), "reserved SQL word")

    def test_fd_rules(self):
        self.fails(lambda l: l.update(functional_dependencies=[]), "no functional dependency listed")
        self.fails(lambda l: l["functional_dependencies"][0].update(dependent=["ghost"]), "unknown columns")
        self.fails(lambda l: l.update(denormalizations=[{"table": "pets", "description": "x", "justification": ""}]),
                   "justification")

    def test_weak_entity_owner_fk_must_be_in_pk(self):
        c, l = fresh()
        c["entities"][4].update(kind="weak", identifying_relationship="pet_appointment")
        conc = dm.validate_conceptual(c)
        with self.assertRaises(InvalidResponseError) as cm:
            dm.validate_physical(l, conc)
        self.assertIn("part of the primary key", str(cm.exception))

    def test_business_rule_enforcement(self):
        c, l = fresh()
        conc = dm.validate_conceptual(c)
        phys = dm.validate_physical(l, conc)
        l["business_rule_enforcement"].pop(0)
        with self.assertRaises(InvalidResponseError):
            dm.apply_rule_enforcement(conc["business_rules"], phys, l)
        c, l = fresh()
        l["business_rule_enforcement"][0]["implemented_by"] = ["ck_does_not_exist"]
        with self.assertRaises(InvalidResponseError):
            dm.apply_rule_enforcement(conc["business_rules"], phys, l)
        c, l = fresh()
        l["business_rule_enforcement"][2]["note"] = ""
        with self.assertRaises(InvalidResponseError):
            dm.apply_rule_enforcement(conc["business_rules"], phys, l)


class CanonicalDesign(unittest.TestCase):
    def test_json_roundtrip_and_idempotent_revalidation(self):
        d = build()
        again = json.loads(json.dumps(d))
        self.assertEqual(dm.link_relationships(dm.validate_design(again)), d)

    def test_schema_version_and_tampering_detected(self):
        d = build()
        bad = copy.deepcopy(d)
        bad["schema_version"] = 99
        with self.assertRaises(InvalidResponseError):
            dm.validate_design(bad)
        bad = copy.deepcopy(d)
        bad["physical"]["tables"][1]["foreign_keys"][0]["ref_table"] = "ghost"
        with self.assertRaises(InvalidResponseError):
            dm.validate_design(bad)

    def test_dbms_switch_keeps_conceptual_layer_separate(self):
        d = build()
        self.assertEqual(set(d), {"schema_version", "project_name", "dbms", "conceptual", "physical", "business_rules",
                                  "assumptions", "open_questions", "requirement_gaps", "meta"})
        self.assertNotIn("tables", d["conceptual"])
        self.assertNotIn("entities", d["physical"])

    def test_assumptions_merged_and_ids_unique(self):
        d = build()
        self.assertEqual([a["id"] for a in d["assumptions"]], ["A1", "T1"])


class MermaidAndMarkdown(unittest.TestCase):
    def test_symbols_for_every_cardinality(self):
        d = build()
        text = mermaid_er(d)
        self.assertEqual(check_mermaid_syntax(text), [])
        lines = {l.strip() for l in text.splitlines()}
        self.assertTrue(any(l.startswith('owner ||..o{ pet : "owns') for l in lines), text)           # 1:N, mandatory one side
        self.assertTrue(any(l.startswith('pet ||..o| microchip') for l in lines), text)               # 1:1 optional
        self.assertTrue(any(l.startswith('vet |o..o{ vet : "supervises (supervisor / junior)"') for l in lines), text)
        self.assertTrue(any(l.startswith('vet |o..o{ appointment') for l in lines), text)             # optional parent
        self.assertTrue(any(l.startswith('pet }o..o{ vaccine') for l in lines), text)                 # M:N drawn directly

    def test_all_four_cardinality_pairs_render(self):
        from dba_agent.design_render import _LEFT, _RIGHT
        self.assertEqual(set(_LEFT), set(_RIGHT))
        self.assertEqual(len(_LEFT), 4)

    def test_one_side_drawn_first_even_if_listed_second(self):
        d = build()
        r = next(r for r in d["conceptual"]["relationships"] if r["id"] == "owner_pet")
        r["participants"].reverse()
        self.assertIn('owner ||..o{ pet', mermaid_er(d))

    def test_identifying_relationship_uses_solid_line(self):
        d = build()
        d["conceptual"]["relationships"][0]["identifying"] = True
        self.assertIn("owner ||--o{ pet", mermaid_er(d))

    def test_many_to_many_has_no_junction_in_conceptual_diagram(self):
        text = mermaid_er(build())
        self.assertNotIn("pet_vaccinations", text)
        self.assertNotIn("batch_no", text.split("pet }o..o{")[0].split("vaccine {")[1] if "vaccine {" in text else "")

    def test_keys_and_optional_marks(self):
        text = mermaid_er(build())
        self.assertIn("integer owner_id PK", text)
        self.assertIn("string email UK", text)
        self.assertIn('string phone "optional"', text)

    def test_syntax_checker_catches_bad_output(self):
        self.assertTrue(check_mermaid_syntax("erDiagram\n    A ||--o{ B : \"x\""))      # undeclared entity
        self.assertTrue(check_mermaid_syntax("graph TD"))

    def test_quotes_in_labels_sanitised(self):
        d = build()
        d["conceptual"]["relationships"][0]["name"] = 'says "hi"'
        self.assertEqual(check_mermaid_syntax(mermaid_er(d)), [])

    MERMAID_CORE = os.environ.get("MERMAID_CORE", "/opt/npm-tools/node_modules/mermaid/dist/mermaid.core.mjs")

    def _mermaid_parse(self, text):
        """Run the REAL Mermaid parser (no browser needed) via node; None if unavailable."""
        if not (shutil.which("node") and os.path.isfile(self.MERMAID_CORE)):
            return None
        script = ("import fs from 'fs';const m=(await import(process.argv[2])).default;"
                  "try{await m.parse(fs.readFileSync(process.argv[3],'utf8'));console.log('OK');}"
                  "catch(e){console.log('ERR '+String(e.message||e).slice(0,300));}")
        with tempfile.TemporaryDirectory() as tmp:
            src, js = os.path.join(tmp, "d.mmd"), os.path.join(tmp, "p.mjs")
            with open(src, "w") as f:
                f.write(text)
            with open(js, "w") as f:
                f.write(script)
            r = subprocess.run(["node", js, self.MERMAID_CORE, src], capture_output=True, text=True, timeout=120)
        return r.stdout.strip()

    def test_real_mermaid_parser_accepts_diagram(self):
        out = self._mermaid_parse(mermaid_er(build()))
        if out is None:
            self.skipTest("mermaid package/node not available (set MERMAID_CORE)")
        self.assertEqual(out, "OK")
        md = render_conceptual_model(build())
        self.assertEqual(self._mermaid_parse(md.split("```mermaid\n")[1].split("```")[0]), "OK")
        self.assertTrue(self._mermaid_parse("erDiagram\n    A ??--x B").startswith("ERR"))   # parser is really checking

    def test_markdown_content(self):
        md = render_conceptual_model(build())
        for needle in ("# Conceptual Model - Clinic", "## Entities", "### Owner (`owner`, strong entity)",
                       "**Key attributes:** owner_id (primary identifier); email (alternative identifier)",
                       "## Relationships and Cardinalities", "- **Type:** many-to-many",
                       "Each Pet is associated with exactly one Owner.",
                       "Each Owner is associated with zero or more Pet.",
                       "Relationship attributes (stored with each link)", "`administered_on`",
                       "```mermaid", "erDiagram", "## Business Rules", "**no_booking_for_deceased** (assumed)",
                       "## Assumptions", "**A1:** A pet has exactly one owner.", "_Rationale: Stated vaguely._",
                       "## Remaining Ambiguities", "Are pets ever co-owned?", "Opening hours unknown",
                       "Allowed values: dog, cat, other"):
            self.assertIn(needle, md)
        self.assertNotIn("pet_vaccinations", md)            # physical table name must not leak into the conceptual doc
        diagram = md.split("```mermaid\n")[1].split("```")[0]
        self.assertEqual(check_mermaid_syntax(diagram.strip()), [])

    def test_markdown_is_deterministic_and_consistent_with_design(self):
        d = build()
        self.assertEqual(render_conceptual_model(d), render_conceptual_model(copy.deepcopy(d)))
        md = render_conceptual_model(d)
        for e in d["conceptual"]["entities"]:
            self.assertIn(f"`{e['id']}`", md)
        for r in d["conceptual"]["relationships"]:
            self.assertIn(f"(`{r['id']}`)", md)

    def test_no_assumptions_message(self):
        d = build()
        d["assumptions"], d["open_questions"], d["requirement_gaps"] = [], [], []
        md = render_conceptual_model(d)
        self.assertIn("_No assumptions were necessary._", md)
        self.assertIn("## Remaining Ambiguities\n\n_None._", md)


if __name__ == "__main__":
    unittest.main()
