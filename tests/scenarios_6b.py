"""Batch 6B - two deterministic mocked scenarios (neutral demo domains; the production code never sees them).

Every dict below is what a real LLM *would have to return* at the respective stage. They are scripted so that the
WORKFLOW (validation, state, traceability, normalization analysis, approval) can be tested offline. They say nothing
about how well a real model would understand an arbitrary scenario."""
import copy

CATEGORIES = ("entities_attributes", "relationships_cardinalities", "data_volume_growth", "read_write_reporting",
              "transactions_concurrency", "security_roles_privacy", "business_rules_constraints")
_RATING = {d: "ok" for d in ("postgresql", "mysql_mariadb", "sqlite", "sql_server")}
CRITERIA = ("data_volume", "concurrency", "transactions_integrity", "security", "reporting_analytics",
            "deployment", "maintainability")
DBMS_ID_NAMES = {"postgresql": "PostgreSQL", "mysql_mariadb": "MySQL/MariaDB", "sqlite": "SQLite", "sql_server": "SQL Server"}


def choice(selected):
    return {"selected": selected, "summary": f"{selected} fits.", "key_requirements": ["small multi-user system"],
            "justification": "Mocked justification: matches the stated volumes and integrity needs.",
            "comparison": [{"criterion": c, "relevance": "r", "ratings": dict(_RATING)} for c in CRITERIA],
            "rejected": [{"dbms": d, "reasons": ["mocked: not needed here"]} for d in _RATING if d != selected]}


def _attr(name, typ="string", required=True, unique=False, **kw):
    return dict({"name": name, "type": typ, "required": required, "unique": unique, "description": name}, **kw)


def _ent(eid, attrs, pk, extra=()):
    return {"id": eid, "name": eid.replace("_", " ").title(), "description": f"The {eid}.", "kind": "strong",
            "attributes": attrs, "identifiers": [{"attributes": [pk], "primary": True}] + list(extra)}


def _part(e, lo, hi, role=""):
    return {"entity": e, "role": role, "cardinality": {"min": lo, "max": hi}}


def _rel(rid, name, parts, attrs=()):
    return {"id": rid, "name": name, "description": "", "identifying": False, "participants": parts, "attributes": list(attrs)}


def _c(name, typ, nullable=False, src=None, identity=False, default=None):
    return {"name": name, "data_type": typ, "nullable": nullable, "identity": identity,
            "source_attribute": src or name, "default": default, "description": ""}


def _fk(name, cols, ref, rcols, rel, od="RESTRICT", ou="CASCADE"):
    return {"name": name, "columns": cols, "ref_table": ref, "ref_columns": rcols, "on_delete": od, "on_update": ou, "relationship": rel}


def _t(name, src, cols, pk, **kw):
    t = {"name": name, "description": "", "source": src, "columns": cols, "primary_key": pk,
         "primary_key_name": f"pk_{name}", "foreign_keys": [], "unique_constraints": [], "check_constraints": [], "indexes": []}
    t.update(kw)
    for c in t["columns"]:
        if c["source_attribute"] == "":
            c["source_attribute"] = None
    return t


def _fd(t, d, dep):
    return {"table": t, "determinant": d, "dependent": dep, "note": ""}


def _stage12(project, summary, entities, requirements, ambiguities, answers_r1, follow, answers_r2, reqs):
    return {"intake": {"project_name": project, "summary": summary,
                       "actors": [{"name": "Customer", "description": "", "responsibilities": []}],
                       "entities": [{"name": e, "description": ""} for e in entities],
                       "processes": [{"name": "Main process", "description": "", "actors": ["Customer"]}],
                       "requirements": requirements, "ambiguities": ambiguities},
            "questions": {"questions": [{"category": c, "question": f"Question about {c}?", "why": "w"} for c in CATEGORIES]},
            "answers_r1": answers_r1, "follow": follow, "answers_r2": answers_r2, "requirements": reqs}


