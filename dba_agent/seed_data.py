"""Stage 6B - seed data: model of the physical schema, validation of LLM-proposed records, deterministic INSERTs.

Pipeline (the LLM never writes SQL):
    design.json (approved) --build_model--> SeedModel
    LLM proposes  {"tables": {"<table>": [ {column: value, ...}, ... ]}}
    validate_records(model, proposal)  -> normalised records + findings   (pure Python, no database needed)
    render_seed_sql(model, records)    -> seed.sql in the dialect of the chosen DBMS

validate_records checks, for every table: it is populated, columns exist, NOT NULL, data types / lengths / ranges,
PRIMARY KEY and UNIQUE uniqueness, FOREIGN KEYs (composite, optional/NULL, self-referencing), CHECK constraints
(evaluated in an in-memory SQLite when the expression allows it) and obvious placeholder values ("test1").
*Coverage* requirements (optional relationships, status values, many-to-many links) make the LLM retry; requirements that
are impossible or not applicable for the schema/row count are reported separately (`.coverage_na`) and never block.
Feasible coverage that is still missing on the last attempt keeps Stage 6 from completing (see sql_seed.generate).

Batch 3 semantics: partial UNIQUE indexes are evaluated through their structured `where` (rows outside the condition may
repeat values; NULL operands exclude a row; undecidable predicates are warnings, never invented verdicts; MySQL/MariaDB has no
partial indexes -> SeedError). A composite foreign key with a NULL component needs no parent row (MATCH SIMPLE in all four
supported DBMSs; `match: FULL` or an unknown dialect is not assumed simple). A NULL example is required only when it is FEASIBLE
(a witness row satisfying every CHECK, NULL-strict like schema.sql); provably impossible -> not applicable, undecidable ->
"uncertain". A missing NULL example is repaired offline (repair_coverage) and always reported.

HONEST SCOPE: CHECK expressions are evaluated with SQLite semantics; expressions that SQLite cannot evaluate are
reported as "not evaluated" and are only enforced by the real DBMS. Rendering is deterministic but only SQLite output
is executed by this project (see sql_validate.py).
"""
import copy
import datetime as _dt
import decimal
import itertools
import json
import math
import re
import sqlite3

from .errors import DBAAgentError
from .schema_sql import (DIALECT_NAMES, MYSQL, POSTGRESQL, SQL_SERVER, SQLITE, _split_literals, is_null_aware, quote,
                         render_check, strict_check)
from .sql_types import _INT_BITS, parse_type

MAX_ROWS_PER_TABLE = 1000


class SeedError(DBAAgentError):
    """The approved schema cannot be seeded safely (reported instead of producing misleading SQL)."""


# ------------------------------------------------------------------ type classification
_FLOAT = {"real", "float", "float4", "float8", "double", "double precision"}
_DECIMAL = {"decimal", "numeric", "dec", "money", "smallmoney"}
_STRING = {"varchar", "char", "character", "character varying", "text", "tinytext", "mediumtext", "longtext", "nvarchar",
           "nchar", "ntext", "clob", "bpchar", "string"}
_NATIONAL = {"nvarchar", "nchar", "ntext"}
_BINARY = {"bytea", "blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary", "image"}
_TS = {"timestamp", "timestamp without time zone", "datetime", "datetime2", "smalldatetime"}
_TSTZ = {"timestamptz", "timestamp with time zone", "datetimeoffset"}
_INT_RANGE = {8: 127, 16: 32767, 24: 8388607, 32: 2147483647, 64: 9223372036854775807}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK = r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,7})?)?"
_TIME = re.compile(r"^" + _CLOCK + r"$")
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T](" + _CLOCK + r"))?(Z|[+-]\d{2}(?::?\d{2})?)?$")
_ENUM_VALUE = re.compile(r"'((?:[^']|'')*)'")


def type_info(data_type, dialect):
    """-> {"kind", "length", "precision", "scale", "unsigned", "bits", "national", "values"}."""
    base, params, flags = parse_type(data_type)
    info = {"kind": "other", "length": None, "precision": None, "scale": None, "unsigned": "unsigned" in flags,
            "bits": None, "national": base in _NATIONAL, "values": None, "base": base}
    nums = [int(p) for p in params if p.isdigit()]
    if dialect == SQLITE:                                    # SQLite: exact names first, then the affinity rules
        up = base.upper()
        if base in ("boolean", "bool"):
            info["kind"] = "bool"
        elif base == "date":
            info["kind"] = "date"
        elif base in _TS or base in _TSTZ:
            info["kind"] = "timestamp"
        elif base == "time":
            info["kind"] = "time"
        elif base in _DECIMAL or up.startswith("NUM") or up.startswith("DEC"):
            # SQLite ignores precision/scale (NUMERIC affinity). A bare NUMERIC/DECIMAL therefore has NO scale limit; a declared
            # NUMERIC(p,s) keeps (p,s) as the stricter documented intent for the sample data.
            info.update(kind="decimal", precision=nums[0] if nums else None, scale=(nums[1] if len(nums) > 1 else 0) if nums else None)
        elif base in _INT_BITS or "INT" in up:
            info.update(kind="int", bits=64)
        elif "CHAR" in up or "CLOB" in up or "TEXT" in up:
            info.update(kind="string", length=nums[0] if nums else None)
        elif "BLOB" in up:
            info["kind"] = "binary"
        elif "REAL" in up or "FLOA" in up or "DOUB" in up:
            info["kind"] = "float"
        return info
    if base in _INT_BITS:
        info.update(kind="int", bits=_INT_BITS[base])
        if base in ("tinyint",) and dialect == SQL_SERVER:
            info["unsigned"] = True                           # SQL Server TINYINT is 0..255
    elif base in ("boolean", "bool") or (base == "bit" and dialect == SQL_SERVER):
        info["kind"] = "bool"
    elif base == "bit":
        info.update(kind="int", bits=64, unsigned=True)
    elif base in _DECIMAL:
        info.update(kind="decimal", precision=nums[0] if nums else None, scale=nums[1] if len(nums) > 1 else 0)
        if not nums and dialect == POSTGRESQL and base in ("numeric", "decimal", "dec"):
            info["scale"] = None                              # PostgreSQL: a bare NUMERIC is unconstrained (MySQL/SQL Server: scale 0)
        if base in ("money", "smallmoney"):
            info.update(precision=None, scale=4)
    elif base in _FLOAT:
        info["kind"] = "float"
    elif base in _STRING:
        info.update(kind="string", length=nums[0] if nums else None)
        if base in ("char", "character", "nchar", "bpchar") and not nums:
            info["length"] = 1
    elif base == "date":
        info["kind"] = "date"
    elif base in ("time", "time without time zone", "timetz", "time with time zone"):
        info["kind"] = "time"
    elif base in _TS:
        info["kind"] = "timestamp"
    elif base in _TSTZ:
        info["kind"] = "timestamptz"
    elif base in ("uuid", "uniqueidentifier"):
        info["kind"] = "uuid"
    elif base in ("json", "jsonb"):
        info["kind"] = "json"
    elif base in _BINARY:
        info["kind"] = "binary"
    elif base in ("enum", "set") and dialect == MYSQL:
        m = re.search(r"\((.*)\)", " ".join(str(data_type).split()))
        info.update(kind="enum" if base == "enum" else "set",
                    values=[v.replace("''", "'") for v in _ENUM_VALUE.findall(m.group(1) if m else "")])
    return info


