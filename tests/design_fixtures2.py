"""Second neutral demo domain (university enrolment, MySQL) for Stage 4 tests.

Differences from design_fixtures (clinic): composite primary key in a junction table, a relationship
attribute, a DECIMAL(8,2) type (Mermaid-unsafe comma), a deliberate denormalization that breaks 2NF,
no self-reference, no unique FK. `logical2(justified=False)` removes the justification so the
analysis must raise a warning."""
import copy


def conceptual2():
    def attr(name, typ="string", required=True, unique=False, **kw):
        return dict({"name": name, "type": typ, "required": required, "unique": unique, "description": name}, **kw)

    def ent(eid, attrs, pk, extra=()):
        return {"id": eid, "name": eid.title(), "description": f"The {eid}.", "kind": "strong", "attributes": attrs,
                "identifiers": [{"attributes": [pk], "primary": True}] + list(extra)}

    def part(e, lo, hi):
        return {"entity": e, "role": "", "cardinality": {"min": lo, "max": hi}}
    return {
        "entities": [
            ent("department", [attr("dept_id", "integer", unique=True), attr("name", unique=True)], "dept_id"),
            ent("course", [attr("course_id", "integer", unique=True), attr("title"), attr("credits", "integer"),
                           attr("fee", "decimal", required=False)], "course_id"),
            ent("student", [attr("student_id", "integer", unique=True), attr("full_name"), attr("email", unique=True)],
                "student_id", [{"attributes": ["email"], "primary": False}]),
        ],
        "relationships": [
            {"id": "dept_course", "name": "offers", "description": "", "identifying": False,
             "participants": [part("department", 1, 1), part("course", 0, "N")], "attributes": []},
            {"id": "student_course", "name": "enrols in", "description": "", "identifying": False,
             "participants": [part("student", 0, "N"), part("course", 0, "N")],
             "attributes": [attr("enrolled_on", "date"), attr("grade", "integer", required=False)]},
        ],
        "business_rules": [
            {"id": "credit_range", "description": "A course is worth 1 to 10 credits.", "source": "requirement",
             "applies_to": ["course"]},
            {"id": "grade_range", "description": "A grade is between 0 and 100.", "source": "requirement",
             "applies_to": ["student_course"]},
        ],
        "assumptions": [{"id": "A1", "text": "Each course belongs to one department.", "rationale": "Brief says 'its department'.",
                         "related_to": ["course"]}],
        "open_questions": [{"id": "Q1", "text": "Can a student retake a course?", "impact": "Would change the enrolment key."}],
    }


def logical2(justified=True):
    def c(name, typ, nullable=False, src=None, identity=False):
        return {"name": name, "data_type": typ, "nullable": nullable, "identity": identity,
                "source_attribute": src, "default": None, "description": ""}

    def fk(name, cols, ref, rcols, rel, od="RESTRICT", ou="CASCADE"):
        return {"name": name, "columns": cols, "ref_table": ref, "ref_columns": rcols, "on_delete": od,
                "on_update": ou, "relationship": rel}

    def t(name, src, cols, pk, **kw):
        d = {"name": name, "description": "", "source": src, "columns": cols, "primary_key": pk,
             "primary_key_name": f"pk_{name}", "foreign_keys": [], "unique_constraints": [], "check_constraints": [],
             "indexes": []}
        d.update(kw)
        return d
    fd = lambda tb, d, dep: {"table": tb, "determinant": d, "dependent": dep, "note": ""}
    tables = [
        t("departments", {"type": "entity", "id": "department"},
          [c("dept_id", "INT", src="dept_id", identity=True), c("name", "VARCHAR(100)", src="name")], ["dept_id"],
          unique_constraints=[{"name": "uq_departments_name", "columns": ["name"]}]),
        t("courses", {"type": "entity", "id": "course"},
          [c("course_id", "INT", src="course_id", identity=True), c("title", "VARCHAR(150)", src="title"),
           c("credits", "INT", src="credits"), c("fee", "DECIMAL(8,2)", True, src="fee"), c("dept_id", "INT")], ["course_id"],
          foreign_keys=[fk("fk_courses_departments", ["dept_id"], "departments", ["dept_id"], "dept_course")],
          check_constraints=[{"name": "ck_courses_credits", "expression": "credits BETWEEN 1 AND 10", "description": ""}],
          indexes=[{"name": "ix_courses_dept", "columns": ["dept_id"], "unique": False, "purpose": "courses of a department"}]),
        t("students", {"type": "entity", "id": "student"},
          [c("student_id", "INT", src="student_id", identity=True), c("full_name", "VARCHAR(120)", src="full_name"),
           c("email", "VARCHAR(200)", src="email")], ["student_id"],
          unique_constraints=[{"name": "uq_students_email", "columns": ["email"]}]),
        t("enrollments", {"type": "relationship", "id": "student_course"},
          [c("student_id", "INT"), c("course_id", "INT"), c("enrolled_on", "DATE", src="enrolled_on"),
           c("grade", "INT", True, src="grade"), c("course_title", "VARCHAR(150)")], ["student_id", "course_id"],
          foreign_keys=[fk("fk_enr_students", ["student_id"], "students", ["student_id"], "student_course", "CASCADE"),
                        fk("fk_enr_courses", ["course_id"], "courses", ["course_id"], "student_course")],
          check_constraints=[{"name": "ck_enrollments_grade", "expression": "grade BETWEEN 0 AND 100", "description": ""}]),
    ]
    denorm = [{"table": "enrollments", "description": "course_title is copied into every enrolment.",
               "justification": "Transcripts must keep the title the course had when the student enrolled."}] if justified else []
    return {
        "tables": tables,
        "functional_dependencies": [
            fd("departments", ["dept_id"], ["name"]), fd("courses", ["course_id"], ["title", "credits", "fee", "dept_id"]),
            fd("students", ["student_id"], ["full_name", "email"]),
            fd("enrollments", ["student_id", "course_id"], ["enrolled_on", "grade", "course_title"]),
            fd("enrollments", ["course_id"], ["course_title"]),
        ],
        "denormalizations": denorm,
        "business_rule_enforcement": [
            {"rule_id": "credit_range", "mechanism": "schema", "implemented_by": ["ck_courses_credits"]},
            {"rule_id": "grade_range", "mechanism": "schema", "implemented_by": ["ck_enrollments_grade"]},
        ],
        "assumptions": [],
    }


def fresh2(justified=True):
    return copy.deepcopy(conceptual2()), copy.deepcopy(logical2(justified))
