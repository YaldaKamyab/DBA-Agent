"""Canonical, JSON-serializable database design (design.json).

Layers (kept apart on purpose, so SQL generation is unambiguous):

  conceptual : entities (attributes, identifiers) and relationships
               (participants with min/max cardinality, relationship attributes).
               M:N relationships are shown as such - no junction tables here.
  physical   : tables (columns, PK, FKs, UNIQUE, CHECK, indexes), functional
               dependencies, deliberate denormalizations. Every table records
               which entity/relationship it implements (`source`), every FK
               which relationship it implements (`relationship`).
  business_rules, assumptions, open_questions, requirement_gaps : cross-cutting.

Cardinality convention (look-across, the same as Mermaid): a participant's
`cardinality` {min, max} says how many instances of THAT participant can be
related to ONE instance of the OTHER participant.
  Customer ||--o{ Order : places    -> Customer {1,1}, Order {0,N}

All validate_* functions accept loose LLM output *and* already-canonical data
(they are idempotent), so design.json can be re-validated after any edit.
Problems are collected and raised together as one InvalidResponseError whose
message is fed back to the LLM on retry.
"""
import copy
import re

from .errors import InvalidResponseError
from .sql_types import fk_type_problem

SCHEMA_VERSION = 1
LOGICAL_TYPES = ("string", "text", "integer", "decimal", "boolean", "date", "time",
                 "datetime", "uuid", "json", "binary", "enum")
FK_ACTIONS = ("NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT")
ENFORCEMENTS = ("schema", "trigger", "application")
TRIGGER_KINDS = ("append_only", "immutable_columns")      # structured, reusable database-level rules (SQLite generator)
TRIGGER_DIALECTS = ("sqlite",)                            # DBMSs for which schema.sql can really generate them
_ROW_OPS = ("UPDATE", "DELETE")
# identifiers that break SQL in at least one supported DBMS when used unquoted
RESERVED = {"order", "group", "user", "table", "index", "select", "from", "where", "key", "primary",
            "references", "check", "default", "constraint", "column", "values", "join", "limit",
            "offset", "role", "desc", "asc", "insert", "update", "delete", "create", "drop", "alter",
            "grant", "union", "having", "case", "when", "end", "foreign", "unique", "null", "all"}
_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_IDENT = 63
MAX_REPORTED = 20


class _Errors:
    def __init__(self):
        self.items = []

    def add(self, msg):
        self.items.append(msg)

    def raise_if_any(self, header):
        if self.items:
            shown = self.items[:MAX_REPORTED]
            more = f"\n- ... and {len(self.items) - MAX_REPORTED} more" if len(self.items) > MAX_REPORTED else ""
            raise InvalidResponseError(header + "\n" + "\n".join(f"- {m}" for m in shown) + more)


# ----------------------------------------------------------------- primitives
def _s(v):
    return str(v if v is not None else "").strip()


