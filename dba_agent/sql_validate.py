"""Stage 6B - validation of schema.sql + seed.sql.

Levels (never mixed up):
  "executed"   - both scripts were really run on the chosen DBMS and all checks passed. SQLite: in-memory database, run
                 here. PostgreSQL / MySQL-MariaDB / SQL Server: only if the config has a `validation` entry, the driver
                 is installed and an EMPTY scratch database is reachable.
  "failed"     - the scripts were run and something is wrong (actionable errors are listed).
  "unverified" - the chosen DBMS was NOT available; only static, dialect-independent checks ran. SQLite is never used
                 as a substitute for another DBMS.
Report: {"dbms", "level", "ok", "completes_stage", "executed_on", "checks": [{name, ok, detail}], "errors", "warnings",
         "row_counts", "reason"}   ("ok" = no error; "completes_stage" = Stage 6 may be marked complete)

Truthful-reporting fields (Batch 5C; always present, kept apart from `completes_stage`, which only says that the SQL
files were generated and nothing is wrong - it never means "verified"):
  "selected_dbms"       DBMS the design/Stage 3 chose (== "dbms").
  "executed_dbms"       DBMS that really ran the scripts, else None. Always equal to selected_dbms when set - another
                        DBMS is never substituted.
  "static_validation"   "passed" | "failed"  (dialect-independent structural checks on seed.sql).
  "execution"           "executed_ok" | "execution_failed" | "unavailable" (no server/driver/connection/empty scratch DB)
                        | "not_attempted" (static checks failed first).
  "unavailable_kind"    why execution was unavailable: no_server_configured | driver_missing | config_incomplete |
                        connection_failed | scratch_db_not_empty | server_check_failed  (else None).
  "verified"            True only when execution == "executed_ok" on the selected DBMS.
  "summary"             one honest sentence for the CLI.
"""
import re
import sqlite3

from . import seed_data as sd
from .llm import redact
from .schema_sql import DIALECT_NAMES, SQLITE, quote

DRIVERS = {"postgresql": "pip install 'psycopg[binary]'  (or psycopg2)", "mysql_mariadb": "pip install pymysql",
           "sql_server": "pip install pyodbc (+ an ODBC driver for SQL Server)"}


