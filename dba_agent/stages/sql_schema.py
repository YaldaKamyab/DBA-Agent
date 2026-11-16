"""Stage 6A - schema.sql generation for an APPROVED design (seed.sql is Stage 6B and does not exist yet).

generate(session) -> verifies the approval (approval.verify_approved_design: fingerprint, design.json, all documents,
DBMS choice, pending revision), generates schema.sql deterministically from the verified design
(schema_sql.generate_schema_sql), writes it atomically and records which approved design it belongs to:

state["sql_stage"]["schema"] = {status: generating|generated, file, design_fingerprint, sha256, dbms_id, ...}

Validity is always *recomputed* by schema_status(): schema.sql is "current" only if the approval is still valid, its
fingerprint equals the recorded one and the file still has the recorded SHA-256. A later revision therefore makes the SQL
stale automatically, and an interrupted run (status `generating`, or a file without a matching record) is simply
regenerated - generation is deterministic, so re-running is always safe.
Stage 6 stays incomplete until Stage 6B (stages/sql_seed.py) has generated and validated seed.sql.
"""
import hashlib
import os

from .. import approval
from ..errors import DBAAgentError
from ..schema_sql import generate_schema_sql
from ..state import now_iso

SCHEMA_FILE = "schema.sql"


class SchemaStateError(DBAAgentError):
    """schema.sql is missing, stale or modified (used by Stage 6B before it builds on schema.sql)."""


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def schema_status(session):
    """-> {"state": missing|incomplete|missing_file|modified|stale|current, "detail": str}. Read-only."""
    rec = (session.data.get("sql_stage") or {}).get("schema")
    if not rec:
        return {"state": "missing", "detail": "schema.sql has not been generated."}
    if rec.get("status") != "generated":
        return {"state": "incomplete", "detail": "A previous generation was interrupted; run `schema` again."}
    text = _read(session.output_dir / SCHEMA_FILE)
    if text is None:
        return {"state": "missing_file", "detail": f"{SCHEMA_FILE} is missing; run `schema` again."}
    if _sha(text) != rec.get("sha256"):
        return {"state": "modified", "detail": f"{SCHEMA_FILE} was modified after it was generated; run `schema --force` "
                                               f"to regenerate it from the approved design."}
    rep = approval.integrity_report(session)
    if rep["blocking"]:
        return {"state": "stale", "detail": "The design is not (or no longer) approved: " + rep["blocking"][0]}
    if session.data["approval"]["design_fingerprint"] != rec.get("design_fingerprint"):
        return {"state": "stale", "detail": f"{SCHEMA_FILE} belongs to an older design; run `schema` to regenerate it."}
    if rep["revalidation"]:
        return {"state": "legacy", "detail": legacy_detail(rep["revalidation"], SCHEMA_FILE)}
    return {"state": "current", "detail": "schema.sql matches the approved design."}


def legacy_detail(findings, what):
    """Historical approval: files verified unchanged, but NOT validated against the current rules."""
    return (f"the approval and {what} are unchanged since approval (integrity verified), but the approved design does not "
            f"pass the current validation rules, so it is not treated as validated and {SCHEMA_FILE} will not be "
            f"regenerated from it. Revalidate with `revise` + `approve`. First finding: {findings[0]}")


def require_current_schema(session, allow_legacy=False):
    """For Stage 6B: schema.sql record, or SchemaStateError if the file is not valid for the approved design.
    allow_legacy=True also accepts the recorded, hash-verified schema.sql of an intact historical approval."""
    st = schema_status(session)
    if st["state"] != "current" and not (allow_legacy and st["state"] == "legacy"):
        raise SchemaStateError(f"schema.sql cannot be used: {st['detail']}")
    return session.data["sql_stage"]["schema"]


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def generate(session, force=False):
    """Generate (or confirm) schema.sql. Returns {"changed": bool, "record": {...}, "notes": [...]}.
    Raises ApprovalError (design not approved / tampered) or SchemaGenerationError (design cannot be translated);
    in both cases nothing is written and the approved design is untouched."""
    ap, design = approval.verify_approved_design(session)         # read-only; never repairs anything
    fp = ap["design_fingerprint"]
    result = generate_schema_sql(design, fp)
    digest = _sha(result.sql)
    d = session.data
    stage = d.setdefault("sql_stage", {})
    path = session.output_dir / SCHEMA_FILE
    if not force and schema_status(session)["state"] == "current" and _read(path) == result.sql:
        return {"changed": False, "record": stage["schema"], "notes": result.notes}
    stage["schema"] = {"status": "generating", "design_fingerprint": fp, "dbms_id": result.dialect,
                       "started_at": now_iso()}
    session.save()                                               # a crash after this point is recoverable: just rerun
    _atomic_write(path, result.sql)
    if _sha(_read(path) or "") != digest:
        raise DBAAgentError(f"{SCHEMA_FILE} could not be written correctly; run `schema` again.")
    rec = {"status": "generated", "file": SCHEMA_FILE, "design_fingerprint": fp, "sha256": digest,
           "dbms_id": result.dialect, "generated_at": now_iso(), "tables": len(result.table_order),
           "table_order": result.table_order, "deferred_foreign_keys": result.deferred_foreign_keys,
           "notes": result.notes, "rule_enforcement": result.enforcement, "executed_against_dbms": False}
    stage["schema"] = rec
    stage["status"] = "in_progress"
    stage["note"] = "Stage 6 is incomplete: run `seed` to generate and validate seed.sql."
    if "sql" in d.get("completed_stages", []):           # a new schema.sql makes any earlier seed.sql stale
        d["completed_stages"].remove("sql")
        d["current_stage"] = "sql"
    files = d.setdefault("output_files", [])
    if SCHEMA_FILE not in files:
        files.append(SCHEMA_FILE)
    d["last_error"] = None
    session.save()
    return {"changed": True, "record": rec, "notes": result.notes}