def _list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _bool(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no", "0"):
        return False
    return default


def _strs(v):
    return [_s(x) for x in _list(v) if _s(x)]


def _ident(name, what, errs):
    name = _s(name)
    if not _IDENT.match(name):
        errs.add(f"{what} '{name}' is not a valid SQL identifier (letters, digits, underscore; must not start with a digit).")
    elif len(name) > MAX_IDENT:
        errs.add(f"{what} '{name}' is longer than {MAX_IDENT} characters.")
    elif name.lower() in RESERVED:
        errs.add(f"{what} '{name}' is a reserved SQL word; choose another name (e.g. a plural or a prefixed name).")
    return name


def _slug(value, what, errs):
    v = _s(value)
    if not _SLUG.match(v):
        errs.add(f"{what} '{v}' must be a lowercase snake_case id (e.g. 'order_item').")
    return v


def _unique(items, what, errs):
    seen = {}
    for x in items:
        k = x.lower()
        if k in seen:
            errs.add(f"Duplicate {what} '{x}'.")
        seen[k] = x


# ---------------------------------------------------------------- cardinality
_CARD_WORDS = {
    "zero_or_one": (0, 1), "optional": (0, 1), "exactly_one": (1, 1), "one": (1, 1), "mandatory": (1, 1),
    "zero_or_many": (0, "N"), "zero_or_more": (0, "N"), "many": (0, "N"),
    "one_or_many": (1, "N"), "one_or_more": (1, "N"),
}


def parse_cardinality(value):
    """Accept {min,max}, '0..N', '1..*', '1', 'N', 'one', 'zero_or_many' ... -> {'min':0|1,'max':1|'N'}."""
    lo = hi = None
    if isinstance(value, dict):
        lo, hi = value.get("min"), value.get("max")
    elif isinstance(value, str):
        t = value.strip().lower().replace(" ", "_").replace("-", "_")
        if t in _CARD_WORDS:
            lo, hi = _CARD_WORDS[t]
        elif ".." in t:
            lo, hi = t.split("..", 1)
        elif t in ("1", "n", "*", "m"):
            lo, hi = (1 if t == "1" else 0), t
    if lo is None or hi is None:
        raise ValueError(f"unreadable cardinality {value!r}; use {{\"min\": 0|1, \"max\": 1|\"N\"}}")
    try:
        lo = int(str(lo).strip())
    except ValueError:
        raise ValueError(f"cardinality min must be 0 or 1, got {lo!r}")
    if lo not in (0, 1):
        raise ValueError(f"cardinality min must be 0 or 1, got {lo}")
    h = str(hi).strip().lower()
    if h == "1":
        hi = 1
    elif h in ("n", "m", "*", "many") or (h.isdigit() and int(h) > 1):
        hi = "N"
    else:
        raise ValueError(f"cardinality max must be 1 or 'N', got {hi!r}")
    return {"min": lo, "max": hi}


def cardinality_text(card):
    return {(0, 1): "zero or one", (1, 1): "exactly one", (0, "N"): "zero or more",
            (1, "N"): "one or more"}[(card["min"], card["max"])]


# ------------------------------------------------------------------ conceptual
def _attribute(raw, where, errs):
    if not isinstance(raw, dict):
        errs.add(f"{where}: every attribute must be an object.")
        return None
    name = _ident(raw.get("name"), f"{where}: attribute name", errs)
    typ = _s(raw.get("type")).lower() or "string"
    if typ not in LOGICAL_TYPES:
        errs.add(f"{where}.{name}: type '{typ}' must be one of {', '.join(LOGICAL_TYPES)}.")
    attr = {"name": name, "description": _s(raw.get("description")), "type": typ,
            "required": _bool(raw.get("required"), True), "unique": _bool(raw.get("unique")),
            "derived": _bool(raw.get("derived"))}
    if attr["derived"]:
        attr["derivation"] = _s(raw.get("derivation"))
    allowed = _strs(raw.get("allowed_values"))
    if typ == "enum" and not allowed:
        errs.add(f"{where}.{name}: an enum attribute needs 'allowed_values'.")
    if allowed:
        attr["allowed_values"] = allowed
    return attr


def validate_conceptual(data):
    """Validate the conceptual part (entities, relationships, rules, assumptions)."""
    errs = _Errors()
    ents_raw = data.get("entities")
    if not isinstance(ents_raw, list) or not ents_raw:
        raise InvalidResponseError("'entities' must be a non-empty list.")
    entities, ids = [], []
    for e in ents_raw:
        if not isinstance(e, dict):
            errs.add("Every entity must be an object.")
            continue
        eid = _slug(e.get("id"), "Entity id", errs)
        ids.append(eid)
        w = f"entity '{eid}'"
        if not _s(e.get("name")) or not _s(e.get("description")):
            errs.add(f"{w}: 'name' and 'description' are required.")
        attrs = [a for a in (_attribute(a, w, errs) for a in _list(e.get("attributes"))) if a]
        if not attrs:
            errs.add(f"{w}: needs at least one attribute.")
        _unique([a["name"] for a in attrs], f"attribute in {w}", errs)
        names = {a["name"] for a in attrs}
        idents = []
        for i in _list(e.get("identifiers")):
            if not isinstance(i, dict):
                errs.add(f"{w}: every identifier must be an object with 'attributes' and 'primary'.")
                continue
            cols = _strs(i.get("attributes"))
            bad = [c for c in cols if c not in names]
            if not cols or bad:
                errs.add(f"{w}: identifier uses unknown attributes {bad or '(none given)'}.")
                continue
            idents.append({"attributes": cols, "primary": _bool(i.get("primary"))})
        if sum(i["primary"] for i in idents) != 1:
            errs.add(f"{w}: exactly one identifier must have \"primary\": true (found {sum(i['primary'] for i in idents)}).")
        kind = _s(e.get("kind")).lower() or "strong"
        if kind not in ("strong", "weak"):
            errs.add(f"{w}: kind must be 'strong' or 'weak'.")
        ent = {"id": eid, "name": _s(e.get("name")), "description": _s(e.get("description")), "kind": kind,
               "attributes": attrs, "identifiers": idents}
        if kind == "weak":
            ent["identifying_relationship"] = _s(e.get("identifying_relationship"))
            if not ent["identifying_relationship"]:
                errs.add(f"{w}: a weak entity needs 'identifying_relationship' (id of the relationship to its owner).")
        entities.append(ent)
    _unique(ids, "entity id", errs)
    _unique([e["name"] for e in entities if e["name"]], "entity name", errs)
    by_id = {e["id"]: e for e in entities}
    by_name = {e["name"].lower(): e["id"] for e in entities}

    def resolve(ref):
        r = _s(ref)
        return r if r in by_id else by_name.get(r.lower())

    rels = []
    for r in _list(data.get("relationships")):
        if not isinstance(r, dict):
            errs.add("Every relationship must be an object.")
            continue
        rid = _slug(r.get("id"), "Relationship id", errs)
        w = f"relationship '{rid}'"
        parts = _list(r.get("participants"))
        if len(parts) != 2:
            errs.add(f"{w}: must have exactly 2 participants (model n-ary relationships as an associative entity).")
            continue
        ps = []
        for p in parts:
            if not isinstance(p, dict):
                errs.add(f"{w}: participants must be objects.")
                continue
            ent = resolve(p.get("entity"))
            if ent is None:
                errs.add(f"{w}: participant entity '{_s(p.get('entity'))}' does not exist.")
                continue
            try:
                card = parse_cardinality(p.get("cardinality"))
            except ValueError as ex:
                errs.add(f"{w}: {ex}")
                continue
            ps.append({"entity": ent, "role": _s(p.get("role")), "cardinality": card})
        if len(ps) != 2:
            continue
        if ps[0]["entity"] == ps[1]["entity"] and (not ps[0]["role"] or not ps[1]["role"]
                                                   or ps[0]["role"].lower() == ps[1]["role"].lower()):
            errs.add(f"{w}: a self-referencing relationship needs two different 'role' values.")
        rattrs = [a for a in (_attribute(a, w, errs) for a in _list(r.get("attributes"))) if a]
        _unique([a["name"] for a in rattrs], f"attribute in {w}", errs)
        rel = {"id": rid, "name": _s(r.get("name")) or rid.replace("_", " "), "description": _s(r.get("description")),
               "participants": ps, "attributes": rattrs, "identifying": _bool(r.get("identifying"))}
        rel.update(_classify(rel))
        if rattrs and rel["type"] != "many_to_many":
            errs.add(f"{w}: relationship attributes are only allowed on many-to-many relationships; "
                     "otherwise promote the relationship to an entity or move the attribute onto an entity.")
        rels.append(rel)
    _unique([r["id"] for r in rels], "relationship id", errs)
    rel_ids = {r["id"] for r in rels}
    for e in entities:
        if e["kind"] == "weak":
            ir = e.get("identifying_relationship")
            rel = next((r for r in rels if r["id"] == ir), None)
            if rel is None or e["id"] not in [p["entity"] for p in rel["participants"]]:
                errs.add(f"entity '{e['id']}': identifying_relationship '{ir}' must be a relationship that this entity takes part in.")
            else:
                rel["identifying"] = True

    rules = []
    for r in _list(data.get("business_rules")):
        if not isinstance(r, dict) or not _s(r.get("description")):
            errs.add("Every business rule needs a 'description'.")
            continue
        applies = _strs(r.get("applies_to"))
        bad = [a for a in applies if a not in by_id and a not in rel_ids]
        if bad:
            errs.add(f"business rule '{_s(r.get('id'))}': applies_to references unknown entity/relationship ids {bad}.")
        src = _s(r.get("source")).lower() or "requirement"
        rule = {"id": _slug(r.get("id"), "Business rule id", errs), "description": _s(r["description"]),
                "source": src if src in ("requirement", "assumption") else "requirement", "applies_to": applies}
        if isinstance(r.get("enforcement"), dict):       # canonical re-validation keeps it
            rule["enforcement"] = r["enforcement"]
        rules.append(rule)
    _unique([r["id"] for r in rules], "business rule id", errs)
    out = {"entities": entities, "relationships": rels, "business_rules": rules,
           "assumptions": _assumptions(data.get("assumptions"), errs),
           "open_questions": _open_questions(data.get("open_questions"), errs)}
    errs.raise_if_any("The conceptual model is invalid:")
    return out


def _classify(rel):
    a, b = rel["participants"]
    ma, mb = a["cardinality"]["max"], b["cardinality"]["max"]
    # participant X with max 1 is the "one" side: one instance of the other relates to at most one X
    if ma == 1 and mb == 1:
        return {"type": "one_to_one"}
    if ma == "N" and mb == "N":
        return {"type": "many_to_many"}
    one, many = (a, b) if ma == 1 else (b, a)
    return {"type": "one_to_many", "one_side": one["entity"], "many_side": many["entity"]}


def _assumptions(raw, errs):
    out = []
    for i, a in enumerate(_list(raw), 1):
        text = _s(a.get("text") if isinstance(a, dict) else a)
        if not text:
            errs.add("Every assumption needs non-empty 'text'.")
            continue
        d = a if isinstance(a, dict) else {}
        out.append({"id": _s(d.get("id")) or f"A{i}", "text": text, "rationale": _s(d.get("rationale")),
                    "related_to": _strs(d.get("related_to"))})
    _unique([a["id"] for a in out], "assumption id", errs)
    return out


def _open_questions(raw, errs):
    out = []
    for i, q in enumerate(_list(raw), 1):
        text = _s(q.get("text") if isinstance(q, dict) else q)
        if not text:
            errs.add("Every open question needs non-empty 'text'.")
            continue
        d = q if isinstance(q, dict) else {}
        out.append({"id": _s(d.get("id")) or f"Q{i}", "text": text, "impact": _s(d.get("impact"))})
    return out


# ----------------------------------------------- conditional (partial) indexes
WHERE_OPERATORS = ("=", "<>", "IN", "IS NULL", "IS NOT NULL")
_OP_ALIASES = {"==": "=", "EQ": "=", "!=": "<>", "NE": "<>", "NOT NULL": "IS NOT NULL", "NULL": "IS NULL"}


def _scalar(v):
    return isinstance(v, (str, int, float, bool)) and not (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))))