_TRIGGER_START = re.compile(r"^CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\b", re.I)
_QUOTED = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]""")
_BLOCK_WORD = re.compile(r"\b(BEGIN|CASE|END)\b", re.I)


def _inside_trigger_body(stmt):
    """True while a CREATE TRIGGER statement still has an open BEGIN / CASE (its END has not been read yet)."""
    if not _TRIGGER_START.match(stmt):
        return False
    depth = 0
    for w in _BLOCK_WORD.findall(_QUOTED.sub("''", stmt)):
        depth += -1 if w.upper() == "END" else 1
    return depth > 0


def split_statements(sql):
    """Split a script into statements at ';' outside quotes/comments (handles '..' \"..\" `..` [..] and -- comments).
    A CREATE TRIGGER statement is kept whole up to its closing END (its body contains ';')."""
    out, buf, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch in "'\"`[":
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i:j + 1])
            i = j + 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if _inside_trigger_body(stmt):               # ';' inside BEGIN ... END of a trigger does not end it
                buf.append(ch)
                i += 1
                continue
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _report(dbms):
    return {"dbms": dbms, "level": "unverified", "ok": True, "completes_stage": False, "executed_on": None,
            "checks": [], "errors": [], "warnings": [], "row_counts": {}, "reason": ""}


def _safe(text, settings=None):
    """Remove database credentials (configured password, `password=`/`pwd=` pairs, user:pw@ URLs) from driver text."""
    secrets = []
    if isinstance(settings, dict):
        secrets = [str(v) for k, v in settings.items() if str(k).lower() in ("password", "passwd", "pwd") and v]
    text = redact(text, *secrets)
    text = re.sub(r"(?i)\b(password|passwd|pwd)\s*=\s*[^;&\s]+", r"\1=[REDACTED]", text)
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1[REDACTED]@", text)


def _annotate(rep):
    """Fill the truthful-reporting fields (see module docstring) from the finished report."""
    level, d = rep["level"], rep["dbms"]
    name = DIALECT_NAMES.get(d, d)
    rep["selected_dbms"] = d
    rep.setdefault("static_validation", "failed" if rep["errors"] and not rep.get("executed_on") else "passed")
    rep.setdefault("unavailable_kind", None)
    ran = bool(rep.get("executed_on"))
    rep["executed_dbms"] = d if ran else None
    if level == "executed":
        rep["execution"] = "executed_ok"
    elif level == "failed" and ran:
        rep["execution"] = "execution_failed"
    elif level == "unverified" and rep["unavailable_kind"]:
        rep["execution"] = "unavailable"
    else:
        rep["execution"] = "not_attempted"
    rep["verified"] = rep["execution"] == "executed_ok"
    gen = "SQL generated"
    stat = f"static checks {rep['static_validation']}"
    ex = {"executed_ok": f"executed successfully on {name}",
          "execution_failed": f"execution on {name} FAILED",
          "unavailable": f"NOT executed on {name} (execution unavailable: {rep['unavailable_kind'].replace('_', ' ') if rep['unavailable_kind'] else ''})",
          "not_attempted": f"NOT executed on {name} (not attempted)"}[rep["execution"]]
    rep["summary"] = f"{gen}; {stat}; {ex}."
    return rep


def _check(rep, name, ok, detail=""):
    rep["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        rep["errors"].append(f"{name}: {detail}")


# ------------------------------------------------------------------ static checks (all dialects)
_INSERT = re.compile(r'^INSERT INTO\s+(?:"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|\[((?:[^\]]|\]\])+)\])', re.I)


def static_checks(model, records, seed_sql, rep):
    stmts = split_statements(seed_sql)
    inserts = []
    for s in stmts:
        m = _INSERT.match(s)
        if m:
            inserts.append(next(g for g in m.groups() if g is not None).replace('""', '"').replace("``", "`").replace("]]", "]"))
    expected = sum(len(r) for r in records.values())
    _check(rep, "seed.sql has one INSERT per sample row", len(inserts) == expected,
           f"{len(inserts)} INSERT statement(s) for {expected} row(s)")
    missing = [t for t in model.tables if t not in inserts]
    _check(rep, "every table is populated in seed.sql", not missing, "no INSERT for: " + ", ".join(missing))
    first = {}
    for i, t in enumerate(inserts):
        first.setdefault(t, i)
    bad = []
    for name, t in model.tables.items():
        for f in t["fks"]:
            if f["ref_table"] != name and (name, f["name"]) not in model.deferred and name in first and \
                    f["ref_table"] in first and first[f["ref_table"]] > first[name]:
                bad.append(f"{name} before {f['ref_table']}")
    _check(rep, "parent tables are inserted before child tables", not bad, "; ".join(bad))
    rep["static_validation"] = "failed" if rep["errors"] else "passed"
    return stmts


# ------------------------------------------------------------------ SQLite (really executed)
def _run_sqlite(con, text, what):
    for i, stmt in enumerate(split_statements(text), 1):
        try:
            con.execute(stmt)
        except sqlite3.Error as e:
            return f"{what} statement {i} failed: {e}\n    {' '.join(stmt.split())[:220]}"
    return None


def db_stats(con, model):
    out = {}
    for name, t in model.tables.items():
        q = quote(SQLITE, name)
        rows = con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
        nulls = {c: con.execute(f"SELECT COUNT(*) FROM {q} WHERE {quote(SQLITE, c)} IS NULL").fetchone()[0]
                 for cn, c in ((cn, cn) for cn, col in t["columns"].items() if not col["not_null"])}
        vals = {c: sorted(str(r[0]) for r in con.execute(f"SELECT DISTINCT {quote(SQLITE, c)} FROM {q} WHERE "
                                                         f"{quote(SQLITE, c)} IS NOT NULL")) for c in sd.allowed_values(model, t)}
        out[name] = {"rows": rows, "nulls": nulls, "values": vals}
    return out


def _validate_sqlite(model, records, schema_sql, seed_sql):
    rep = _report("sqlite")
    rep["executed_on"] = f"SQLite {sqlite3.sqlite_version} (fresh in-memory database, foreign_keys=ON)"
    static_checks(model, records, seed_sql, rep)
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        e = _run_sqlite(con, schema_sql, "schema.sql")
        _check(rep, "schema.sql executes without errors", e is None, e or "")
        if e:
            return _finish(rep, executed=True)
        e = _run_sqlite(con, seed_sql, "seed.sql")
        _check(rep, "seed.sql executes without errors after schema.sql", e is None, e or "")
        if e:
            return _finish(rep, executed=True)
        _check(rep, "foreign-key enforcement was ON", con.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
               "PRAGMA foreign_keys is 0")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        absent = [t for t in model.tables if t not in tables]
        _check(rep, "every design table exists", not absent, "missing: " + ", ".join(absent))
        if absent:
            return _finish(rep, executed=True)
        got = db_stats(con, model)
        want = sd.record_stats(model, records)
        rep["row_counts"] = {t: s["rows"] for t, s in got.items()}
        empty = [t for t, s in got.items() if s["rows"] == 0]
        _check(rep, "every table has records", not empty, "empty: " + ", ".join(empty))
        fkv = con.execute("PRAGMA foreign_key_check").fetchall()
        _check(rep, "PRAGMA foreign_key_check reports no violations", not fkv,
               "; ".join(f"{r[0]} row {r[1]} -> {r[2]}" for r in fkv[:5]))
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        _check(rep, "integrity_check is ok", integ == "ok", integ)
        diff = [f"{t}: database has {got[t]['rows']} rows, expected {want[t]['rows']}" for t in want if got[t]["rows"] != want[t]["rows"]]
        _check(rep, "row counts match the validated sample data", not diff, "; ".join(diff))
        diff = [f"{t}.{c}: {got[t]['nulls'].get(c)} NULL(s) in the database, expected {n}"
                for t in want for c, n in want[t]["nulls"].items() if got[t]["nulls"].get(c) != n]
        _check(rep, "optional relationships/columns (NULLs) match the sample data", not diff, "; ".join(diff))
        diff = [f"{t}.{c}: database has {got[t]['values'].get(c)}, expected {v}"
                for t in want for c, v in want[t]["values"].items() if got[t]["values"].get(c) != v]
        _check(rep, "status/category values match the sample data", not diff, "; ".join(diff))
        return _finish(rep, executed=True)
    finally:
        con.close()


def _finish(rep, executed):
    rep["ok"] = not rep["errors"]
    rep["level"] = ("executed" if rep["ok"] else "failed") if executed else "unverified"
    rep["completes_stage"] = rep["ok"]
    return rep


def validate_sqlite(model, records, schema_sql, seed_sql):
    return _annotate(_validate_sqlite(model, records, schema_sql, seed_sql))


# ------------------------------------------------------------------ other DBMSs (server needed)
def _connect(dialect, s):
    """-> DB-API connection in autocommit mode. Raises ImportError (driver) or Exception (connection)."""
    if dialect == "postgresql":
        try:
            import psycopg
            return psycopg.connect(s["dsn"], autocommit=True)
        except ImportError:
            import psycopg2
            c = psycopg2.connect(s["dsn"])
            c.autocommit = True
            return c
    if dialect == "mysql_mariadb":
        import pymysql
        return pymysql.connect(host=s.get("host", "localhost"), port=int(s.get("port", 3306)), user=s["user"],
                               password=s.get("password", ""), database=s["database"], autocommit=True)
    import pyodbc
    return pyodbc.connect(s["connection_string"], autocommit=True)


_COUNT_TABLES = {"postgresql": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
                 "mysql_mariadb": "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'",
                 "sql_server": "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"}


def _validate_on_server(model, records, schema_sql, seed_sql, settings=None, connect=None):
    """Run both scripts on a real server if configured. `connect(dialect, settings)` is injectable for tests.
    The target database must be EMPTY and is left populated afterwards (use a throw-away database/container)."""
    d = model.dialect
    rep = _report(d)
    static_checks(model, records, seed_sql, rep)
    rep["ok"] = not rep["errors"]
    if rep["errors"]:
        rep["level"], rep["completes_stage"] = "failed", False
        return rep
    name = DIALECT_NAMES[d]

    def unavailable(kind, text):
        rep["unavailable_kind"] = kind
        rep["reason"] = f"UNVERIFIED: {text} The SQL was NOT executed on {name}; only static checks ran."
        rep["completes_stage"] = True                  # generation is complete; execution is simply unverified
        return rep

    if not settings:
        return unavailable("no_server_configured", f"no {name} server is configured (config key `validation.{d}`).")
    try:
        con = (connect or _connect)(d, settings)
    except ImportError as e:
        return unavailable("driver_missing", f"{name} driver not installed ({_safe(e, settings)}). {DRIVERS[d]}.")
    except KeyError as e:
        return unavailable("config_incomplete", f"config `validation.{d}` lacks the required setting {e}.")
    except Exception as e:                                       # connection problems are reported, never hidden
        return unavailable("connection_failed", f"cannot connect to {name} ({type(e).__name__}: {_safe(e, settings)}).")
    try:
        try:
            cur = con.cursor()
            cur.execute(_COUNT_TABLES[d])
            existing = cur.fetchone()[0]
        except Exception as e:                                   # connected, but the server cannot even be inspected
            return unavailable("server_check_failed", f"the {name} server could not be inspected ({type(e).__name__}: {_safe(e, settings)}).")
        if existing:
            return unavailable("scratch_db_not_empty", f"the configured {name} database is not empty; refusing to run the "
                               "scripts there (use a fresh scratch database).")
        rep["executed_on"] = f"{name} server (configured scratch database)"
        for label, text in (("schema.sql", schema_sql), ("seed.sql", seed_sql)):
            for i, stmt in enumerate(split_statements(text), 1):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    _check(rep, f"{label} executes without errors", False,
                           f"statement {i} failed: {_safe(e, settings)}\n    {' '.join(stmt.split())[:220]}")
                    return _finish(rep, executed=True)
            _check(rep, f"{label} executes without errors", True)
        counts = {}
        try:
            for t in model.tables:
                cur.execute(f"SELECT COUNT(*) FROM {quote(d, t)}")
                counts[t] = cur.fetchone()[0]
        except Exception as e:
            _check(rep, "tables can be read back after the scripts ran", False, _safe(e, settings))
            return _finish(rep, executed=True)
        rep["row_counts"] = counts
        diff = [f"{t}: {counts[t]} rows, expected {len(records[t])}" for t in records if counts[t] != len(records[t])]
        _check(rep, "every table has the expected number of records", not diff, "; ".join(diff))
        return _finish(rep, executed=True)
    finally:
        try:
            con.close()
        except Exception:
            pass


def validate_on_server(model, records, schema_sql, seed_sql, settings=None, connect=None):
    return _annotate(_validate_on_server(model, records, schema_sql, seed_sql, settings, connect))


def validate(model, records, schema_sql, seed_sql, settings=None, connect=None):
    if model.dialect == SQLITE:
        return validate_sqlite(model, records, schema_sql, seed_sql)
    return validate_on_server(model, records, schema_sql, seed_sql, (settings or {}).get(model.dialect), connect)