# =============================================================== A. appointment booking (SQLite)
BOOKING_DESCRIPTION = (
    "A small salon. Customers book services with employees. Each employee provides only some of the services. "
    "An appointment has a status (booked, confirmed, completed, cancelled, no-show). The system must prevent two active "
    "bookings for the same employee at the same time. Cancelled and past appointments and their status changes must be kept as history.")

BOOKING_REQUIREMENTS = [
    {"type": "business rule", "description": "Customers book services with employees."},
    {"type": "business rule", "description": "Each employee provides only selected services."},
    {"type": "business rule", "description": "Appointments have a status."},
    {"type": "business rule", "description": "The system must prevent conflicting active bookings."},
    {"type": "business rule", "description": "Appointment history must be preserved."},
]
BOOKING_AMBIGUITIES = [
    {"description": "Can one appointment contain several services?", "why_it_matters": "Changes appointment-service cardinality."},
    {"description": "How long does an appointment last and can it overlap another by a few minutes?", "why_it_matters": "Overlap rule."},
]


def booking_stage12():
    r1 = ["customers: name, e-mail, phone; employees: name, e-mail; services: name, duration, price; appointments with date and time",
          "an appointment is for one customer, one employee and one service; employees offer many services and services are offered by many employees",
          "about 20 employees and 200 appointments a week",
          "daily agenda per employee; customer history",
          "two receptionists book at the same time; double booking must be impossible",
          "(skipped)",                                       # security question skipped -> must not be silently dropped
          "statuses booked, confirmed, completed, cancelled, no_show; no two active bookings for one employee at the same time; history is never deleted"]
    follow = {"enough_information": False, "reasoning": "Appointment length and multi-service bookings are unclear.",
              "follow_up_questions": [
                  {"category": "relationships_cardinalities", "question": "Can one appointment include several services?"},
                  {"category": "business_rules_constraints", "question": "May active appointments of one employee overlap?"}]}
    r2 = ["I don't know yet", "no overlaps at all"]
    reqs = {"summary": "Salon booking system: customers book services with employees; employee/service eligibility, statuses, "
                       "conflict prevention and complete history are required.",
            "categories": [{"id": c, "findings": [f"Finding for {c}"] } for c in CATEGORIES],
            "open_issues": ["Whether one appointment can contain several services is not decided."]}
    reqs["categories"][1]["findings"] = ["An appointment is for one customer, one employee and one service.",
                                         "Employees offer many services; a service is offered by many employees."]
    reqs["categories"][6]["findings"] = ["Appointment status is booked, confirmed, completed, cancelled or no_show.",
                                         "No two active bookings for one employee at the same time.",
                                         "Appointment history, including status changes, is never deleted."]
    out = _stage12("Salon Booking", "A salon booking system.", ["Customer", "Employee", "Service", "Appointment"],
                   BOOKING_REQUIREMENTS, BOOKING_AMBIGUITIES, r1, follow, r2, reqs)
    enough = {"enough_information": True, "reasoning": "Covered.", "follow_up_questions": []}
    out["reviews"] = [follow, enough, enough]          # the skipped security question forces a third round (coverage guard)
    return out