def _where(raw, w, errs):
    """Structured condition of a partial (conditional) index -> [{"column", "operator"[, "value"]}], ANDed.

    Optional and backward compatible: an index without 'where' is an ordinary index. Free SQL text is rejected on
    purpose: a structured condition can be analysed (normalization) and rendered safely for every DBMS.
    Idempotent: canonical output is accepted again."""
    if raw is None or raw == [] or raw == {} or (isinstance(raw, str) and not raw.strip()):
        return []
    if isinstance(raw, str):
        errs.add(f"{w}: 'where' must be a list of structured conditions such as "
                 "[{\"column\": \"status\", \"operator\": \"=\", \"value\": \"active\"}] or "
                 "[{\"column\": \"deleted_at\", \"operator\": \"IS NULL\"}], not free SQL text.")
        return []
    items = [raw] if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        errs.add(f"{w}: 'where' must be a list of conditions.")
        return []
    out = []
    for c in items:
        if not isinstance(c, dict):
            errs.add(f"{w}: every 'where' condition must be an object with 'column', 'operator' and (usually) 'value'.")
            continue
        col = _s(c.get("column"))
        op = re.sub(r"[\s_]+", " ", _s(c.get("operator")).upper())
        op = _OP_ALIASES.get(op, op)
        if not col:
            errs.add(f"{w}: a 'where' condition needs a 'column'.")
            continue
        if op not in WHERE_OPERATORS:
            errs.add(f"{w}: 'where' operator '{_s(c.get('operator'))}' must be one of {', '.join(WHERE_OPERATORS)}.")
            continue
        cond = {"column": col, "operator": op}
        if op == "IN":
            vals = _list(c.get("value"))
            if not vals or not all(_scalar(v) for v in vals):
                errs.add(f"{w}: 'where' condition on '{col}': IN needs a non-empty list of plain values.")
                continue
            cond["value"] = vals
        elif op in ("=", "<>"):
            if not _scalar(c.get("value")):
                errs.add(f"{w}: 'where' condition on '{col}': '{op}' needs a plain value (use IS NULL / IS NOT NULL for NULL).")
                continue
            cond["value"] = c["value"]
        out.append(cond)
    return out


