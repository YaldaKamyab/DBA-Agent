"""Foreign-key data-type compatibility for the four supported DBMSs.

`fk_type_problem(src_type, ref_type, dbms_id)` returns None when a foreign-key
column may reference the given column, otherwise a short human-readable reason.

Policy (deliberately conservative - we never guess):
  * Type names are parsed into a canonical form, so real aliases match:
    INT = INTEGER = INT4 = SERIAL, BIGINT = INT8 = BIGSERIAL, VARCHAR(n) =
    CHARACTER VARYING(n), NUMERIC(p,s) = DECIMAL(p,s), BOOL = BOOLEAN ...
  * Integer widths must be identical (even where a DBMS could convert
    implicitly) and, for MySQL, so must signedness. Decimal precision/scale must
    be identical. String lengths may differ.
  * SQLite has dynamic typing: types are compatible when they are the same
    declared type or share a clear INTEGER / TEXT / REAL / BLOB affinity.
  * A type we do not recognise is accepted only if both columns use exactly the
    same type text; otherwise it is reported as "cannot verify".
"""
import re

_INT_BITS = {"tinyint": 8, "smallint": 16, "int2": 16, "smallserial": 16, "serial2": 16,
             "mediumint": 24, "int": 32, "integer": 32, "int4": 32, "serial": 32, "serial4": 32,
             "bigint": 64, "int8": 64, "bigserial": 64, "serial8": 64}
_DECIMAL = {"decimal", "numeric", "dec", "number", "fixed"}
_VARCHAR = {"varchar", "character varying", "varchar2", "nvarchar", "nvarchar2", "string"}
_CHAR = {"char", "character", "bpchar", "nchar"}
_TEXT = {"text", "clob", "ntext", "longtext", "mediumtext", "tinytext", "nclob"}
_NATIONAL = {"nvarchar", "nvarchar2", "nchar", "ntext", "nclob"}
_BINARY = {"bytea", "blob", "varbinary", "binary", "bytes", "longblob", "mediumblob", "tinyblob", "image"}
_DATES = {"date": ("date",), "time": ("time",), "time without time zone": ("time",),
          "timetz": ("timetz",), "time with time zone": ("timetz",),
          "timestamp": ("timestamp",), "timestamp without time zone": ("timestamp",),
          "timestamptz": ("timestamptz",), "timestamp with time zone": ("timestamptz",),
          "datetimeoffset": ("timestamptz",), "datetime": ("datetime", "datetime"),
          "datetime2": ("datetime", "datetime2"), "smalldatetime": ("datetime", "smalldatetime")}


def parse_type(data_type):
    """'Character Varying(20)' -> ('character varying', ['20'], set())."""
    s = " ".join(str(data_type or "").lower().split())
    m = re.search(r"\(([^)]*)\)", s)
    params = [p.strip() for p in m.group(1).split(",")] if m else []
    tokens = re.sub(r"\([^)]*\)", " ", s).split()
    flags = {t for t in tokens if t in ("unsigned", "signed", "zerofill")}
    return " ".join(t for t in tokens if t not in flags), params, flags


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def canonical(data_type, dbms_id=None):
    """Canonical tuple for a (non-SQLite) type, or None when not recognised."""
    base, params, flags = parse_type(data_type)
    national = base in _NATIONAL and dbms_id == "sql_server"
    if base in _INT_BITS:
        return ("int", _INT_BITS[base], "unsigned" in flags and dbms_id == "mysql_mariadb")
    if base in ("boolean", "bool"):
        return ("int", 8, False) if dbms_id == "mysql_mariadb" else ("bool",)   # MySQL: alias of TINYINT(1)
    if base == "bit" and dbms_id == "sql_server":
        return ("bool",)
    if base in _DECIMAL:
        p = _int(params[0]) if params else None
        s = (_int(params[1]) if len(params) > 1 else 0) if params else None
        return ("decimal", p, s)
    if base in ("real", "float4"):
        return ("float", 32)
    if base in ("double precision", "double", "float8"):
        return ("float", 64)
    if base == "float":
        p = _int(params[0]) if params else None
        if dbms_id == "mysql_mariadb":
            return ("float", 64 if p and p > 24 else 32)
        return ("float", 32 if p and p <= 24 else 64)
    if base in _VARCHAR:
        return ("varchar", national)
    if base in _CHAR:
        return ("char", national)
    if base in _TEXT:
        return ("text", national)
    if base in _DATES:
        return _DATES[base]
    if base in ("uuid", "uniqueidentifier"):
        return ("uuid",)
    if base in _BINARY:
        return ("binary", base)
    return None


def _sqlite_affinity(base):
    if "int" in base:
        return "INTEGER"
    if any(x in base for x in ("char", "clob", "text")):
        return "TEXT"
    if "blob" in base or not base:
        return "BLOB"
    if any(x in base for x in ("real", "floa", "doub")):
        return "REAL"
    return "NUMERIC"


def _describe(c):
    if c is None:
        return "unknown type"
    if c[0] == "int":
        return f"{c[1]}-bit integer" + (" unsigned" if c[2] else "")
    if c[0] == "decimal":
        return "decimal" if c[1] is None else f"decimal({c[1]},{c[2]})"
    if c[0] == "float":
        return f"{c[1]}-bit float"
    return " ".join(str(x) for x in c if x not in (False, None))


def fk_type_problem(src_type, ref_type, dbms_id=None):
    """None if `src_type` may reference `ref_type`, else the reason why not."""
    sb, rb = parse_type(src_type)[0], parse_type(ref_type)[0]
    sp, rp = parse_type(src_type)[1], parse_type(ref_type)[1]
    hint = "use the same type on both columns"
    if dbms_id == "sqlite":
        if (sb, sp) == (rb, rp) or sb == rb:
            return None
        a, b = _sqlite_affinity(sb), _sqlite_affinity(rb)
        if a == b and a != "NUMERIC":
            return None
        return f"'{src_type}' ({a} affinity) is not compatible with '{ref_type}' ({b} affinity) in SQLite; {hint}."
    a, b = canonical(src_type, dbms_id), canonical(ref_type, dbms_id)
    if a is None or b is None:
        if " ".join(str(src_type).lower().split()) == " ".join(str(ref_type).lower().split()):
            return None                                   # identical custom/unknown type on both sides
        return (f"cannot verify that '{src_type}' is compatible with '{ref_type}' (unrecognised type); "
                f"{hint} (identical type text).")
    if a == b:
        return None
    if dbms_id == "postgresql" and {a[0], b[0]} == {"varchar", "text"}:
        return None                                       # PostgreSQL treats varchar and text as interchangeable
    return (f"'{src_type}' ({_describe(a)}) is not compatible with the referenced column type "
            f"'{ref_type}' ({_describe(b)}); {hint}.")