def booking_conceptual():
    return {
        "entities": [
            _ent("customer", [_attr("customer_id", "integer", unique=True), _attr("full_name"), _attr("email", unique=True),
                              _attr("phone", required=False)], "customer_id", [{"attributes": ["email"], "primary": False}]),
            _ent("employee", [_attr("employee_id", "integer", unique=True), _attr("full_name"), _attr("email", unique=True)],
                 "employee_id", [{"attributes": ["email"], "primary": False}]),
            _ent("service", [_attr("service_id", "integer", unique=True), _attr("name", unique=True),
                             _attr("duration_minutes", "integer"), _attr("price", "decimal")], "service_id"),
            _ent("appointment", [_attr("appointment_id", "integer", unique=True), _attr("starts_at", "datetime"),
                                 _attr("ends_at", "datetime"),
                                 _attr("status", "enum", allowed_values=["booked", "confirmed", "completed", "cancelled", "no_show"]),
                                 _attr("notes", "text", required=False)], "appointment_id"),
            _ent("status_change", [_attr("change_id", "integer", unique=True), _attr("old_status", required=False),
                                   _attr("new_status"), _attr("changed_at", "datetime"), _attr("reason", "text", required=False)],
                 "change_id"),
        ],
        "relationships": [
            _rel("customer_appointment", "books", [_part("customer", 1, 1), _part("appointment", 0, "N")]),
            _rel("employee_appointment", "performs", [_part("employee", 1, 1), _part("appointment", 0, "N")]),
            _rel("service_appointment", "is booked as", [_part("service", 1, 1), _part("appointment", 0, "N")]),
            _rel("employee_service", "provides", [_part("employee", 0, "N"), _part("service", 0, "N")]),
            _rel("appointment_change", "has history", [_part("appointment", 1, 1), _part("status_change", 0, "N")]),
        ],
        "business_rules": [
            {"id": "status_values", "description": "Appointment status is booked, confirmed, completed, cancelled or no_show.",
             "source": "requirement", "applies_to": ["appointment"]},
            {"id": "no_double_booking_same_start", "description": "An employee has at most one active appointment starting at a given time.",
             "source": "requirement", "applies_to": ["appointment"]},
            {"id": "no_overlapping_active_bookings", "description": "Active appointments of one employee must not overlap in time.",
             "source": "requirement", "applies_to": ["appointment"]},
            {"id": "history_preserved", "description": "Appointments and their status changes are never deleted or rewritten.",
             "source": "requirement", "applies_to": ["appointment", "status_change"]},
            {"id": "employee_provides_service", "description": "An appointment may only use a service the employee provides.",
             "source": "requirement", "applies_to": ["employee_service"]},
            {"id": "end_after_start", "description": "An appointment ends after it starts.", "source": "assumption",
             "applies_to": ["appointment"]},
        ],
        "assumptions": [{"id": "A1", "text": "One appointment is for exactly one service.",
                         "rationale": "The client could not say whether several services are possible (see Q1).", "related_to": ["appointment"]}],
        "open_questions": [{"id": "Q1", "text": "Can one appointment contain several services?", "impact": "Would need an appointment-service junction table."}],
    }