def _lit(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return "'" + v.replace("'", "''") + "'" if isinstance(v, str) else str(v)


def where_text(where):
    """Readable, DBMS-neutral text of a structured condition (used in documents and notes)."""
    parts = []
    for c in where or []:
        if c["operator"] == "IN":
            parts.append(f"{c['column']} IN ({', '.join(_lit(v) for v in c['value'])})")
        elif c["operator"] in ("IS NULL", "IS NOT NULL"):
            parts.append(f"{c['column']} {c['operator']}")
        else:
            parts.append(f"{c['column']} {c['operator']} {_lit(c['value'])}")
    return " AND ".join(parts)


# ----------------------------------------------------- structured triggers
def _triggers(raw, w, errs):
    """Optional structured database triggers of a table -> [{"name", "kind", ...}].

    kind "append_only": rows can be inserted but not changed/removed; "operations" is a subset of UPDATE, DELETE
    (default both). kind "immutable_columns": the listed "columns" cannot change after INSERT. Free SQL is never
    accepted. Key only present on a table when used, so existing designs stay byte-identical. Idempotent."""
    out = []
    for x in _list(raw):
        if not isinstance(x, dict):
            errs.add(f"{w}: every entry of 'triggers' must be an object.")
            continue
        name = _ident(x.get("name"), f"{w}: trigger name", errs)
        kind = _s(x.get("kind")).lower().replace("-", "_").replace(" ", "_")
        if kind not in TRIGGER_KINDS:
            errs.add(f"{w}: trigger '{name}' kind '{_s(x.get('kind'))}' must be one of {', '.join(TRIGGER_KINDS)}.")
            continue
        tr = {"name": name, "kind": kind, "description": _s(x.get("description"))}
        if kind == "append_only":
            ops = [o.upper() for o in _strs(x.get("operations"))] or list(_ROW_OPS)
            bad = [o for o in ops if o not in _ROW_OPS]
            if bad:
                errs.add(f"{w}: trigger '{name}' operations {bad} must be UPDATE and/or DELETE.")
            tr["operations"] = [o for o in _ROW_OPS if o in ops]
        else:
            tr["columns"] = _strs(x.get("columns"))
            if not tr["columns"]:
                errs.add(f"{w}: trigger '{name}' (immutable_columns) needs a non-empty 'columns' list.")
        out.append(tr)
    return out


# -------------------------------------------------------------------- physical
def _constraint_names(table):
    names = [table["primary_key_name"]] if table.get("primary_key_name") else []
    for key in ("foreign_keys", "unique_constraints", "check_constraints", "indexes", "triggers"):
        names += [x["name"] for x in table.get(key, [])]
    return names


def validate_physical(data, conceptual, dbms_id=None):
    """Validate tables / FDs / denormalizations against the conceptual model.

    Besides structure, this cross-checks the mapping: every entity has exactly one
    table, every attribute a column, every identifier a key, every 1:N / 1:1
    relationship a matching FK (nullability = optionality), every M:N relationship
    a junction table.
    """
    errs = _Errors()
    ents = {e["id"]: e for e in conceptual["entities"]}
    rels = {r["id"]: r for r in conceptual["relationships"]}
    raw_tables = data.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise InvalidResponseError("'tables' must be a non-empty list.")
    tables = []
    for t in raw_tables:
        if not isinstance(t, dict):
            errs.add("Every table must be an object.")
            continue
        tables.append(_table(t, errs))
    tnames = [t["name"] for t in tables]
    _unique(tnames, "table name", errs)
    tmap = {t["name"]: t for t in tables}

    if dbms_id == "sql_server":
        for t in tables:
            for fk in t["foreign_keys"]:
                if "RESTRICT" in (fk["on_delete"], fk["on_update"]):
                    errs.add(f"table '{t['name']}': foreign key '{fk['name']}' uses RESTRICT, which SQL Server does not support; use NO ACTION.")
    # --- references inside tables
    all_cnames = []
    for t in tables:
        w = f"table '{t['name']}'"
        cols = {c["name"] for c in t["columns"]}
        pk = t["primary_key"]
        if not pk:
            errs.add(f"{w}: must have a primary key.")
        for c in pk:
            if c not in cols:
                errs.add(f"{w}: primary key column '{c}' does not exist.")
            else:
                col = next(x for x in t["columns"] if x["name"] == c)
                col["nullable"] = False
        for fk in t["foreign_keys"]:
            _check_fk(t, fk, tmap, errs, dbms_id)
        for u in t["unique_constraints"] + t["indexes"]:
            bad = [c for c in u["columns"] if c not in cols]
            if bad or not u["columns"]:
                errs.add(f"{w}: '{u['name']}' references unknown columns {bad or '(none)'}.")
        for i in t["indexes"]:
            badw = [c["column"] for c in i.get("where", []) if c["column"] not in cols]
            if badw:
                errs.add(f"{w}: the 'where' condition of index '{i['name']}' uses unknown columns {badw}.")
            if i.get("where") and dbms_id == "mysql_mariadb":
                errs.add(f"{w}: index '{i['name']}' is partial (has a 'where' condition), which MySQL/MariaDB does not support; "
                         "use a full index, a generated column, or enforce the rule in the application and record an assumption.")
        for tr in t.get("triggers", []):
            badt = [c for c in tr.get("columns", []) if c not in cols]
            if badt:
                errs.add(f"{w}: trigger '{tr['name']}' uses unknown columns {badt}.")
            if dbms_id and dbms_id not in TRIGGER_DIALECTS:
                errs.add(f"{w}: trigger '{tr['name']}' cannot be generated for this DBMS (structured triggers are "
                         f"supported for SQLite only). Remove it and enforce the rule in the application: set the rule's "
                         f"enforcement mechanism to 'application' and say so in its note.")
        all_cnames += _constraint_names(t)
    _unique(all_cnames, "constraint/index name (names must be unique across the whole schema)", errs)

    # --- entity mapping
    entity_table = {}
    for t in tables:
        s = t["source"]
        if s["type"] == "entity":
            if s["id"] not in ents:
                errs.add(f"table '{t['name']}': source entity '{s['id']}' does not exist.")
            elif s["id"] in entity_table:
                errs.add(f"entity '{s['id']}' is implemented by more than one table.")
            else:
                entity_table[s["id"]] = t
        elif s["type"] == "relationship":
            if s["id"] not in rels:
                errs.add(f"table '{t['name']}': source relationship '{s['id']}' does not exist.")
            elif rels[s["id"]]["type"] != "many_to_many":
                errs.add(f"table '{t['name']}': only many-to-many relationships get their own table.")
        else:
            errs.add(f"table '{t['name']}': source.type must be 'entity' or 'relationship'.")
    for eid, e in ents.items():
        t = entity_table.get(eid)
        if t is None:
            errs.add(f"entity '{eid}' has no table (add a table with source {{\"type\":\"entity\",\"id\":\"{eid}\"}}).")
            continue
        colmap = {c.get("source_attribute"): c for c in t["columns"] if c.get("source_attribute")}
        for a in e["attributes"]:
            col = colmap.get(a["name"])
            if col is None:
                if not a["derived"]:
                    errs.add(f"table '{t['name']}': no column with source_attribute '{a['name']}' (entity '{eid}').")
                continue
            if a["required"] and col["nullable"]:
                errs.add(f"table '{t['name']}'.{col['name']}: attribute '{a['name']}' is required but the column is nullable.")
            if not a["required"] and not col["nullable"] and col["name"] not in t["primary_key"] and not col["default"]:
                errs.add(f"table '{t['name']}'.{col['name']}: attribute '{a['name']}' is optional but the column is NOT NULL (without a default).")
        for ident in e["identifiers"]:
            mapped = [colmap[a]["name"] for a in ident["attributes"] if a in colmap]
            if len(mapped) != len(ident["attributes"]):
                continue
            if not _is_unique(t, mapped):
                errs.add(f"table '{t['name']}': identifier {ident['attributes']} of entity '{eid}' is not enforced "
                         "(make it the primary key or add a UNIQUE constraint).")
    # --- relationship mapping
    for rid, r in rels.items():
        _check_relationship(r, ents, entity_table, tables, errs)
    for eid, e in ents.items():       # weak entities: owner FK inside the PK
        t = entity_table.get(eid)
        rel = rels.get(e.get("identifying_relationship"))
        if e["kind"] == "weak" and t and rel:
            fks = [fk for fk in t["foreign_keys"] if fk["relationship"] == rel["id"]]
            if not fks or not set(fks[0]["columns"]) <= set(t["primary_key"]):
                errs.add(f"weak entity '{eid}': the foreign key to its owner must be part of the primary key of '{t['name']}'.")

    fds = _fds(data.get("functional_dependencies"), tmap, errs)
    for t in tables:
        non_key = [c for c in t["columns"] if c["name"] not in t["primary_key"]]
        if non_key and not any(f["table"] == t["name"] for f in fds):
            errs.add(f"table '{t['name']}': no functional dependency listed (at least the primary key determining the other columns).")
    denorm = []
    for d in _list(data.get("denormalizations")):
        if not isinstance(d, dict) or not _s(d.get("table")) or not _s(d.get("justification")):
            errs.add("Every denormalization needs 'table', 'description' and a non-empty 'justification'.")
            continue
        if _s(d["table"]) not in tmap:
            errs.add(f"denormalization refers to unknown table '{_s(d['table'])}'.")
        denorm.append({"table": _s(d["table"]), "description": _s(d.get("description")),
                       "justification": _s(d["justification"])})
    errs.raise_if_any("The physical design (tables) is invalid:")
    return {"tables": tables, "functional_dependencies": fds, "denormalizations": denorm}


def _table(t, errs):
    name = _ident(t.get("name"), "Table name", errs)
    w = f"table '{name}'"
    src = t.get("source") if isinstance(t.get("source"), dict) else {}
    cols = []
    for c in _list(t.get("columns")):
        if not isinstance(c, dict):
            errs.add(f"{w}: every column must be an object.")
            continue
        cname = _ident(c.get("name"), f"{w}: column name", errs)
        if not _s(c.get("data_type")):
            errs.add(f"{w}.{cname}: 'data_type' is required (use the chosen DBMS's types).")
        cols.append({"name": cname, "data_type": _s(c.get("data_type")), "nullable": _bool(c.get("nullable"), True),
                     "default": (_s(c.get("default")) or None) if c.get("default") is not None else None,
                     "identity": _bool(c.get("identity")),
                     "source_attribute": _s(c.get("source_attribute")) or None,
                     "description": _s(c.get("description"))})
    if not cols:
        errs.add(f"{w}: needs at least one column.")
    _unique([c["name"] for c in cols], f"column in {w}", errs)

    def named(key, fields):
        out = []
        for x in _list(t.get(key)):
            if not isinstance(x, dict):
                errs.add(f"{w}: every entry of '{key}' must be an object.")
                continue
            item = {"name": _ident(x.get("name"), f"{w}: name in {key}", errs)}
            for f in fields:
                item[f] = _strs(x.get(f)) if f in ("columns", "ref_columns") else x.get(f)
            out.append(item)
        return out
    fks = []
    for x in named("foreign_keys", ("columns", "ref_table", "ref_columns", "on_delete", "on_update", "relationship")):
        x["ref_table"] = _s(x["ref_table"])
        x["relationship"] = _s(x["relationship"]) or None
        for k in ("on_delete", "on_update"):
            v = _s(x[k]).upper().replace("_", " ") or "NO ACTION"
            if v not in FK_ACTIONS:
                errs.add(f"{w}: foreign key '{x['name']}' {k.upper()} '{v}' must be one of {', '.join(FK_ACTIONS)}.")
            x[k] = v
        fks.append(x)
    uniques = named("unique_constraints", ("columns",))
    idx = named("indexes", ("columns", "unique", "purpose"))
    raw_idx = [x for x in _list(t.get("indexes")) if isinstance(x, dict)]      # same order as idx
    for i, raw in zip(idx, raw_idx):
        i["unique"] = _bool(i["unique"])
        i["purpose"] = _s(i["purpose"])
        where = _where(raw.get("where"), f"{w}: index '{i['name']}'", errs)
        if where:                                    # key only present when used: old designs stay byte-identical
            i["where"] = where
        flag = raw.get("conditional")                # optional structured marker: is the uniqueness conditional?
        if flag is not None and flag != "":
            flag = _bool(flag, None)
            if flag is None:
                errs.add(f"{w}: index '{i['name']}': 'conditional' must be true or false.")
            elif flag and not where:
                errs.add(f"{w}: index '{i['name']}' is marked \"conditional\": true but has no structured 'where' list; add the "
                         f"condition (e.g. [{{\"column\": \"status\", \"operator\": \"=\", \"value\": \"active\"}}]) or set "
                         f"\"conditional\": false.")
            elif not flag and where:
                errs.add(f"{w}: index '{i['name']}' has a 'where' condition but \"conditional\": false.")
            else:
                i["conditional"] = flag              # key only present when used: old designs stay byte-identical
    checks = []
    for x in _list(t.get("check_constraints")):
        if not isinstance(x, dict) or not _s(x.get("expression")):
            errs.add(f"{w}: every check constraint needs 'name' and 'expression'.")
            continue
        checks.append({"name": _ident(x.get("name"), f"{w}: check name", errs), "expression": _s(x["expression"]),
                       "description": _s(x.get("description"))})
    out = {"name": name, "description": _s(t.get("description")),
           "source": {"type": _s(src.get("type")).lower(), "id": _s(src.get("id"))},
           "kind": "junction" if _s(src.get("type")).lower() == "relationship" else "entity",
           "columns": cols, "primary_key": _strs(t.get("primary_key")),
           "primary_key_name": _s(t.get("primary_key_name")) or None,
           "foreign_keys": fks, "unique_constraints": uniques, "check_constraints": checks, "indexes": idx}
    trig = _triggers(t.get("triggers"), w, errs)
    if trig:                                         # key only present when used: old designs stay byte-identical
        out["triggers"] = trig
    return out


def _check_fk(t, fk, tmap, errs, dbms_id=None):
    w = f"table '{t['name']}': foreign key '{fk['name']}'"
    cols = {c["name"]: c for c in t["columns"]}
    bad = [c for c in fk["columns"] if c not in cols]
    if not fk["columns"] or bad:
        errs.add(f"{w} uses unknown columns {bad or '(none)'}.")
        return
    ref = tmap.get(fk["ref_table"])
    if ref is None:
        errs.add(f"{w} references unknown table '{fk['ref_table']}'.")
        return
    rcols = {c["name"]: c for c in ref["columns"]}
    if not fk["ref_columns"] or len(fk["ref_columns"]) != len(fk["columns"]) or any(c not in rcols for c in fk["ref_columns"]):
        errs.add(f"{w}: ref_columns must exist in '{ref['name']}' and match the number of columns.")
        return
    if not _is_referenceable(ref, fk["ref_columns"], dbms_id):
        errs.add(f"{w}: referenced columns {fk['ref_columns']} of '{ref['name']}' are not its primary key or a UNIQUE key "
                 f"(they must match EXACTLY one declared key; declared keys: {_declared_keys(ref)}).")
    for c, rc in zip(fk["columns"], fk["ref_columns"]):      # pairwise: column i references ref column i
        problem = fk_type_problem(cols[c]["data_type"], rcols[rc]["data_type"], dbms_id)
        if problem:
            errs.add(f"{w}: column '{t['name']}.{c}' -> '{ref['name']}.{rc}': {problem}")
    if "SET NULL" in (fk["on_delete"], fk["on_update"]) and any(
            not cols[c]["nullable"] or c in t["primary_key"] for c in fk["columns"]):
        errs.add(f"{w}: SET NULL cannot be used on NOT NULL / primary-key columns.")


def _key_defs(t):
    """Every declared unique key with the facts that decide whether it identifies EVERY row.

    kind / name / columns, `where` (condition of a partial unique index, [] otherwise), `nullable_columns`
    (non-PK columns that may be NULL: SQL treats NULLs as distinct, so such a key does not identify rows with
    NULLs) and `total`: True only for a key without condition and without nullable columns. Only total keys
    are candidate keys / superkeys in the normalization analysis."""
    nullable = {c["name"] for c in t["columns"] if c["nullable"]} - set(t["primary_key"])
    raw = [("PRIMARY KEY", t.get("primary_key_name"), t["primary_key"], [])]
    raw += [("UNIQUE", u["name"], u["columns"], []) for u in t["unique_constraints"]]
    raw += [("UNIQUE INDEX", i["name"], i["columns"], list(i.get("where") or [])) for i in t["indexes"] if i["unique"]]
    out = []
    for kind, name, cols, where in raw:
        if not cols:
            continue
        nul = [c for c in cols if c in nullable]
        out.append({"kind": kind, "name": name, "columns": list(cols), "where": where, "nullable_columns": nul,
                    "total": not where and not nul})
    return out


def _keys(t):
    """Declared keys that apply to ALL rows (a nullable UNIQUE still counts: it is unique among non-NULL values and
    may be referenced by a foreign key). Partial unique indexes are excluded: they constrain only some rows and
    cannot be referenced by a foreign key."""
    return [(d["kind"], d["columns"]) for d in _key_defs(t) if not d["where"]]


def _declared_keys(t):
    return "; ".join(f"{kind} ({', '.join(cols)})" for kind, cols in _keys(t)) or "none"


def _is_referenceable(t, cols, dbms_id=None):
    """Can a foreign key point at exactly `cols`? They must equal a declared key - a
    superset of a key (or a mix of columns from two different keys) is NOT enough.
    PostgreSQL / SQL Server / SQLite ignore column order; MySQL/MariaDB needs the same order."""
    if len(set(cols)) != len(cols):
        return False
    for _, key in _keys(t):
        if (list(key) == list(cols)) if dbms_id == "mysql_mariadb" else (set(key) == set(cols)):
            return True
    return False


def _is_unique(t, cols):
    s = set(cols)
    if t["primary_key"] and set(t["primary_key"]) <= s:
        return True
    return any(set(u["columns"]) <= s and u["columns"] for u in t["unique_constraints"]) or any(
        i["unique"] and not i.get("where") and set(i["columns"]) <= s and i["columns"] for i in t["indexes"])


def _key_contains(t, cols):
    """True if some key of the table (PK / UNIQUE / unique index) includes all `cols`."""
    s = set(cols)
    keys = [t["primary_key"]] + [u["columns"] for u in t["unique_constraints"]] + [
        i["columns"] for i in t["indexes"] if i["unique"] and not i.get("where")]
    return any(k and s <= set(k) for k in keys)


def _check_relationship(r, ents, entity_table, tables, errs):
    rid = r["id"]
    a, b = r["participants"]
    ta, tb = entity_table.get(a["entity"]), entity_table.get(b["entity"])
    if not ta or not tb:
        return                                     # already reported as missing entity table
    tagged = [(t, fk) for t in tables for fk in t["foreign_keys"] if fk["relationship"] == rid]
    w = f"relationship '{rid}'"
    if r["type"] == "many_to_many":
        junction = [t for t in tables if t["source"] == {"type": "relationship", "id": rid}]
        if len(junction) != 1:
            errs.add(f"{w} is many-to-many: it needs exactly one junction table with source "
                     f"{{\"type\":\"relationship\",\"id\":\"{rid}\"}}.")
            return
        j = junction[0]
        fks = [fk for t, fk in tagged if t is j]
        want = sorted([ta["name"], tb["name"]])
        if sorted(fk["ref_table"] for fk in fks) != want:
            errs.add(f"{w}: junction table '{j['name']}' needs one FK (tagged relationship '{rid}') to each of {want}.")
            return
        if not _key_contains(j, [c for fk in fks for c in fk["columns"]]):
            errs.add(f"{w}: junction table '{j['name']}' needs a composite PRIMARY KEY or UNIQUE constraint that contains both "
                     "foreign keys (it may add e.g. a date column if the same pair may legitimately repeat).")
        for fk in fks:
            if any(next(c for c in j["columns"] if c["name"] == x)["nullable"] for x in fk["columns"]):
                errs.add(f"{w}: junction FK '{fk['name']}' columns must be NOT NULL.")
        colmap = {c["source_attribute"] for c in j["columns"] if c["source_attribute"]}
        for at in r["attributes"]:
            if at["name"] not in colmap:
                errs.add(f"junction table '{j['name']}': relationship attribute '{at['name']}' has no column (source_attribute).")
        return
    if r["type"] == "one_to_many":
        fk_entity, other = r["many_side"], r["one_side"]
    else:                                          # one-to-one: FK may live on either side
        fk_entity = None
        other = None
    cands = [(t, fk) for t, fk in tagged
             if (fk_entity is None and fk["ref_table"] in (ta["name"], tb["name"]))
             or (fk_entity is not None and t is entity_table[fk_entity] and fk["ref_table"] == entity_table[other]["name"])]
    if len(cands) != 1:
        side = f" on table '{entity_table[fk_entity]['name']}' referencing '{entity_table[other]['name']}'" if fk_entity else ""
        errs.add(f"{w} ({r['type'].replace('_', '-')}) needs exactly one foreign key{side} with \"relationship\": \"{rid}\" "
                 f"(found {len(cands)}).")
        return
    t, fk = cands[0]
    if r["type"] == "one_to_many":
        # the referenced ("one") side is the participant whose max is 1
        ref_part = next(p for p in r["participants"] if p["cardinality"]["max"] == 1)
    else:
        holder = next(e for e, tt in entity_table.items() if tt is t)
        others = [p for p in r["participants"] if p["entity"] != holder]
        ref_part = others[0] if others else r["participants"][0]
    mandatory = ref_part["cardinality"]["min"] == 1
    colinfo = {c["name"]: c for c in t["columns"]}
    for c in fk["columns"]:
        in_pk = c in t["primary_key"]
        if mandatory and colinfo[c]["nullable"] and not in_pk:
            errs.add(f"{w}: participation is mandatory, so FK column '{t['name']}.{c}' must be NOT NULL.")
        if not mandatory and not colinfo[c]["nullable"] and not in_pk:
            errs.add(f"{w}: participation is optional (min 0), so FK column '{t['name']}.{c}' must be nullable.")
    if r["type"] == "one_to_one" and not _is_unique(t, fk["columns"]):
        errs.add(f"{w} is one-to-one: FK '{fk['name']}' needs a UNIQUE constraint (or be the primary key).")


def _fds(raw, tmap, errs):
    out = []
    for f in _list(raw):
        if not isinstance(f, dict):
            errs.add("Every functional dependency must be an object.")
            continue
        t = tmap.get(_s(f.get("table")))
        det, dep = _strs(f.get("determinant")), _strs(f.get("dependent"))
        if t is None:
            errs.add(f"functional dependency refers to unknown table '{_s(f.get('table'))}'.")
            continue
        cols = {c["name"] for c in t["columns"]}
        bad = [c for c in det + dep if c not in cols]
        if not det or not dep or bad:
            errs.add(f"functional dependency on '{t['name']}' has empty sides or unknown columns {bad}.")
            continue
        out.append({"table": t["name"], "determinant": det, "dependent": dep, "note": _s(f.get("note"))})
    return out


# ------------------------------------------------------------- business rules
def apply_rule_enforcement(rules, physical, data):
    """Attach `enforcement` {mechanism, implemented_by, note} to every business rule
    (from the logical-stage output) and validate the references."""
    errs = _Errors()
    names, cols = set(), set()
    for t in physical["tables"]:
        names |= {n.lower() for n in _constraint_names(t)}
        cols |= {f"{t['name']}.{c['name']}".lower() for c in t["columns"]}
    given = {}
    for e in _list(data.get("business_rule_enforcement")):
        if isinstance(e, dict) and _s(e.get("rule_id")):
            given[_s(e["rule_id"])] = e
    out = []
    for r in rules:
        g = given.get(r["id"]) or (r.get("enforcement") if isinstance(r.get("enforcement"), dict) else None)
        if not g:
            errs.add(f"business rule '{r['id']}' has no entry in 'business_rule_enforcement'.")
            continue
        mech = _s(g.get("mechanism")).lower()
        impl = _strs(g.get("implemented_by"))
        if mech not in ENFORCEMENTS:
            errs.add(f"business rule '{r['id']}': mechanism must be one of {', '.join(ENFORCEMENTS)}.")
        elif mech == "schema":
            bad = [i for i in impl if i.lower() not in names and i.lower() not in cols]
            if not impl or bad:
                errs.add(f"business rule '{r['id']}': 'implemented_by' must list existing constraint/index names or "
                         f"'table.column' (not found: {bad or 'nothing listed'}).")
        elif not _s(g.get("note")):
            errs.add(f"business rule '{r['id']}': a {mech}-level rule needs a 'note' explaining how it is enforced.")
        out.append(dict(r, enforcement={"mechanism": mech, "implemented_by": impl, "note": _s(g.get("note"))}))
    errs.raise_if_any("Business-rule enforcement is invalid:")
    return out


# ------------------------------------------------------------------ full design
def build_design(project_name, dbms, conceptual, physical, rules, extra_assumptions=(), meta=None,
                 requirement_gaps=()):
    assumptions = list(conceptual["assumptions"])
    seen = {a["id"] for a in assumptions}
    for a in extra_assumptions:
        a = dict(a)
        while a["id"] in seen:
            a["id"] += "_b"
        seen.add(a["id"])
        assumptions.append(a)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_name": project_name,
        "dbms": {"id": dbms["selected_id"], "name": dbms["selected"]},
        "conceptual": {"entities": conceptual["entities"], "relationships": conceptual["relationships"]},
        "physical": physical,
        "business_rules": rules,
        "assumptions": assumptions,
        "open_questions": conceptual["open_questions"],
        "requirement_gaps": list(requirement_gaps),
        "meta": meta or {},
    }


