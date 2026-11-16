"""Neutral demo domain (a small clinic) used only by the tests; the code never knows it."""
import copy


def conceptual():
    def attr(name, typ="string", required=True, unique=False, **kw):
        return dict({"name": name, "type": typ, "required": required, "unique": unique, "description": name}, **kw)
    def ent(eid, attrs, pk, extra=()):
        return {"id": eid, "name": eid.replace("_", " ").title(), "description": f"The {eid}.", "kind": "strong",
                "attributes": attrs, "identifiers": [{"attributes": [pk], "primary": True}] + list(extra)}
    def part(e, lo, hi, role=""):
        return {"entity": e, "role": role, "cardinality": {"min": lo, "max": hi}}
    return {
        "entities": [
            ent("owner", [attr("owner_id", "integer", unique=True), attr("full_name"), attr("email", unique=True),
                          attr("phone", required=False)], "owner_id", [{"attributes": ["email"], "primary": False}]),
            ent("pet", [attr("pet_id", "integer", unique=True), attr("name"),
                        attr("species", "enum", allowed_values=["dog", "cat", "other"]),
                        attr("birth_date", "date", required=False)], "pet_id"),
            ent("microchip", [attr("chip_no", unique=True), attr("implanted_on", "date")], "chip_no"),
            ent("vet", [attr("vet_id", "integer", unique=True), attr("full_name"), attr("license_no", unique=True)],
                "vet_id", [{"attributes": ["license_no"], "primary": False}]),
            ent("appointment", [attr("appointment_id", "integer", unique=True), attr("scheduled_at", "datetime"),
                                attr("status", "enum", allowed_values=["booked", "done", "cancelled"]),
                                attr("notes", "text", required=False)], "appointment_id"),
            ent("vaccine", [attr("vaccine_id", "integer", unique=True), attr("name", unique=True)], "vaccine_id"),
        ],
        "relationships": [
            {"id": "owner_pet", "name": "owns", "description": "", "identifying": False,
             "participants": [part("owner", 1, 1), part("pet", 0, "N")], "attributes": []},
            {"id": "pet_microchip", "name": "is tagged with", "description": "", "identifying": False,
             "participants": [part("pet", 1, 1), part("microchip", 0, 1)], "attributes": []},
            {"id": "vet_supervision", "name": "supervises", "description": "", "identifying": False,
             "participants": [part("vet", 0, 1, "supervisor"), part("vet", 0, "N", "junior")], "attributes": []},
            {"id": "pet_appointment", "name": "attends", "description": "", "identifying": False,
             "participants": [part("pet", 1, 1), part("appointment", 0, "N")], "attributes": []},
            {"id": "vet_appointment", "name": "handles", "description": "", "identifying": False,
             "participants": [part("vet", 0, 1), part("appointment", 0, "N")], "attributes": []},
            {"id": "pet_vaccination", "name": "is vaccinated with", "description": "", "identifying": False,
             "participants": [part("pet", 0, "N"), part("vaccine", 0, "N")],
             "attributes": [attr("administered_on", "date"), attr("batch_no", required=False)]},
        ],
        "business_rules": [
            {"id": "status_values", "description": "Appointment status is booked, done or cancelled.",
             "source": "requirement", "applies_to": ["appointment"]},
            {"id": "no_self_supervision", "description": "A vet cannot supervise themself.",
             "source": "requirement", "applies_to": ["vet_supervision"]},
            {"id": "no_booking_for_deceased", "description": "No appointments for deceased pets.",
             "source": "assumption", "applies_to": ["pet_appointment"]},
        ],
        "assumptions": [{"id": "A1", "text": "A pet has exactly one owner.", "rationale": "Stated vaguely.", "related_to": ["pet"]}],
        "open_questions": [{"id": "Q1", "text": "Are pets ever co-owned?", "impact": "Would make owner-pet many-to-many."}],
    }


def _c(name, typ, nullable=False, src=None, identity=False, default=None):
    return {"name": name, "data_type": typ, "nullable": nullable, "identity": identity,
            "source_attribute": src or name, "default": default, "description": ""}


def _fk(name, cols, ref, rcols, rel, od="RESTRICT", ou="CASCADE"):
    return {"name": name, "columns": cols, "ref_table": ref, "ref_columns": rcols, "on_delete": od,
            "on_update": ou, "relationship": rel}


def _t(name, src, cols, pk, **kw):
    t = {"name": name, "description": "", "source": src, "columns": cols, "primary_key": pk,
         "primary_key_name": f"pk_{name}", "foreign_keys": [], "unique_constraints": [],
         "check_constraints": [], "indexes": []}
    t.update(kw)
    return t