def booking_logical():
    e = lambda i: {"type": "entity", "id": i}
    tables = [
        _t("customers", e("customer"), [_c("customer_id", "INTEGER", identity=True), _c("full_name", "TEXT"), _c("email", "TEXT"),
                                        _c("phone", "TEXT", True)], ["customer_id"],
           unique_constraints=[{"name": "uq_customers_email", "columns": ["email"]}]),
        _t("employees", e("employee"), [_c("employee_id", "INTEGER", identity=True), _c("full_name", "TEXT"), _c("email", "TEXT")],
           ["employee_id"], unique_constraints=[{"name": "uq_employees_email", "columns": ["email"]}]),
        _t("services", e("service"), [_c("service_id", "INTEGER", identity=True), _c("name", "TEXT"),
                                      _c("duration_minutes", "INTEGER"), _c("price", "NUMERIC")], ["service_id"],
           unique_constraints=[{"name": "uq_services_name", "columns": ["name"]}],
           check_constraints=[{"name": "ck_services_duration", "expression": "duration_minutes > 0", "description": ""}]),
        _t("employee_services", {"type": "relationship", "id": "employee_service"},
           [_c("employee_id", "INTEGER", src=""), _c("service_id", "INTEGER", src="")], ["employee_id", "service_id"],
           foreign_keys=[_fk("fk_es_employees", ["employee_id"], "employees", ["employee_id"], "employee_service", "CASCADE"),
                         _fk("fk_es_services", ["service_id"], "services", ["service_id"], "employee_service", "CASCADE")]),
        _t("appointments", e("appointment"),
           [_c("appointment_id", "INTEGER", identity=True), _c("starts_at", "TEXT"), _c("ends_at", "TEXT"), _c("status", "TEXT"),
            _c("notes", "TEXT", True), _c("customer_id", "INTEGER", src=""), _c("employee_id", "INTEGER", src=""),
            _c("service_id", "INTEGER", src="")], ["appointment_id"],
           foreign_keys=[_fk("fk_appt_customers", ["customer_id"], "customers", ["customer_id"], "customer_appointment"),
                         _fk("fk_appt_employees", ["employee_id"], "employees", ["employee_id"], "employee_appointment"),
                         _fk("fk_appt_services", ["service_id"], "services", ["service_id"], "service_appointment")],
           check_constraints=[{"name": "ck_appt_status", "expression": "status IN ('booked','confirmed','completed','cancelled','no_show')", "description": ""},
                              {"name": "ck_appt_end_after_start", "expression": "ends_at > starts_at", "description": ""}],
           indexes=[{"name": "ux_appt_employee_start_active", "columns": ["employee_id", "starts_at"], "unique": True,
                     "purpose": "no two active bookings for one employee at the same start time",
                     "where": [{"column": "status", "operator": "IN", "value": ["booked", "confirmed"]}], "conditional": True},
                    {"name": "ix_appt_customer", "columns": ["customer_id"], "unique": False, "purpose": "customer history"}]),
        _t("status_changes", e("status_change"),
           [_c("change_id", "INTEGER", identity=True), _c("old_status", "TEXT", True), _c("new_status", "TEXT"),
            _c("changed_at", "TEXT"), _c("reason", "TEXT", True), _c("appointment_id", "INTEGER", src="")], ["change_id"],
           foreign_keys=[_fk("fk_sc_appointments", ["appointment_id"], "appointments", ["appointment_id"], "appointment_change")],
           triggers=[{"name": "tg_status_changes_append_only", "kind": "append_only", "description": "history is never changed",
                      "operations": ["UPDATE", "DELETE"]}]),
    ]
    fds = [_fd("customers", ["customer_id"], ["full_name", "email", "phone"]), _fd("customers", ["email"], ["customer_id"]),
           _fd("employees", ["employee_id"], ["full_name", "email"]), _fd("employees", ["email"], ["employee_id"]),
           _fd("services", ["service_id"], ["name", "duration_minutes", "price"]), _fd("services", ["name"], ["service_id"]),
           _fd("appointments", ["appointment_id"], ["starts_at", "ends_at", "status", "notes", "customer_id", "employee_id", "service_id"]),
           _fd("status_changes", ["change_id"], ["old_status", "new_status", "changed_at", "reason", "appointment_id"])]
    return {"tables": tables, "functional_dependencies": fds, "denormalizations": [],
            "business_rule_enforcement": [
                {"rule_id": "status_values", "mechanism": "schema", "implemented_by": ["ck_appt_status"]},
                {"rule_id": "no_double_booking_same_start", "mechanism": "schema", "implemented_by": ["ux_appt_employee_start_active"]},
                {"rule_id": "no_overlapping_active_bookings", "mechanism": "application", "implemented_by": [],
                 "note": "A partial unique index only stops identical start times; overlap of time ranges needs an application-level (or exclusion-constraint) check in the booking transaction."},
                {"rule_id": "history_preserved", "mechanism": "trigger", "implemented_by": ["tg_status_changes_append_only"],
                 "note": "Status changes are append-only (trigger tg_status_changes_append_only); appointments are cancelled, never deleted (RESTRICT foreign key)."},
                {"rule_id": "employee_provides_service", "mechanism": "application", "implemented_by": [],
                 "note": "appointments(employee_id, service_id) is not a composite foreign key to employee_services here; the booking code must check the pair."},
                {"rule_id": "end_after_start", "mechanism": "schema", "implemented_by": ["ck_appt_end_after_start"]}],
            "assumptions": [{"id": "T1", "text": "Dates and times are stored as ISO-8601 text (SQLite has no date type).", "rationale": "SQLite storage classes."}]}