def validate_design(design):
    """Validate a complete canonical design (e.g. loaded from design.json). Returns a
    normalized deep copy; raises InvalidResponseError listing every problem."""
    if not isinstance(design, dict):
        raise InvalidResponseError("design must be an object.")
    if design.get("schema_version") != SCHEMA_VERSION:
        raise InvalidResponseError(f"Unsupported design schema_version {design.get('schema_version')!r}; expected {SCHEMA_VERSION}.")
    d = copy.deepcopy(design)
    conc_in = dict(d.get("conceptual") or {}, business_rules=d.get("business_rules"),
                   assumptions=d.get("assumptions"), open_questions=d.get("open_questions"))
    conc = validate_conceptual(conc_in)
    phys = validate_physical(d.get("physical") or {}, conc, _s((d.get("dbms") or {}).get("id")))
    rules = apply_rule_enforcement(conc["business_rules"], phys, {})
    dbms = d.get("dbms") or {}
    if not _s(dbms.get("id")) or not _s(dbms.get("name")):
        raise InvalidResponseError("design.dbms needs 'id' and 'name'.")
    out = dict(d)
    out.update({"conceptual": {"entities": conc["entities"], "relationships": conc["relationships"]},
                "physical": phys, "business_rules": rules, "assumptions": conc["assumptions"],
                "open_questions": conc["open_questions"], "requirement_gaps": _list(d.get("requirement_gaps")),
                "meta": d.get("meta") or {}})
    return out


def link_relationships(design):
    """Add, for every relationship, where it is physically implemented (derived,
    never trusted from input): FK table/columns or junction table."""
    tables = design["physical"]["tables"]
    for r in design["conceptual"]["relationships"]:
        if r["type"] == "many_to_many":
            j = next(t for t in tables if t["source"] == {"type": "relationship", "id": r["id"]})
            r["implemented_by"] = {"kind": "junction_table", "table": j["name"],
                                   "foreign_keys": [fk["name"] for fk in j["foreign_keys"] if fk["relationship"] == r["id"]]}
        else:
            t, fk = next((t, fk) for t in tables for fk in t["foreign_keys"] if fk["relationship"] == r["id"])
            r["implemented_by"] = {"kind": "foreign_key", "table": t["name"], "foreign_keys": [fk["name"]],
                                   "columns": fk["columns"]}
    return design