# ------------------------------------------------------------------ model
class SeedModel:
    def __init__(self, dialect, tables, project, description, rules):
        self.dialect, self.tables, self.project, self.description, self.rules = dialect, tables, project, description, rules
        self.order = list(tables)                               # table order chosen by plan_tables()
        self.deferred = []                                      # [(table, fk name)] loaded as NULL, then UPDATEd
        self.participation = []                                 # declared mandatory parent->child participation

    def table(self, name):
        return self.tables[name]


def _literal_default(raw):
    """Python value of a simple DEFAULT (number / 'text' / true / false), else the marker None (unknown/dynamic)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    if re.fullmatch(r"[+-]?\d+\.\d+", s):
        return float(s)
    if re.fullmatch(r"'(?:[^']|'')*'", s):
        return s[1:-1].replace("''", "'")
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return None


def build_model(design, description=""):
    """Approved design -> SeedModel. Raises SeedError for schemas that cannot be seeded safely."""
    dialect = design["dbms"]["id"]
    if dialect not in DIALECT_NAMES:
        raise SeedError(f"DBMS '{dialect}' is not supported by the seed generator.")
    tables = {}
    for t in design["physical"]["tables"]:
        cols = {}
        for c in t["columns"]:
            info = type_info(c["data_type"], dialect)
            nn = (not c["nullable"]) or c["name"] in t["primary_key"]
            if c["identity"] and info["kind"] != "int":
                raise SeedError(f"{t['name']}.{c['name']}: identity column must be an integer type to be seeded.")
            if info["kind"] == "binary" and nn and c["default"] is None and not c["identity"]:
                raise SeedError(f"{t['name']}.{c['name']}: NOT NULL binary column ({c['data_type']}) without a DEFAULT "
                                f"cannot be seeded with realistic text data. Make it nullable or give it a default with `revise`.")
            cols[c["name"]] = {"name": c["name"], "type": c["data_type"], "info": info, "not_null": nn,
                               "identity": bool(c["identity"]), "default": c["default"],
                               "default_value": _literal_default(c["default"]), "in_pk": c["name"] in t["primary_key"]}
        uniques = [{"name": u["name"], "columns": list(u["columns"]), "where": []} for u in t["unique_constraints"]]
        for i in t["indexes"]:
            if i["unique"]:
                where = [dict(c) for c in (i.get("where") or [])]
                if where and dialect not in PARTIAL_UNIQUE_DIALECTS:
                    raise SeedError(f"{t['name']}: unique index {i['name']} is partial (WHERE {_where_text(where)}), which "
                                    f"{DIALECT_NAMES[dialect]} does not support; its rule cannot be enforced or checked. "
                                    f"Change the design with `revise`.")
                uniques.append({"name": i["name"], "columns": list(i["columns"]), "where": where})
        tables[t["name"]] = {"name": t["name"], "description": t.get("description", ""), "kind": t.get("kind", ""),
                             "columns": cols, "pk": list(t["primary_key"]), "uniques": uniques,
                             "fks": [dict(f) for f in t["foreign_keys"]],
                             "checks": [{"name": c["name"], "expression": c["expression"]} for c in t["check_constraints"]]}
    model = SeedModel(dialect, tables, design.get("project_name", ""), description,
                      [r.get("description", "") for r in design.get("business_rules", [])])
    model.participation = declared_participation(design)
    plan_tables(model)
    return model


# Dialects whose CREATE UNIQUE INDEX ... WHERE is generated by schema_sql (MySQL/MariaDB has no partial indexes).
PARTIAL_UNIQUE_DIALECTS = (SQLITE, POSTGRESQL, SQL_SERVER)

# Composite foreign key whose child columns are partly NULL. SQLite, PostgreSQL (default MATCH SIMPLE), MySQL/MariaDB
# (InnoDB) and SQL Server skip the parent lookup when ANY child component is NULL. A design may declare "match": "FULL"
# on a key (PostgreSQL/SQLite syntax), which makes a partly-NULL key invalid. Unknown dialect -> None (not verified).
FK_PARTIAL_NULL = {SQLITE: "simple", POSTGRESQL: "simple", MYSQL: "simple", SQL_SERVER: "simple"}


def fk_partial_null_semantics(dialect, fk):
    """-> "simple" (partly-NULL key is not checked), "full" (partly-NULL key is invalid) or None (unknown)."""
    if str(fk.get("match") or "").upper() == "FULL":
        return "full"
    return FK_PARTIAL_NULL.get(dialect)


def declared_participation(design):
    """Machine-readable mandatory participation declared by the design: a one-to-many relationship whose 'many'
    participant has min cardinality 1 means every parent row must have at least one child row.
    -> [{"relationship", "parent", "child", "fk"}]. Nothing is inferred from free text."""
    out = []
    conceptual = design.get("conceptual") or {}
    rels = {r.get("id"): r for r in conceptual.get("relationships", []) if isinstance(r, dict)}
    for t in design["physical"]["tables"]:
        for f in t["foreign_keys"]:
            r = rels.get(f.get("relationship"))
            if not r or r.get("type") != "one_to_many" or f["ref_table"] == t["name"]:
                continue
            many = [p for p in r.get("participants", []) if (p.get("cardinality") or {}).get("max") != 1]
            if len(many) == 1 and (many[0].get("cardinality") or {}).get("min") == 1:
                out.append({"relationship": r["id"], "parent": f["ref_table"], "child": t["name"], "fk": f["name"]})
    return out


def plan_tables(model):
    """Table insertion order (parents first). A cycle between tables is broken by loading one nullable foreign key as
    NULL and filling it with UPDATE afterwards; a cycle of NOT NULL keys can never hold a first row -> SeedError."""
    deferred = []
    names = list(model.tables)

    def deps(n):
        return {f["ref_table"] for f in model.tables[n]["fks"] if f["ref_table"] != n and (n, f["name"]) not in deferred}

    order, remaining = [], list(names)
    while remaining:
        ready = [n for n in remaining if not (deps(n) & set(remaining))]
        if ready:
            order.append(ready[0])
            remaining.remove(ready[0])
            continue
        broke = False
        for n in remaining:                                     # deterministic: design order
            for f in model.tables[n]["fks"]:
                if f["ref_table"] in remaining and f["ref_table"] != n and (n, f["name"]) not in deferred:
                    cm = model.tables[n]["columns"]
                    if all(not cm[c]["not_null"] for c in f["columns"]) and model.tables[n]["pk"]:
                        deferred.append((n, f["name"]))
                        broke = True
                        break
            if broke:
                break
        if not broke:
            raise SeedError("Circular dependency between tables " + ", ".join(f"`{n}`" for n in remaining) +
                            ": every foreign key in the cycle is NOT NULL (or the table has no primary key), so no first "
                            "row can be inserted. Make one foreign key nullable with `revise`.")
    model.order, model.deferred = order, deferred


# ------------------------------------------------------------------ value coercion
def _num_str(v):
    return str(v).strip()


def _valid_clock(clock):
    """True when HH:MM[:SS[.fff]] is a real clock value (00:00-23:59:59.9999999)."""
    h, mi = int(clock[0:2]), int(clock[3:5])
    sec = int(clock[6:8]) if len(clock) >= 8 else 0
    return h <= 23 and mi <= 59 and sec <= 59


def _valid_offset(tz):
    if not tz or tz == "Z":
        return True
    d = tz[1:].replace(":", "")
    return int(d[:2]) <= 14 and (len(d) < 4 or int(d[2:]) <= 59)


def coerce(v, info, dialect, where):
    """-> (normalised value, error or None)."""
    if v is None:
        return None, None
    k = info["kind"]
    try:
        if k == "int":
            if isinstance(v, bool):
                v = int(v)
            elif isinstance(v, float) and v.is_integer():
                v = int(v)
            elif isinstance(v, str) and re.fullmatch(r"[+-]?\d+", v.strip()):
                v = int(v.strip())
            if not isinstance(v, int):
                return None, f"{where}: {v!r} is not an integer"
            bits = info["bits"] or 64
            hi = _INT_RANGE[bits]
            lo = 0 if info["unsigned"] else -hi - 1
            hi = hi * 2 + 1 if info["unsigned"] else hi
            if not lo <= v <= hi:
                return None, f"{where}: {v} is outside the range of {bits}-bit integers"
            return v, None
        if k in ("decimal", "float"):
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                return None, f"{where}: {v!r} is not a number"
            d = decimal.Decimal(_num_str(v))
            if not d.is_finite():
                return None, f"{where}: {v!r} is not a finite number"
            if k == "float":
                return float(d), None
            if info["scale"] is None and info["precision"] is None:
                return d, None                                # unconstrained NUMERIC (SQLite / bare PostgreSQL NUMERIC)
            sc = info["scale"] or 0
            if -d.as_tuple().exponent > sc and d != d.quantize(decimal.Decimal(1).scaleb(-sc)):
                return None, f"{where}: {v} has more than {sc} decimal place(s)"
            if info["precision"] is not None:
                digits = len(d.quantize(decimal.Decimal(1).scaleb(-sc)).as_tuple().digits)
                if digits > info["precision"]:
                    return None, f"{where}: {v} needs more than {info['precision']} significant digits"
            return d.quantize(decimal.Decimal(1).scaleb(-sc)), None
        if k == "bool":
            if isinstance(v, bool):
                return v, None
            if v in (0, 1) and not isinstance(v, str):
                return bool(v), None
            if isinstance(v, str) and v.strip().lower() in ("true", "false"):
                return v.strip().lower() == "true", None
            return None, f"{where}: {v!r} is not a boolean (use true/false)"
        if k == "date":
            if not isinstance(v, str) or not _DATE.match(v.strip()):
                return None, f"{where}: {v!r} is not a date (YYYY-MM-DD)"
            _dt.date.fromisoformat(v.strip())
            return v.strip(), None
        if k == "time":
            if not isinstance(v, str) or not _TIME.match(v.strip()):
                return None, f"{where}: {v!r} is not a time (HH:MM[:SS])"
            t = v.strip()
            if not _valid_clock(t):
                return None, f"{where}: {v!r} is not a valid time (hours 00-23, minutes/seconds 00-59)"
            return t if len(t) >= 8 else t + ":00", None
        if k in ("timestamp", "timestamptz"):
            m = _TIMESTAMP.match(v.strip()) if isinstance(v, str) else None
            if not m:
                return None, f"{where}: {v!r} is not a timestamp (YYYY-MM-DD HH:MM:SS)"
            try:
                _dt.date.fromisoformat(m.group(1))
            except ValueError:
                return None, f"{where}: {v!r} is not a valid calendar date"
            clock = m.group(2) or "00:00:00"
            if not _valid_clock(clock) or not _valid_offset(m.group(3)):
                return None, f"{where}: {v!r} is not a valid timestamp (invalid clock value or time zone offset)"
            clock = clock if len(clock) >= 8 else clock + ":00"
            tz = m.group(3) or ""
            if len(tz) == 3 and tz[0] in "+-":
                tz += ":00"
            elif len(tz) == 5 and tz[0] in "+-":
                tz = tz[:3] + ":" + tz[3:]
            if tz and k == "timestamp":
                return None, f"{where}: {v!r} has a time zone but the column type has none"
            if k == "timestamptz" and not tz:
                tz = "+00:00"
            return f"{m.group(1)} {clock}{tz}", None
        if k == "uuid":
            return (v, None) if isinstance(v, str) and _UUID.match(v) else (None, f"{where}: {v!r} is not a UUID")
        if k == "json":
            if isinstance(v, (dict, list)):
                return json.dumps(v, ensure_ascii=False), None
            if isinstance(v, str):
                json.loads(v)
                return v, None
            return None, f"{where}: {v!r} is not JSON"
        if k == "enum":
            return (v, None) if v in (info["values"] or []) else (None, f"{where}: {v!r} is not one of {info['values']}")
        if k == "binary":
            return None, f"{where}: binary columns are not seeded; leave the value null"
        # strings and unknown types
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            return None, f"{where}: {v!r} is not text"
        s = " ".join(str(v).split()) if re.search(r"[\x00-\x1f]", str(v)) else str(v)
        if "\x00" in s:
            return None, f"{where}: contains a NUL character"
        if k == "string" and info["length"] is not None and len(s) > info["length"]:
            return None, f"{where}: {len(s)} characters exceed the column length {info['length']}"
        return s, None
    except (ValueError, decimal.InvalidOperation, json.JSONDecodeError) as e:
        return None, f"{where}: invalid value {v!r} ({e})"


# ------------------------------------------------------------------ placeholders
_ALWAYS = {"test", "testing", "sample", "dummy", "foo", "bar", "baz", "placeholder", "lorem", "ipsum", "string", "xxx",
           "asdf", "todo", "tbd", "abc", "xyz"}
_WITH_NUMBER = {"item", "record", "row", "entry", "data", "value", "name", "text", "example", "user", "field", "string"}


def looks_like_placeholder(value, column):
    s = str(value).strip()
    low = s.lower()
    if "lorem ipsum" in low:
        return True
    m = re.fullmatch(r"([a-z]+)[\s_\-]*#?(\d*)", low)
    if m and (m.group(1) in _ALWAYS or (m.group(2) and m.group(1) in _WITH_NUMBER)):
        return True
    if re.fullmatch(rf"{re.escape(column.lower())}[\s_\-]*#?\d+", low):
        return True
    return bool(re.match(r"(test|sample|dummy|foo|user|name)\d*@", low))


# ------------------------------------------------------------------ validation
class Validated:
    def __init__(self):
        self.records, self.errors, self.warnings, self.coverage, self.coverage_na = {}, [], [], [], []
        self.coverage_uncertain = []        # NULL examples whose feasibility could not be established (never blocking)
        self.coverage_items = []            # structured form of `coverage` (kind/table/column/message)
        self.semantic_unmet = []            # declared (machine-readable) participation that the data does not show
        self.repairs = []                   # deterministic offline corrections applied to the proposal (never silent)


def _key(row, cols):
    return tuple(row.get(c) for c in cols)


def _norm(v, ci):
    return v.casefold() if ci and isinstance(v, str) else v


def required_rows(model, rows):
    """-> ({table: required row count}, [notes]). Every table must reach the requested target `rows` unless the schema
    makes that impossible, in which case the target is capped at the largest feasible count and a note says why:
      * a UNIQUE/PK made only of foreign-key columns (1:1 and link tables) allows at most the product of the
        parents' row counts;
      * a UNIQUE/PK single column limited by a CHECK ... IN (...) list / ENUM allows at most that many values."""
    req, notes = {}, []
    for name in model.order:
        t = model.tables[name]
        cap, why = rows, ""
        for key in ([t["pk"]] if t["pk"] else []) + [u["columns"] for u in t["uniques"] if not u.get("where")]:
            cols = set(key)
            fks = [f for f in t["fks"] if set(f["columns"]) <= cols and f["ref_table"] != name]
            if fks and set().union(*(set(f["columns"]) for f in fks)) == cols and all(f["ref_table"] in req for f in fks):
                n = 1
                for f in fks:
                    n *= req[f["ref_table"]]
                if n < cap:
                    cap, why = n, f"its key ({', '.join(key)}) is made of foreign keys, so at most {n} distinct rows exist"
            if len(key) == 1:
                vals = allowed_values(model, t).get(key[0])
                if vals and len(vals) < cap:
                    cap, why = len(vals), f"`{key[0]}` is unique and limited to {len(vals)} allowed values"
        req[name] = max(1, cap)
        if cap < rows:
            notes.append(f"requested {rows} rows for `{name}` cannot be satisfied: {why}; {req[name]} required instead")
    return req, notes


def validate_records(model, proposal, min_rows=3, max_rows=MAX_ROWS_PER_TABLE):
    """Check an LLM proposal against the schema. Never raises for bad data: returns Validated(.errors, .coverage,
    .warnings, .records). `.records` is only meaningful when there are no errors."""
    res = Validated()
    err = res.errors
    if not isinstance(proposal, dict) or not isinstance(proposal.get("tables"), dict):
        err.append('The answer must be a JSON object {"tables": {"<table>": [ {column: value}, ... ]}}.')
        return res
    given = proposal["tables"]
    for n in given:
        if n not in model.tables:
            err.append(f"Unknown table `{n}` (valid tables: {', '.join(model.tables)}).")
    records = {}
    for name, t in model.tables.items():
        rows = given.get(name)
        if not isinstance(rows, list) or not rows:
            err.append(f"Table `{name}` has no rows - every table must be populated.")
            continue
        need = min_rows.get(name, 1) if isinstance(min_rows, dict) else min_rows
        if len(rows) < need:
            err.append(f"Table `{name}` has {len(rows)} row(s); at least {need} are required.")
        if len(rows) > max_rows:
            err.append(f"Table `{name}` has {len(rows)} rows; at most {max_rows} are allowed.")
            continue
        records[name] = _normalise_table(model, t, rows, err)
    if err:
        return res
    for name, t in model.tables.items():
        _check_table(model, t, records, err, res.warnings)
    if not err:
        _check_row_cycles(model, records, err)
    if not err:
        items, res.coverage_na, res.coverage_uncertain = _coverage(model, records)
        res.coverage_items, res.coverage = items, [i["message"] for i in items]
        res.semantic_unmet = semantic_findings(model, records)
    res.records = records
    return res


def _normalise_table(model, t, rows, err):
    out, name, cols = [], t["name"], t["columns"]
    ci = {c.lower(): c for c in cols}
    ident = [c for c in cols.values() if c["identity"]]
    have_ids = [bool(isinstance(r, dict) and ident and r.get(ident[0]["name"]) is not None) for r in rows]
    if ident and any(have_ids) and not all(have_ids):
        err.append(f"Table `{name}`: give `{ident[0]['name']}` for every row or for none.")
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            err.append(f"Table `{name}` row {i} is not an object.")
            continue
        row, where_row = {}, f"{name} row {i}"
        for k in r:
            if k not in cols and k.lower() not in ci:
                err.append(f"{where_row}: unknown column `{k}` (columns: {', '.join(cols)}).")
        for cn, c in cols.items():
            present = cn in r or any(k.lower() == cn.lower() for k in r)
            raw = r[cn] if cn in r else next((r[k] for k in r if k.lower() == cn.lower()), None)
            if c["identity"] and raw is None:
                row[cn] = None                                  # assigned below (1..n)
                continue
            if not present and c["default"] is not None:
                row[cn] = _ABSENT                               # the database applies the DEFAULT
                continue
            v, e = coerce(raw, c["info"], model.dialect, f"{where_row}.{cn}")
            if e:
                err.append(e)
            row[cn] = v
        out.append(row)
    for c in ident:                                             # identity values: given, or 1..n in row order
        vals = [r.get(c["name"]) for r in out]
        if all(v is None for v in vals):
            for i, r in enumerate(out, 1):
                r[c["name"]] = i
        elif any(v is not None and v < 1 for v in vals if isinstance(v, int)):
            err.append(f"Table `{name}`: identity values of `{c['name']}` must be positive.")
    return out


class _Absent:
    def __repr__(self):
        return "<DEFAULT>"


_ABSENT = _Absent()


def _value_for_check(c, v):
    if v is _ABSENT:
        v = c["default_value"]
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, bool):
        return int(v)
    return v


def _check_table(model, t, records, err, warnings):
    name, rows, cols = t["name"], records[t["name"]], t["columns"]
    ci = model.dialect in (MYSQL, SQL_SERVER)                   # default collations compare text case-insensitively
    for i, r in enumerate(rows, 1):
        for cn, c in cols.items():
            v = r[cn]
            if v is None and c["not_null"] and not (c["identity"]):
                err.append(f"{name} row {i}: `{cn}` is NOT NULL but has no value.")
            if isinstance(v, str) and c["info"]["kind"] == "string" and not c["in_pk"] and looks_like_placeholder(v, cn):
                err.append(f"{name} row {i}: `{cn}` = {v!r} looks like a placeholder; use realistic values.")
    if t["pk"]:
        seen = set()
        for i, r in enumerate(rows, 1):
            k = tuple(_norm(r[c], ci) for c in t["pk"])
            if k in seen:
                err.append(f"{name} row {i}: duplicate primary key {dict(zip(t['pk'], _key(r, t['pk'])))}.")
            seen.add(k)
    for u in t["uniques"]:
        seen = set()
        for i, r in enumerate(rows, 1):
            if u.get("where"):
                member = where_matches(model, t, r, u["where"])
                if member is None:
                    warnings.append(f"{name} row {i}: the condition of partial UNIQUE `{u['name']}` "
                                    f"({_where_text(u['where'])}) cannot be evaluated here (value comes from a DEFAULT that is not "
                                    f"a plain literal, or an unsupported operator); only the DBMS enforces it for this row.")
                    continue
                if not member:
                    continue                                    # outside the condition: repeated values are legitimate
            vals = _key(r, u["columns"])
            if any(v is None or v is _ABSENT for v in vals):
                continue                                        # NULLs are distinct in a unique index (all four DBMSs as generated)
            k = tuple(_norm(v, ci) for v in vals)
            if k in seen:
                cond = f" among rows where {_where_text(u['where'])}" if u.get("where") else ""
                err.append(f"{name} row {i}: duplicate value {list(vals)} violates UNIQUE `{u['name']}`{cond}.")
            seen.add(k)
    for f in t["fks"]:
        parent = records[f["ref_table"]]
        keys = {tuple(_norm(p[c], ci) for c in f["ref_columns"]) for p in parent}
        sem = None
        for i, r in enumerate(rows, 1):
            vals = _key(r, f["columns"])
            if all(v is None for v in vals):
                continue
            if any(v is None for v in vals):
                sem = fk_partial_null_semantics(model.dialect, f)
                if sem == "simple":
                    continue                                    # MATCH SIMPLE: no parent row is required; NOT NULL is checked above
                why = ("MATCH FULL forbids a partly-NULL key" if sem == "full" else
                       f"the NULL semantics of composite keys are not verified for {DIALECT_NAMES[model.dialect]}")
                err.append(f"{name} row {i}: foreign key `{f['name']}` is only partly NULL {list(vals)} ({why}).")
            elif tuple(_norm(v, ci) for v in vals) not in keys:
                err.append(f"{name} row {i}: {f['columns']} = {list(vals)} does not exist in "
                           f"`{f['ref_table']}`.{f['ref_columns']} (foreign key `{f['name']}`).")
    _eval_checks(model, t, rows, err, warnings)


def _eval_checks(model, t, rows, err, warnings):
    if not t["checks"]:
        return
    cols = list(t["columns"])
    con = sqlite3.connect(":memory:")
    try:
        for ck in t["checks"]:
            try:
                expr = render_check(ck["expression"], cols, model.dialect, f"{t['name']}: check {ck['name']}", table=t["name"])
            except DBAAgentError as e:
                warnings.append(f"CHECK `{ck['name']}` on `{t['name']}` was not evaluated in Python ({e}); the DBMS enforces it.")
                continue
            if is_null_aware(ck["expression"]):                 # schema.sql generates these NULL-strict; mirror it exactly
                expr = strict_check(expr)
            sql = f"SELECT ({expr}) FROM (SELECT " + ", ".join(f'? AS "{c}"' for c in cols) + ")"
            for i, r in enumerate(rows, 1):
                vals = [_value_for_check(t["columns"][c], r[c]) for c in cols]
                try:
                    out = con.execute(sql, vals).fetchone()[0]
                except sqlite3.Error as e:
                    warnings.append(f"CHECK `{ck['name']}` on `{t['name']}` was not evaluated in Python ({e}); "
                                    f"the DBMS enforces it.")
                    break
                if out is not None and not out:
                    shown = {c: v for c, v in zip(cols, vals)}
                    err.append(f"{t['name']} row {i}: violates CHECK `{ck['name']}` ({ck['expression']}); values {shown}.")
    finally:
        con.close()


def _check_row_cycles(model, records, err):
    """Rows of one table that reference each other in a loop cannot be inserted in any order."""
    for name, t in model.tables.items():
        selfs = [f for f in t["fks"] if f["ref_table"] == name]
        if not selfs:
            continue
        rows = records[name]
        ci = model.dialect in (MYSQL, SQL_SERVER)
        for f in selfs:
            index = {tuple(_norm(r[c], ci) for c in f["ref_columns"]): n for n, r in enumerate(rows)}
            parent = {}
            for n, r in enumerate(rows):
                vals = _key(r, f["columns"])
                if all(v is not None for v in vals):
                    parent[n] = index.get(tuple(_norm(v, ci) for v in vals))
            for start in parent:
                seen, cur = set(), start
                while cur is not None and cur in parent:
                    if cur in seen:
                        err.append(f"{name}: rows reference each other in a loop through `{f['name']}`; at least one "
                                   f"row must have NULL there (a root).")
                        return
                    seen.add(cur)
                    cur = parent[cur]


# ------------------------------------------------------------------ coverage (optional relations, statuses, M:N)
_IN_LIST = re.compile(r"""["`\[]?(\w+)["`\]]?\s+IN\s*\(([^)]*)\)""", re.I)


def allowed_values(model, t):
    """{column: [allowed literals]} for CHECK (col IN (...)) and MySQL ENUM columns."""
    out = {}
    for ck in t["checks"]:
        for m in _IN_LIST.finditer(ck["expression"]):
            col = next((c for c in t["columns"] if c.lower() == m.group(1).lower()), None)
            vals = [x.replace("''", "'") for x in _ENUM_VALUE.findall(m.group(2))]
            nums = re.findall(r"(?<![\w'])-?\d+(?:\.\d+)?(?![\w'])", re.sub(r"'(?:[^']|'')*'", "", m.group(2)))
            if col and (vals or nums):
                out[col] = vals or nums
    for cn, c in t["columns"].items():
        if c["info"]["kind"] == "enum" and c["info"]["values"]:
            out[cn] = list(c["info"]["values"])
    return out


# ---- structured partial-index predicates (three-valued: True / False / None = cannot be evaluated here)
def _where_text(where):
    from .design_model import where_text
    return where_text(where)


def _cmp_equal(model, got, want):
    """True/False/None for `got = want` (both non-NULL). Text compares case-insensitively only where the default
    collation does (MySQL/MariaDB, SQL Server); numbers/booleans compare numerically."""
    if isinstance(got, str) and isinstance(want, str):
        return (got.casefold() == want.casefold()) if model.dialect in (MYSQL, SQL_SERVER) else got == want
    num = (int, float, decimal.Decimal, bool)
    if isinstance(got, num) and isinstance(want, num):
        return float(got) == float(want)
    if isinstance(got, num) and isinstance(want, str):
        try:
            return float(got) == float(want)
        except ValueError:
            return None
    if isinstance(got, str) and isinstance(want, num):
        try:
            return float(got) == float(want)
        except ValueError:
            return None
    return None


def where_matches(model, t, row, where):
    """Does `row` satisfy every condition of a structured partial-index predicate? SQL semantics: a row is INCLUDED only
    when the predicate is TRUE; FALSE and UNKNOWN (NULL operand) exclude it. -> True / False / None (cannot be decided:
    unknown column, unsupported operator, or a value left to a non-literal DEFAULT)."""
    result = True
    for c in where:
        col = t["columns"].get(c.get("column"))
        op = c.get("operator")
        if col is None or op not in ("=", "<>", "IN", "IS NULL", "IS NOT NULL"):
            return None
        v = row.get(c["column"])
        if v is _ABSENT:
            v = col["default_value"]
            if v is None and col["default"] is not None and str(col["default"]).strip().upper() != "NULL":
                return None                                     # dynamic default (e.g. CURRENT_TIMESTAMP): unknown
        if op == "IS NULL":
            ok = v is None
        elif op == "IS NOT NULL":
            ok = v is not None
        elif v is None:
            ok = False                                          # NULL = x is UNKNOWN -> row not in the index
        elif op == "IN":
            res = [_cmp_equal(model, v, w) for w in c["value"]]
            ok = True if any(r is True for r in res) else (None if any(r is None for r in res) else False)
        else:
            eq = _cmp_equal(model, v, c["value"])
            ok = eq if eq is None else (eq if op == "=" else not eq)
        if ok is None:
            return None
        if not ok:
            result = False
    return result


# ---- NULL feasibility (three-valued logic; never infers "NOT NULL" from a CHECK that merely mentions IS NOT NULL)
def _code(expr):
    return "".join(x for lit, x in _split_literals(str(expr)) if not lit)


def _refs(t, expr):
    toks = {w.lower() for w in re.findall(r"[A-Za-z_]\w*", _code(expr).replace('"', " ").replace("`", " "))}
    return [c for c in t["columns"] if c.lower() in toks]


def _strip_parens(e):
    e = e.strip()
    while e.startswith("(") and e.endswith(")"):
        depth = 0
        for i, ch in enumerate(e):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and i < len(e) - 1:
                return e
        e = e[1:-1].strip()
    return e


def _conjuncts(expr):
    """Top-level AND terms of a CHECK expression; [whole] when the shape is not a plain conjunction (OR / BETWEEN at the
    top level). String literals are masked with same-length filler so quoted text never splits an expression."""
    e = _strip_parens(str(expr))
    masked = "".join("'" + "x" * (len(x) - 2) + "'" if lit else x for lit, x in _split_literals(e))
    if re.search(r"\bBETWEEN\b", masked, re.I):
        return [e]
    depth, cuts = 0, []
    for m in re.finditer(r"\(|\)|\bAND\b|\bOR\b", masked, re.I):
        w = m.group(0).upper()
        if w == "(":
            depth += 1
        elif w == ")":
            depth -= 1
        elif depth == 0 and w == "OR":
            return [e]
        elif depth == 0:
            cuts.append((m.start(), m.end()))
    if not cuts:
        return [e]
    parts, last = [], 0
    for st, en in cuts:
        parts.append(e[last:st])
        last = en
    parts.append(e[last:])
    out = []
    for part in parts:
        out += _conjuncts(part)
    return out


def _requires_value(expr, cn):
    pat = re.compile(r"^[\"`\[]?" + re.escape(cn) + r"[\"`\]]?\s+IS\s+NOT\s+NULL$", re.I)
    return any(pat.match(_strip_parens(c)) for c in _conjuncts(expr))


_ANY = object()                      # feasibility target: "some non-NULL value" (as opposed to NULL or one specific value)


def _literal(text):
    t = text.strip()
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return True, t[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return True, float(t) if "." in t else int(t)
    return False, None


def _excludes_value(model, expr, cn, value):
    """PROOF that no row can hold `value` in `cn` (value _ANY = any non-NULL value): some top-level AND term of the CHECK is
    `cn IS NULL`, `cn = <other literal>`, `cn <> <value>` or `cn IN (<literals without value>)`. A term that is FALSE or
    UNKNOWN-only for the value would not count, so only these exact shapes are used; anything else is not a proof."""
    ident = r"[\"`\[]?" + re.escape(cn) + r"[\"`\]]?"
    for c in _conjuncts(expr):
        c = _strip_parens(c)
        if re.fullmatch(ident + r"\s+IS\s+NULL", c, re.I):
            return True                                          # NULL is the only value that can satisfy this CHECK
        if value is _ANY:
            continue
        m = re.fullmatch(ident + r"\s*(=|<>|!=)\s*(.+)", c, re.I | re.S)
        if m:
            ok, lit = _literal(m.group(2))
            eq = _cmp_equal(model, value, lit) if ok else None
            if eq is not None and ((m.group(1) == "=" and not eq) or (m.group(1) != "=" and eq)):
                return True
            continue
        m = re.fullmatch(ident + r"\s+IN\s*\((.*)\)", c, re.I | re.S)
        if m:
            parts = [_literal(x) for x in re.findall(r"'(?:[^']|'')*'|-?\d+(?:\.\d+)?", m.group(1))]
            if parts and all(ok for ok, _ in parts) and re.fullmatch(r"[\s,]*", re.sub(r"'(?:[^']|'')*'|-?\d+(?:\.\d+)?", "", m.group(1))):
                res = [_cmp_equal(model, value, lit) for _, lit in parts]
                if all(r is False for r in res):
                    return True
    return False


def _candidates(t, cn, rows, expr_list, neutral=True):
    """Candidate values for one column of the witness search: values already used, allowed values, literals of the CHECKs
    (and their neighbours / a neutral alternative, so `<>` and ranges can be satisfied), NULL when the column is nullable."""
    c = t["columns"][cn]
    kind = c["info"]["kind"]
    numeric = kind in ("int", "float", "decimal")
    vals, seen = [], set()

    def add(v):
        if v is None or v is _ABSENT:
            return
        key = (type(v).__name__, str(v))
        if key not in seen:
            seen.add(key)
            vals.append(v)
    for r in rows:
        if len(vals) >= 5:
            break
        add(_value_for_check(c, r[cn]))
    for v in allowed_values_of(t, cn):
        add(float(v) if numeric and re.fullmatch(r"-?\d+(?:\.\d+)?", v) else v)
    for e in expr_list:
        for lit in _ENUM_VALUE.findall(e):
            add(lit.replace("''", "'"))
        for n in re.findall(r"(?<![\w'.])-?\d+(?:\.\d+)?(?![\w'])", _code(e)):
            num = float(n) if "." in n else int(n)
            add(num)
            add(num + 1)
            add(num - 1)
    if not neutral:                                             # values other columns of the same type hold (e.g. end >= start)
        for oc, ocol in t["columns"].items():
            if oc != cn and ocol["info"]["kind"] == kind:
                for r in rows[:5]:
                    add(_value_for_check(ocol, r[oc]))
    vals = vals[:10]
    if neutral:
        if kind == "string" or kind == "other":
            add("zz-other")                                     # neutral value different from every literal
        if numeric:
            add(0)
            add(1)
        if not c["not_null"]:
            vals.append(None)
    return vals or ([None] if neutral else [])


def allowed_values_of(t, cn):
    class _M:        # allowed_values() only needs the table
        pass
    return allowed_values(_M(), t).get(cn, [])


def null_feasibility(model, t, cn, rows, limit=6000):
    """Can a row of `t` hold NULL in `cn`? -> (state, why); see value_feasibility."""
    return value_feasibility(model, t, cn, rows, None, limit)


def value_feasibility(model, t, cn, rows, target, limit=6000):
    """Can a row of `t` hold `target` in `cn` without violating any CHECK of the table? target: None (NULL), _ANY (some
    non-NULL value) or one value. -> (state, why), state in
    "feasible" (a witness row was found by really evaluating every CHECK of the table, NULL-strict where schema.sql is),
    "infeasible" (PROVED from a top-level AND term of a CHECK: `cn IS NOT NULL` forbids NULL; `cn IS NULL`, `cn = other`,
    `cn <> value` or `cn IN (...)` forbid the non-NULL target) or
    "unknown" (no witness and no proof, or a CHECK cannot be evaluated here: reported as uncertain, never as a claim)."""
    cks = [ck for ck in t["checks"] if cn in _refs(t, ck["expression"])]
    if not cks:
        return "feasible", "no CHECK mentions the column"
    for ck in cks:
        if target is None and _requires_value(ck["expression"], cn):
            return "infeasible", f"CHECK `{ck['name']}` always requires `{cn}` IS NOT NULL"
        if target is not None and _excludes_value(model, ck["expression"], cn, target):
            what = "a non-NULL value" if target is _ANY else f"the value {target!r}"
            return "infeasible", f"CHECK `{ck['name']}` never allows {what} in `{cn}`"
    cols = list(t["columns"])
    sqls = []
    for ck in t["checks"]:                                       # every CHECK of the table must hold for the witness
        try:
            expr = render_check(ck["expression"], cols, model.dialect, f"{t['name']}: check {ck['name']}", table=t["name"])
        except DBAAgentError:
            return "unknown", f"CHECK `{ck['name']}` cannot be evaluated here"
        if is_null_aware(ck["expression"]):
            expr = strict_check(expr)
        sqls.append(f"SELECT ({expr}) FROM (SELECT " + ", ".join(f'? AS "{c}"' for c in cols) + ")")
    exprs = [ck["expression"] for ck in cks]
    others = sorted({c for ck in cks for c in _refs(t, ck["expression"])} - {cn})
    if target is None:
        own = [None]
    elif target is _ANY:
        own = _candidates(t, cn, rows, exprs, neutral=False)
    else:
        col_ = t["columns"][cn]
        numeric = col_["info"]["kind"] in ("int", "float", "decimal")
        own = [float(target) if numeric and isinstance(target, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", target) else target]
    doms = [_candidates(t, c, rows, exprs) for c in others]
    templates = [{c: _value_for_check(t["columns"][c], r[c]) for c in cols} for r in rows[:3]] or [{c: None for c in cols}]
    con = sqlite3.connect(":memory:")
    tried = 0
    try:
        for tpl in templates:
            for mine in own:
                for combo in itertools.product(*doms):
                    tried += 1
                    if tried > limit:
                        break
                    vals = dict(tpl)
                    vals.update(zip(others, combo))
                    vals[cn] = mine
                    params = [vals.get(c) for c in cols]
                    try:
                        outs = [con.execute(q, params).fetchone()[0] for q in sqls]
                    except sqlite3.Error:
                        return "unknown", "a CHECK of this table cannot be evaluated here"
                    if all(o is None or o for o in outs):
                        return "feasible", "a row satisfies every CHECK: " + ", ".join(f"{c}={vals[c]!r}" for c in others + [cn])
    finally:
        con.close()
    return "unknown", "no satisfying row found among the candidate values and no proof that none exists"


def _coverage(model, records):
    """-> (missing items, not applicable, uncertain). Items are dicts {kind, table, column, message}; `kind` is
    "null_example" (optional column without any NULL: repairable offline), "value" (column NULL everywhere),
    "status" (allowed value never used) or "link" (many-to-many link table)."""
    items, na, unc = [], [], []

    def add(kind, table, column, msg):
        items.append({"kind": kind, "table": table, "column": column, "message": msg})
    for name, t in model.tables.items():
        rows = records[name]
        n = len(rows)
        fk_cols = {c for f in t["fks"] for c in f["columns"]}
        for cn, c in t["columns"].items():
            if c["not_null"] or c["identity"] or n < 3:
                continue
            nulls = sum(1 for r in rows if r[cn] is None)
            if nulls == 0:
                state, why = null_feasibility(model, t, cn, rows)
                if state == "infeasible":
                    na.append(f"{name}.{cn}: optional in the schema but {why}, so NULL coverage is not applicable.")
                    continue
                if state == "unknown":
                    unc.append(f"{name}.{cn}: whether a NULL example is feasible could not be established ({why}); "
                               f"not required, and the DBMS enforces the constraint.")
                    continue
                what = "optional relationship" if cn in fk_cols else "optional column"
                add("null_example", name, cn,
                    f"{name}.{cn} is an {what} but every row has a value; include at least one row where it is NULL.")
            elif nulls == n:
                state, why = value_feasibility(model, t, cn, rows, _ANY)
                if state == "infeasible":
                    na.append(f"{name}.{cn}: NULL in every row and {why}, so a non-NULL example is not applicable.")
                elif state == "unknown":
                    unc.append(f"{name}.{cn}: whether a non-NULL example is feasible could not be established ({why}); "
                               f"not required, and the DBMS enforces the constraint.")
                else:
                    add("value", name, cn, f"{name}.{cn} is NULL in every row; include rows where it has a value.")
        for cn, vals in allowed_values(model, t).items():
            if len(vals) > 12:
                continue
            used = {str(r[cn]) for r in rows if r[cn] is not None and r[cn] is not _ABSENT}
            missing = [v for v in vals if v not in used]
            if missing and n >= 3:
                if len(vals) > n:
                    na.append(f"{name}.{cn}: {len(vals)} allowed values cannot all appear in {n} rows; use --rows {len(vals)} or more "
                              f"to show every value.")
                    continue
                feasible = []
                for v in missing:                               # a listed value is not automatically possible: other CHECKs may forbid it
                    state, why = value_feasibility(model, t, cn, rows, v)
                    if state == "feasible":
                        feasible.append(v)
                    elif state == "infeasible":
                        na.append(f"{name}.{cn}: value {v!r} is listed but {why}, so it cannot be shown (not applicable).")
                    else:
                        unc.append(f"{name}.{cn}: whether value {v!r} can occur could not be established ({why}); "
                                   f"not required, and the DBMS enforces the constraints.")
                if feasible:
                    add("status", name, cn, f"{name}.{cn}: allowed value(s) {feasible} are never used; show every status/value at least once.")
        if len(t["pk"]) > 1 and t["fks"] and all(c in fk_cols for c in t["pk"]) and n >= 3:
            for f in t["fks"]:
                counts = {}
                for r in rows:
                    k = _key(r, f["columns"])
                    counts[k] = counts.get(k, 0) + 1
                if counts and max(counts.values()) < 2:
                    others = [o for o in t["fks"] if o is not f]
                    partners = max((len(records.get(o["ref_table"], [])) for o in others), default=0)
                    if partners < 2:
                        na.append(f"{name} (many-to-many link table): the other side has fewer than 2 rows, so several partners per "
                                  f"`{f['ref_table']}` are impossible.")
                        continue
                    add("link", name, None, f"{name} (many-to-many link table): each `{f['ref_table']}` appears only once via "
                                            f"{f['columns']}; link some `{f['ref_table']}` rows to several partners.")
    return items, na, unc


def coverage_report(model, records):
    """-> (missing, not_applicable). `missing` = feasible coverage the data lacks (the LLM must fix it);
    `not_applicable` = requirements that cannot be met for this schema / row count (informational, never blocking).
    NULL examples whose feasibility is undecidable are neither: see coverage_uncertain()."""
    items, na, _ = _coverage(model, records)
    return [i["message"] for i in items], na


def coverage_uncertain(model, records):
    return _coverage(model, records)[2]


def semantic_findings(model, records):
    """Declared mandatory participation (design.conceptual relationship, min cardinality 1 on the 'many' side): every
    parent row should have a child row. Reported, never blocking (the data may legitimately be a sample)."""
    out = []
    for p in model.participation:
        child, parent = model.tables[p["child"]], model.tables[p["parent"]]
        f = next((x for x in child["fks"] if x["name"] == p["fk"]), None)
        if not f:
            continue
        used = {tuple(_norm(r[c], False) for c in f["columns"]) for r in records[p["child"]] if all(r[c] is not None for c in f["columns"])}
        lonely = [r for r in records[p["parent"]] if tuple(_norm(r[c], False) for c in f["ref_columns"]) not in used]
        if lonely:
            out.append(f"relationship `{p['relationship']}`: {len(lonely)} `{p['parent']}` row(s) have no `{p['child']}` row, "
                       f"but the design declares the participation mandatory.")
    return out


def repair_coverage(model, validated, max_attempts=300):
    """Offline, deterministic repair of NULL-example coverage. For each missing NULL example the single cell
    (table, column) of one existing row is set to NULL, trying the sparsest rows first; the dataset is then FULLY
    re-validated and the change is kept only if there are no errors, the target is covered and no new coverage gap or
    declared-participation gap appeared. Other coverage kinds (status values, link tables, NULL-everywhere) need
    different data and are left for the LLM. Returns a new Validated (with `.repairs`); `validated` is not modified."""
    if validated.errors or not validated.coverage_items:
        return validated
    data = deep_records(validated.records)
    current, repairs, tries = validated, [], 0
    for item in list(validated.coverage_items):
        if item["kind"] != "null_example":
            continue
        tn, cn = item["table"], item["column"]
        optional = [c for c, col in model.tables[tn]["columns"].items() if not col["not_null"] and c != cn]
        # Deterministic row order: the row whose OTHER optional columns are already mostly NULL first (the sparsest record is the
        # least likely to describe something the NULL would contradict), then the lowest row number.
        order = sorted(range(len(data[tn])), key=lambda k: (-sum(1 for c in optional if data[tn][k].get(c) is None), k))
        for i in order:
            if tries >= max_attempts:
                break
            tries += 1
            if data[tn][i].get(cn) is None and cn in data[tn][i]:
                continue
            trial = copy.deepcopy(data)
            old = trial[tn][i].get(cn, "<default>")
            trial[tn][i][cn] = None
            res = validate_records(model, {"tables": trial}, 1)
            if res.errors or item["message"] in res.coverage:
                continue
            if set(res.coverage) - set(current.coverage) or set(res.semantic_unmet) - set(current.semantic_unmet):
                continue                                        # would trade one gap for another
            data = trial
            repairs.append({"table": tn, "column": cn, "row": i + 1, "old_value": None if old == "<default>" else old,
                            "new_value": None,
                            "reason": "NULL example required by coverage; the CHECK/FK/UNIQUE constraints and every other "
                                      "coverage requirement still hold after the change",
                            "caveat": "relationships between this value and other columns of the same row that are not declared "
                                      "as constraints cannot be verified offline"})
            current = res
            break
    if not repairs:
        return validated
    current.repairs = list(validated.repairs) + repairs
    current.warnings = list(validated.warnings)
    return current


def coverage_findings(model, records):
    return coverage_report(model, records)[0]


def record_stats(model, records):
    """{table: {"rows", "nulls": {col: n}, "values": {col: [distinct used values of IN-list columns]}}}."""
    out = {}
    for name, t in model.tables.items():
        rows = records[name]
        nulls = {cn: sum(1 for r in rows if r[cn] is None) for cn, c in t["columns"].items() if not c["not_null"]}
        vals = {cn: sorted({str(r[cn]) for r in rows if r[cn] is not None}) for cn in allowed_values(model, t)}
        out[name] = {"rows": len(rows), "nulls": nulls, "values": vals}
    return out


# ------------------------------------------------------------------ ordering + rendering
def row_order(model, name, rows):
    """Indices of `rows` with self-referenced parents first (stable)."""
    t = model.tables[name]
    selfs = [f for f in t["fks"] if f["ref_table"] == name and (name, f["name"]) not in model.deferred]
    if not selfs:
        return list(range(len(rows)))
    ci = model.dialect in (MYSQL, SQL_SERVER)
    done, order = set(), []

    def visit(n, stack=()):
        if n in done or n in stack:
            return
        for f in selfs:
            vals = _key(rows[n], f["columns"])
            if all(v is not None for v in vals):
                want = tuple(_norm(v, ci) for v in vals)
                for m, r in enumerate(rows):
                    if m != n and tuple(_norm(r[c], ci) for c in f["ref_columns"]) == want:
                        visit(m, stack + (n,))
        done.add(n)
        order.append(n)
    for n in range(len(rows)):
        visit(n)
    return order


def literal(v, info, dialect):
    if v is None:
        return "NULL"
    k = info["kind"]
    if isinstance(v, bool):
        return ("TRUE" if v else "FALSE") if dialect == POSTGRESQL else ("1" if v else "0")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, decimal.Decimal):
        return format(v, "f")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise SeedError(f"non-finite number {v} cannot be written to SQL")
        return repr(v)
    s = str(v)
    if k == "timestamp" and dialect == SQL_SERVER:
        s = s.replace(" ", "T", 1)                              # ISO 8601 form is language-independent in SQL Server
    s = s.replace("'", "''")
    if dialect == MYSQL:
        s = s.replace("\\", "\\\\")
    prefix = "N" if dialect == SQL_SERVER and (info["national"] or not s.isascii()) else ""
    return f"{prefix}'{s}'"


def _begin(dialect):
    return {POSTGRESQL: "BEGIN;", MYSQL: "START TRANSACTION;", SQLITE: "BEGIN TRANSACTION;",
            SQL_SERVER: "BEGIN TRANSACTION;"}[dialect]


def render_seed_sql(model, records, fingerprint=None):
    """Validated records -> seed.sql text (deterministic). INSERT order: tables parents-first, rows parents-first."""
    d = model.dialect
    q = lambda n: quote(d, n)
    body, updates, total = [], [], 0
    for name in model.order:
        t, rows = model.tables[name], records[name]
        defer = {f["name"]: f for (tn, fn) in model.deferred if tn == name for f in t["fks"] if f["name"] == fn}
        deferred_cols = {c for f in defer.values() for c in f["columns"]}
        ident = [c for c in t["columns"].values() if c["identity"]]
        lines = [f"-- {name}: {len(rows)} row(s)"]
        if d == SQL_SERVER and ident:
            lines.append(f"SET IDENTITY_INSERT {q(name)} ON;")
        for i in row_order(model, name, rows):
            r = rows[i]
            cols = [c for c in t["columns"] if r[c] is not _ABSENT]
            vals = [literal(None if c in deferred_cols else r[c], t["columns"][c]["info"], d) for c in cols]
            lines.append(f"INSERT INTO {q(name)} ({', '.join(q(c) for c in cols)}) VALUES ({', '.join(vals)});")
            for f in defer.values():
                fv = _key(r, f["columns"])
                if any(v is not None for v in fv):
                    sets = ", ".join(f"{q(c)} = {literal(r[c], t['columns'][c]['info'], d)}" for c in f["columns"])
                    where = " AND ".join(f"{q(c)} = {literal(r[c], t['columns'][c]['info'], d)}" for c in t["pk"])
                    updates.append(f"UPDATE {q(name)} SET {sets} WHERE {where};")
            total += 1
        if d == SQL_SERVER and ident:
            lines.append(f"SET IDENTITY_INSERT {q(name)} OFF;")
        if d == POSTGRESQL and ident:
            c = ident[0]["name"]
            lines.append(f"SELECT setval(pg_get_serial_sequence('{q(name)}', '{c}'), (SELECT MAX({q(c)}) FROM {q(name)}));")
        body.append("\n".join(lines))
    head = [f"-- seed.sql - {' '.join(str(model.project).split())} ({DIALECT_NAMES[d]})",
            "-- Generated deterministically by DBA Agent (Stage 6B) from validated sample records; do not edit by hand.",
            f"-- Approved design fingerprint (SHA-256): {fingerprint or 'not recorded'}",
            f"-- Run AFTER schema.sql, on the empty database it created. {total} row(s) in {len(model.order)} table(s).",
            "-- Parents are inserted before children, so all foreign keys hold while loading."]
    if model.deferred:
        head.append("-- Circular foreign key(s) " + ", ".join(f"{t}.{f}" for t, f in model.deferred) +
                    " are loaded as NULL and set by UPDATE at the end.")
    parts = ["\n".join(head)]
    if d == SQLITE:
        parts.append("PRAGMA foreign_keys = ON;")
    parts.append(_begin(d))
    parts += body
    if updates:
        parts.append("-- circular foreign keys\n" + "\n".join(updates))
    parts.append("COMMIT;")
    return "\n\n".join(parts) + "\n"


# ------------------------------------------------------------------ prompt description
def describe_schema(model):
    """Plain-text description of every table (in insertion order) for the LLM prompt."""
    lines = []
    for name in model.order:
        t = model.tables[name]
        fk_of = {c: f for f in t["fks"] for c in f["columns"]}
        lines.append(f"Table {name}" + (f" - {t['description']}" if t["description"] else ""))
        for cn, c in t["columns"].items():
            bits = [c["type"]]
            if c["identity"]:
                bits.append("AUTO-GENERATED identity (give explicit ids 1,2,3,... in row order)")
            if c["in_pk"]:
                bits.append("PRIMARY KEY")
            bits.append("NOT NULL" if c["not_null"] else "optional (NULL allowed)")
            if c["default"] is not None:
                bits.append(f"DEFAULT {c['default']} (may be omitted)")
            if cn in fk_of:
                f = fk_of[cn]
                bits.append(f"FOREIGN KEY -> {f['ref_table']}({', '.join(f['ref_columns'])})")
            lines.append(f"  - {cn}: " + ", ".join(bits))
        for u in t["uniques"]:
            if u.get("where"):
                lines.append(f"  UNIQUE ({', '.join(u['columns'])}) ONLY AMONG ROWS WHERE {_where_text(u['where'])} "
                             f"(partial index: the same value may repeat in rows that do not satisfy the condition)")
            else:
                lines.append(f"  UNIQUE ({', '.join(u['columns'])})")
        for ck in t["checks"]:
            lines.append(f"  CHECK {ck['name']}: {ck['expression']}")
        for cn, vals in allowed_values(model, t).items():
            lines.append(f"  allowed values of {cn}: {', '.join(vals)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def deep_records(records):
    """JSON-safe copy of validated records (seed-data.json). Columns left to a DEFAULT are omitted."""
    return {name: [{c: (format(v, "f") if isinstance(v, decimal.Decimal) else v)
                    for c, v in r.items() if v is not _ABSENT} for r in rows] for name, rows in records.items()}