# =============================================================== B. online ordering (PostgreSQL)
ORDERING_DESCRIPTION = (
    "An online shop. Customers place orders that contain several products. Product prices change over time, but an order must "
    "keep the price the customer paid. Payments and order statuses are tracked. Historical orders must stay valid even when "
    "products or prices change later.")
ORDERING_REQUIREMENTS = [
    {"type": "business rule", "description": "Customers place orders containing multiple products."},
    {"type": "business rule", "description": "Product prices can change over time."},
    {"type": "business rule", "description": "An order preserves the purchase-time price."},
    {"type": "functional", "description": "Payments and order statuses are tracked."},
    {"type": "business rule", "description": "Historical orders must remain valid."},
]
ORDERING_AMBIGUITIES = [
    {"description": "Can one order be paid in several payments?", "why_it_matters": "Order-payment cardinality."},
    {"description": "Are orders ever deleted, or is a product that was sold ever removed from the catalogue?", "why_it_matters": "History/FK delete behaviour."}]


def ordering_stage12():
    r1 = ["customers: name, e-mail; products: name, SKU; orders with date; order lines with quantity; payments with amount and method",
          "a customer places many orders; an order has many lines; each line is for one product; a product appears in many orders",
          "5000 products, 1000 orders a day",
          "order history per customer, revenue per month",
          "payment and order creation must be consistent",
          "customers see only their own orders",
          "price at purchase time is kept; statuses pending, paid, shipped, cancelled; quantity at least 1"]
    follow = {"enough_information": False, "reasoning": "Payment cardinality is unclear.",
              "follow_up_questions": [{"category": "relationships_cardinalities", "question": "Can an order be paid in several payments?"}]}
    r2 = ["I don't know"]
    reqs = {"summary": "Online ordering: orders with several products, price history, purchase-time price snapshot, payments and statuses.",
            "categories": [{"id": c, "findings": [f"Finding for {c}"]} for c in CATEGORIES],
            "open_issues": ["Whether an order can be paid in several payments is not decided."]}
    reqs["categories"][6]["findings"] = ["The price at purchase time is kept on the order line.", "Order status is pending, paid, shipped or cancelled.",
                                         "Quantity is at least 1.", "Historical orders must stay valid."]
    return _stage12("Online Shop", "An online ordering system.", ["Customer", "Product", "Order", "Payment"],
                    ORDERING_REQUIREMENTS, ORDERING_AMBIGUITIES, r1, follow, r2, reqs)