def logical():
    e = lambda i: {"type": "entity", "id": i}
    tables = [
        _t("owners", e("owner"), [_c("owner_id", "INTEGER", identity=True), _c("full_name", "VARCHAR(120)"),
                                  _c("email", "VARCHAR(200)"), _c("phone", "VARCHAR(30)", True)], ["owner_id"],
           unique_constraints=[{"name": "uq_owners_email", "columns": ["email"]}]),
        _t("pets", e("pet"), [_c("pet_id", "INTEGER", identity=True), _c("name", "VARCHAR(80)"),
                              _c("species", "VARCHAR(10)"), _c("birth_date", "DATE", True),
                              _c("owner_id", "INTEGER", src="")], ["pet_id"],
           foreign_keys=[_fk("fk_pets_owners", ["owner_id"], "owners", ["owner_id"], "owner_pet")],
           check_constraints=[{"name": "ck_pets_species", "expression": "species IN ('dog','cat','other')", "description": ""}],
           indexes=[{"name": "ix_pets_owner", "columns": ["owner_id"], "unique": False, "purpose": "list pets of an owner"}]),
        _t("microchips", e("microchip"), [_c("chip_no", "VARCHAR(30)"), _c("implanted_on", "DATE"),
                                          _c("pet_id", "INTEGER", src="")], ["chip_no"],
           foreign_keys=[_fk("fk_microchips_pets", ["pet_id"], "pets", ["pet_id"], "pet_microchip", "CASCADE")],
           unique_constraints=[{"name": "uq_microchips_pet", "columns": ["pet_id"]}]),
        _t("vets", e("vet"), [_c("vet_id", "INTEGER", identity=True), _c("full_name", "VARCHAR(120)"),
                              _c("license_no", "VARCHAR(40)"), _c("supervisor_id", "INTEGER", True, src="")], ["vet_id"],
           unique_constraints=[{"name": "uq_vets_license", "columns": ["license_no"]}],
           foreign_keys=[_fk("fk_vets_supervisor", ["supervisor_id"], "vets", ["vet_id"], "vet_supervision", "SET NULL")],
           check_constraints=[{"name": "ck_vets_not_self", "expression": "supervisor_id <> vet_id", "description": ""}]),
        _t("appointments", e("appointment"), [_c("appointment_id", "INTEGER", identity=True),
                                              _c("scheduled_at", "TIMESTAMP"), _c("status", "VARCHAR(10)"),
                                              _c("notes", "TEXT", True), _c("pet_id", "INTEGER", src=""),
                                              _c("vet_id", "INTEGER", True, src="")], ["appointment_id"],
           foreign_keys=[_fk("fk_appointments_pets", ["pet_id"], "pets", ["pet_id"], "pet_appointment"),
                         _fk("fk_appointments_vets", ["vet_id"], "vets", ["vet_id"], "vet_appointment", "SET NULL")],
           check_constraints=[{"name": "ck_appointments_status", "expression": "status IN ('booked','done','cancelled')",
                               "description": ""}]),
        _t("vaccines", e("vaccine"), [_c("vaccine_id", "INTEGER", identity=True), _c("name", "VARCHAR(100)")],
           ["vaccine_id"], unique_constraints=[{"name": "uq_vaccines_name", "columns": ["name"]}]),
        _t("pet_vaccinations", {"type": "relationship", "id": "pet_vaccination"},
           [_c("pet_id", "INTEGER", src=""), _c("vaccine_id", "INTEGER", src=""), _c("administered_on", "DATE"),
            _c("batch_no", "VARCHAR(40)", True)], ["pet_id", "vaccine_id", "administered_on"],
           foreign_keys=[_fk("fk_pv_pets", ["pet_id"], "pets", ["pet_id"], "pet_vaccination", "CASCADE"),
                         _fk("fk_pv_vaccines", ["vaccine_id"], "vaccines", ["vaccine_id"], "pet_vaccination")],
           unique_constraints=[{"name": "uq_pv_pair_day", "columns": ["pet_id", "vaccine_id", "administered_on"]}]),
    ]
    for t in tables:
        for c in t["columns"]:
            if c["source_attribute"] == "":
                c["source_attribute"] = None
    fd = lambda t, d, dep: {"table": t, "determinant": d, "dependent": dep, "note": ""}
    return {
        "tables": tables,
        "functional_dependencies": [
            fd("owners", ["owner_id"], ["full_name", "email", "phone"]), fd("owners", ["email"], ["owner_id"]),
            fd("pets", ["pet_id"], ["name", "species", "birth_date", "owner_id"]),
            fd("microchips", ["chip_no"], ["implanted_on", "pet_id"]),
            fd("vets", ["vet_id"], ["full_name", "license_no", "supervisor_id"]),
            fd("appointments", ["appointment_id"], ["scheduled_at", "status", "notes", "pet_id", "vet_id"]),
            fd("vaccines", ["vaccine_id"], ["name"]),
            fd("pet_vaccinations", ["pet_id", "vaccine_id", "administered_on"], ["batch_no"]),
        ],
        "denormalizations": [],
        "business_rule_enforcement": [
            {"rule_id": "status_values", "mechanism": "schema", "implemented_by": ["ck_appointments_status"]},
            {"rule_id": "no_self_supervision", "mechanism": "schema", "implemented_by": ["ck_vets_not_self"]},
            {"rule_id": "no_booking_for_deceased", "mechanism": "application", "implemented_by": [],
             "note": "The schema stores no deceased flag; the application must check."},
        ],
        "assumptions": [{"id": "T1", "text": "Surrogate integer keys are generated by the DBMS.", "rationale": "Simplicity."}],
    }


def fresh():
    return copy.deepcopy(conceptual()), copy.deepcopy(logical())