def ordering_conceptual():
    return {
        "entities": [
            _ent("customer", [_attr("customer_id", "integer", unique=True), _attr("full_name"), _attr("email", unique=True)],
                 "customer_id", [{"attributes": ["email"], "primary": False}]),
            _ent("product", [_attr("product_id", "integer", unique=True), _attr("sku", unique=True), _attr("name"),
                             _attr("current_price", "decimal")], "product_id", [{"attributes": ["sku"], "primary": False}]),
            _ent("product_price", [_attr("price_id", "integer", unique=True), _attr("price", "decimal"),
                                   _attr("valid_from", "datetime")], "price_id"),
            _ent("sales_order", [_attr("order_id", "integer", unique=True), _attr("placed_at", "datetime"),
                                 _attr("status", "enum", allowed_values=["pending", "paid", "shipped", "cancelled"])], "order_id"),
            _ent("payment", [_attr("payment_id", "integer", unique=True), _attr("amount", "decimal"),
                             _attr("method", "enum", allowed_values=["card", "transfer", "cash"]),
                             _attr("paid_at", "datetime", required=False)], "payment_id"),
            _ent("order_status_event", [_attr("event_id", "integer", unique=True), _attr("status"), _attr("changed_at", "datetime")], "event_id"),
        ],
        "relationships": [
            _rel("customer_order", "places", [_part("customer", 1, 1), _part("sales_order", 0, "N")]),
            _rel("order_product", "contains", [_part("sales_order", 1, "N"), _part("product", 0, "N")],
                 [_attr("quantity", "integer"), _attr("unit_price", "decimal")]),
            _rel("product_price_history", "has price history", [_part("product", 1, 1), _part("product_price", 0, "N")]),
            _rel("order_payment", "is paid by", [_part("sales_order", 1, 1), _part("payment", 0, "N")]),
            _rel("order_event", "has status history", [_part("sales_order", 1, 1), _part("order_status_event", 0, "N")]),
        ],
        "business_rules": [
            {"id": "price_snapshot", "description": "Each order line keeps the unit price valid at purchase time; later price changes do not alter it.",
             "source": "requirement", "applies_to": ["order_product"]},
            {"id": "status_values", "description": "Order status is pending, paid, shipped or cancelled.", "source": "requirement", "applies_to": ["sales_order"]},
            {"id": "positive_quantity", "description": "Quantity on an order line is at least 1.", "source": "requirement", "applies_to": ["order_product"]},
            {"id": "history_valid", "description": "Orders, lines and prices are never deleted when a product or price changes.", "source": "requirement",
             "applies_to": ["sales_order", "product_price"]},
            {"id": "positive_amount", "description": "A payment amount is positive.", "source": "assumption", "applies_to": ["payment"]},
        ],
        "assumptions": [{"id": "A1", "text": "An order may have several payments (0..N).", "rationale": "The client did not know (see Q1); the weaker constraint avoids inventing a rule.",
                         "related_to": ["payment"]}],
        "open_questions": [{"id": "Q1", "text": "Can one order be paid in several payments?", "impact": "A single payment would allow a unique FK."}],
    }


def ordering_logical():
    e = lambda i: {"type": "entity", "id": i}
    tables = [
        _t("customers", e("customer"), [_c("customer_id", "INTEGER", identity=True), _c("full_name", "VARCHAR(120)"), _c("email", "VARCHAR(200)")],
           ["customer_id"], unique_constraints=[{"name": "uq_customers_email", "columns": ["email"]}]),
        _t("products", e("product"), [_c("product_id", "INTEGER", identity=True), _c("sku", "VARCHAR(40)"), _c("name", "VARCHAR(150)"),
                                      _c("current_price", "NUMERIC(10,2)")], ["product_id"],
           unique_constraints=[{"name": "uq_products_sku", "columns": ["sku"]}],
           check_constraints=[{"name": "ck_products_price", "expression": "current_price >= 0", "description": ""}]),
        _t("product_prices", e("product_price"), [_c("price_id", "INTEGER", identity=True), _c("price", "NUMERIC(10,2)"),
                                                  _c("valid_from", "TIMESTAMP"), _c("product_id", "INTEGER", src="")], ["price_id"],
           foreign_keys=[_fk("fk_pp_products", ["product_id"], "products", ["product_id"], "product_price_history")],
           unique_constraints=[{"name": "uq_pp_product_from", "columns": ["product_id", "valid_from"]}]),
        _t("sales_orders", e("sales_order"), [_c("order_id", "INTEGER", identity=True), _c("placed_at", "TIMESTAMP"),
                                              _c("status", "VARCHAR(10)"), _c("customer_id", "INTEGER", src="")], ["order_id"],
           foreign_keys=[_fk("fk_so_customers", ["customer_id"], "customers", ["customer_id"], "customer_order")],
           check_constraints=[{"name": "ck_so_status", "expression": "status IN ('pending','paid','shipped','cancelled')", "description": ""}],
           indexes=[{"name": "ix_so_customer", "columns": ["customer_id"], "unique": False, "purpose": "order history of a customer"}]),
        _t("order_items", {"type": "relationship", "id": "order_product"},
           [_c("order_id", "INTEGER", src=""), _c("product_id", "INTEGER", src=""), _c("quantity", "INTEGER"), _c("unit_price", "NUMERIC(10,2)")],
           ["order_id", "product_id"],
           foreign_keys=[_fk("fk_oi_orders", ["order_id"], "sales_orders", ["order_id"], "order_product", "CASCADE"),
                         _fk("fk_oi_products", ["product_id"], "products", ["product_id"], "order_product")],
           check_constraints=[{"name": "ck_oi_quantity", "expression": "quantity >= 1", "description": ""}]),
        _t("payments", e("payment"), [_c("payment_id", "INTEGER", identity=True), _c("amount", "NUMERIC(10,2)"), _c("method", "VARCHAR(10)"),
                                      _c("paid_at", "TIMESTAMP", True), _c("order_id", "INTEGER", src="")], ["payment_id"],
           foreign_keys=[_fk("fk_pay_orders", ["order_id"], "sales_orders", ["order_id"], "order_payment")],
           check_constraints=[{"name": "ck_pay_method", "expression": "method IN ('card','transfer','cash')", "description": ""},
                              {"name": "ck_pay_amount", "expression": "amount > 0", "description": ""}]),
        _t("order_status_events", e("order_status_event"), [_c("event_id", "INTEGER", identity=True), _c("status", "VARCHAR(10)"),
                                                            _c("changed_at", "TIMESTAMP"), _c("order_id", "INTEGER", src="")], ["event_id"],
           foreign_keys=[_fk("fk_ose_orders", ["order_id"], "sales_orders", ["order_id"], "order_event")]),
    ]
    fds = [_fd("customers", ["customer_id"], ["full_name", "email"]), _fd("customers", ["email"], ["customer_id"]),
           _fd("products", ["product_id"], ["sku", "name", "current_price"]), _fd("products", ["sku"], ["product_id"]),
           _fd("product_prices", ["price_id"], ["price", "valid_from", "product_id"]),
           _fd("product_prices", ["product_id", "valid_from"], ["price_id"]),
           _fd("sales_orders", ["order_id"], ["placed_at", "status", "customer_id"]),
           _fd("order_items", ["order_id", "product_id"], ["quantity", "unit_price"]),
           _fd("payments", ["payment_id"], ["amount", "method", "paid_at", "order_id"]),
           _fd("order_status_events", ["event_id"], ["status", "changed_at", "order_id"])]
    return {"tables": tables, "functional_dependencies": fds, "denormalizations": [],
            "business_rule_enforcement": [
                {"rule_id": "price_snapshot", "mechanism": "schema", "implemented_by": ["order_items.unit_price"]},
                {"rule_id": "status_values", "mechanism": "schema", "implemented_by": ["ck_so_status"]},
                {"rule_id": "positive_quantity", "mechanism": "schema", "implemented_by": ["ck_oi_quantity"]},
                {"rule_id": "history_valid", "mechanism": "schema", "implemented_by": ["fk_oi_products", "fk_pp_products"]},
                {"rule_id": "positive_amount", "mechanism": "schema", "implemented_by": ["ck_pay_amount"]}],
            "assumptions": [{"id": "T1", "text": "unit_price is copied from the then-current price when the line is created (application step).",
                             "rationale": "Snapshot semantics."}]}


SCENARIOS = {
    "booking": dict(description=BOOKING_DESCRIPTION, stage12=booking_stage12, conceptual=booking_conceptual,
                    logical=booking_logical, dbms="sqlite"),
    "ordering": dict(description=ORDERING_DESCRIPTION, stage12=ordering_stage12, conceptual=ordering_conceptual,
                     logical=ordering_logical, dbms="postgresql"),
}
