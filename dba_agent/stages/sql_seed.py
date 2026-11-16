"""Stage 6B - seed.sql generation + validation + Stage 6 completion.

generate(session, llm, prompts, ...) does, in this order (nothing is written before the checks pass):
  1. verify the approval (approval.verify_approved_design) and that schema.sql is current for it (SchemaStateError otherwise)
  2. build the seed model from design.json (SeedError if the schema cannot be seeded safely - before any LLM call)
  3. get sample records: reuse output/seed-data.json if it belongs to this design + schema and still validates
     (interrupted run / --regenerate), otherwise ask the LLM (stage "seed", every attempt logged, invalid answers are
     retried with the validation errors as feedback), then save the validated records
  4. render seed.sql deterministically, write it atomically, record it, validate it (sql_validate) and record the result
  5. mark Stage 6 complete only if validation passed at the available level (SQLite: really executed)

state["sql_stage"]["seed"] = {status: generating|generated, design_fingerprint, schema_sha256, sha256, data_sha256,
                              validation: {...report...}, ...}
seed_status() recomputes validity every time (missing / incomplete / missing_file / modified / stale / failed_validation /
coverage_incomplete / current). `coverage_incomplete` means the SQL executed and every check passed but feasible sample-data
coverage is still missing - it is never reported as an SQL failure.

A missing NULL example is repaired OFFLINE (no LLM call) when one existing row can be changed and the whole dataset still
validates (seed_data.repair_coverage); every repair is recorded in seed-data.json, state.json and the CLI output.
"""
import hashlib
import json
import os

from .. import approval, seed_data as sd, sql_validate
from ..errors import DBAAgentError, InvalidResponseError
from ..state import now_iso
from . import sql_schema

SEED_FILE = "seed.sql"
DATA_FILE = "seed-data.json"
DEFAULT_ROWS = 8


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def seed_status(session):
    """-> {"state": missing|incomplete|missing_file|modified|stale|failed_validation|current, "detail": str}. Read-only."""
    rec = (session.data.get("sql_stage") or {}).get("seed")
    if not rec:
        return {"state": "missing", "detail": "seed.sql has not been generated."}
    if rec.get("status") != "generated":
        return {"state": "incomplete", "detail": "A previous generation was interrupted; run `seed` again "
                                                 "(saved sample data is reused when it is still valid)."}
    text = _read(session.output_dir / SEED_FILE)
    if text is None:
        return {"state": "missing_file", "detail": f"{SEED_FILE} is missing; run `seed` again."}
    if _sha(text) != rec.get("sha256"):
        return {"state": "modified", "detail": f"{SEED_FILE} was modified after it was generated; run `seed --force` "
                                               f"to regenerate it."}
    rep = approval.integrity_report(session)
    if rep["blocking"]:
        return {"state": "stale", "detail": "The design is not (or no longer) approved: " + rep["blocking"][0]}
    legacy = rep["revalidation"]
    sch = sql_schema.schema_status(session)
    if sch["state"] not in ("current", "legacy"):
        return {"state": "stale", "detail": f"schema.sql is {sch['state']} ({sch['detail']}), so {SEED_FILE} cannot be trusted."}
    if session.data["approval"]["design_fingerprint"] != rec.get("design_fingerprint"):
        return {"state": "stale", "detail": f"{SEED_FILE} belongs to an older design; run `seed` to regenerate it."}
    if session.data["sql_stage"]["schema"].get("sha256") != rec.get("schema_sha256"):
        return {"state": "stale", "detail": f"{SEED_FILE} was generated for a different schema.sql; run `seed` again."}
    v = rec.get("validation") or {}
    if not v.get("completes_stage"):
        sql_errors = sql_error_list(v)
        note = (" [legacy approval: " + sql_schema.legacy_detail(legacy, SEED_FILE) + "]") if legacy else ""
        if not sql_errors and v.get("coverage_missing"):
            return {"state": "coverage_incomplete",
                    "detail": "SQL executed/checked successfully, but sample-data coverage is incomplete: "
                              + "; ".join(v["coverage_missing"][:2]) + note}
        return {"state": "failed_validation", "detail": "The last validation failed: " + "; ".join(sql_errors[:2] or v.get("errors", [])[:2]) + note}
    if legacy:
        return {"state": "legacy", "detail": sql_schema.legacy_detail(legacy, SEED_FILE)}
    return {"state": "current", "detail": f"seed.sql matches the approved design (validation: {v.get('level')}; " +
                                                                  ("executed on " + str(v.get("executed_dbms")) if v.get("level") == "executed"
                                                                   else "NOT executed on the selected DBMS") + ")."}


COVERAGE_PREFIX = "coverage missing: "


def sql_error_list(report):
    """Errors of the SQL checks only (coverage gaps excluded; old records mixed them into `errors`)."""
    if "sql_errors" in report:
        return list(report["sql_errors"])
    return [e for e in report.get("errors", []) if not str(e).startswith(COVERAGE_PREFIX)]


def status_summary(report, validated, model, schema_rec):
    """Six separate facts (never collapsed into one flag): seed generation, SQL execution, referential integrity,
    business rules, coverage, Stage 6 overall."""
    sql_errors = sql_error_list(report)
    level = report.get("level")
    checks = {c["name"]: c["ok"] for c in report.get("checks", [])}
    fk_name = "PRAGMA foreign_key_check reports no violations"
    if level == "executed" and fk_name in checks:
        ri = "passed (PRAGMA foreign_key_check, executed)" if checks[fk_name] else "FAILED (PRAGMA foreign_key_check)"
    elif level == "failed":
        ri = "failed or not reached (see SQL errors)"
    else:
        ri = "checked by the Python validator only; the DBMS did not verify it"
    enf = (schema_rec or {}).get("rule_enforcement") or {}
    n_rules = len(model.rules)
    declared = validated.semantic_unmet
    return {
        "seed_generation": "succeeded (records validated against NOT NULL, types, UNIQUE incl. partial, FK, CHECK)",
        "selected_dbms": report.get("selected_dbms") or report.get("dbms"),
        "executed_dbms": report.get("executed_dbms"),
        "static_validation": report.get("static_validation"),
        "execution": report.get("execution"),
        "sql_execution": ("passed" if level == "executed" and not sql_errors else
                          "failed" if sql_errors else "not executed on the chosen DBMS (static checks only)"),
        "referential_integrity": ri,
        "business_rules": {
            "status": "unverified" if n_rules else "not_applicable",
            "free_text_rules": n_rules,
            "enforced_by_schema_objects": len(enf.get("database", [])) if enf else None,
            "mapping_not_verified": len(enf.get("unverified", [])) if enf else None,
            "application_logic_only": len(enf.get("application", [])) if enf else None,
            "declared_participation_unmet": list(declared),
            "note": "Seed data is checked only against declared constraints; free-text business rules are not interpreted "
                    "and cross-table state consistency is not verified.",
        },
        "coverage": ("complete" if not validated.coverage else "incomplete"),
        "coverage_missing": list(validated.coverage),
        "coverage_not_applicable": list(validated.coverage_na),
        "coverage_uncertain": list(validated.coverage_uncertain),
        "offline_repairs": [dict(r) for r in validated.repairs],
        "stage6": "complete" if report.get("completes_stage") else "incomplete",
    }


def sql_files_status(session):
    return {"schema": sql_schema.schema_status(session), "seed": seed_status(session)}


def refresh_completion(session):
    """Stage 6 is complete only while both files are current and validated; otherwise reopen it (no LLM, no files)."""
    d = session.data
    st = d.get("sql_stage")
    if not st:
        return False
    ok = sql_schema.schema_status(session)["state"] == "current" and seed_status(session)["state"] == "current"
    if not ok and "sql" in d.get("completed_stages", []):
        d["completed_stages"].remove("sql")
        d["current_stage"] = "sql" if d.get("approval") else "revision"
        st["status"] = "in_progress"
        st["note"] = "Stage 6 was reopened: schema.sql or seed.sql is no longer current."
        session.save()
        return True
    return False


class SeedGenerationResult:
    def __init__(self, changed, record, report, completed, warnings, reused_data):
        self.changed, self.record, self.report, self.completed = changed, record, report, completed
        self.warnings, self.reused_data = warnings, reused_data


def _description(session):
    text = session.data.get("description") or ""
    return text if len(text) <= 1500 else text[:1500] + " ..."


def _load_saved(session, fp, schema_sha, model, min_rows):
    raw = _read(session.output_dir / DATA_FILE)
    if raw is None:
        return None
    try:
        saved = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if saved.get("design_fingerprint") != fp or saved.get("schema_sha256") != schema_sha:
        return None
    res = sd.validate_records(model, {"tables": saved.get("records")}, min_rows)
    if res.errors:
        return None
    if not res.coverage:
        res.repairs = list(saved.get("repairs") or [])           # repairs made in an earlier run stay documented
        return res
    fixed = sd.repair_coverage(model, res)                         # saved data with a missing NULL example: repair offline
    if fixed.coverage:
        return None                                                # other gaps (status values, link tables): ask again
    fixed.repairs = list(saved.get("repairs") or []) + [r for r in fixed.repairs]
    return fixed


def generate(session, llm, prompts, rows=None, force=False, regenerate=False, server_settings=None, connect=None):
    """Generate seed.sql. Raises ApprovalError / SchemaStateError / SeedError / LLMError (nothing is written then,
    except an `incomplete` marker when the LLM step fails, which `seed` simply re-runs)."""
    ap, design = approval.verify_approved_design(session, allow_legacy=True)
    legacy = approval.legacy_findings(session)      # intact historical approval that fails the current rules
    fp = ap["design_fingerprint"]
    schema_rec = sql_schema.require_current_schema(session, allow_legacy=True)
    schema_text = _read(session.output_dir / sql_schema.SCHEMA_FILE)
    model = sd.build_model(design, _description(session))
    rows = DEFAULT_ROWS if rows is None else rows
    if isinstance(rows, bool) or not isinstance(rows, int):
        raise DBAAgentError("--rows must be a whole number.")
    if rows < 1 or rows > sd.MAX_ROWS_PER_TABLE:
        raise DBAAgentError(f"--rows must be between 1 and {sd.MAX_ROWS_PER_TABLE}.")
    min_rows, row_notes = sd.required_rows(model, rows)       # every table must reach `rows` unless infeasible
    d = session.data
    stage = d.setdefault("sql_stage", {})
    st_now = seed_status(session)["state"]
    if not force and not regenerate and st_now in ("current", "legacy"):
        return SeedGenerationResult(False, stage["seed"], stage["seed"]["validation"], "sql" in d["completed_stages"], [], False)
    if legacy:        # offline recovery only: never new LLM data, never a forced rebuild, checked BEFORE anything is changed
        if force or regenerate:
            raise approval.ApprovalError("This approved design predates the current validation rules (" + legacy[0] + "); "
                                         "seed data cannot be regenerated from it. Only offline repair of the saved sample "
                                         "data is allowed. Revalidate with `revise` + `approve`.")
        if _load_saved(session, fp, schema_rec["sha256"], model, min_rows) is None:
            raise approval.ApprovalError("This approved design predates the current validation rules (" + legacy[0] + ") and "
                                         "there is no valid saved sample data (seed-data.json) that can be repaired offline; "
                                         "new sample data is not requested for an unrevalidated design. Nothing was written. "
                                         "Revalidate with `revise` + `approve`.")

    prev = stage.get("seed") or {}
    stage["seed"] = {"status": "generating", "design_fingerprint": fp, "schema_sha256": schema_rec["sha256"],
                     "dbms_id": model.dialect, "started_at": now_iso(), "rows_requested": rows}
    if prev.get("sha256"):          # earlier seed.sql stays on disk as a recoverable artifact, but is not "validated"
        stage["seed"]["previous_artifact"] = {"file": prev.get("file", SEED_FILE), "sha256": prev["sha256"],
                                              "generated_at": prev.get("generated_at"),
                                              "note": "kept for recovery; not validated by the current run"}
    # Reopen Stage 6 and persist that BEFORE the LLM call, so a failed run can never look complete.
    if "sql" in d.get("completed_stages", []):
        d["completed_stages"].remove("sql")
    d["current_stage"] = "sql"
    stage["status"] = "in_progress"
    stage.pop("validation_level", None)
    stage["note"] = "Stage 6 is incomplete: seed.sql generation is in progress or did not finish; run `seed` again."
    session.save()

    validated = None if (force or regenerate) else _load_saved(session, fp, schema_rec["sha256"], model, min_rows)
    reused = validated is not None
    if validated is None:
        validated = _ask_llm(session, llm, prompts, model, design, rows, min_rows)
    payload = {"design_fingerprint": fp, "schema_sha256": schema_rec["sha256"], "dbms_id": model.dialect,
               "generated_at": now_iso(), "records": sd.deep_records(validated.records)}
    if validated.repairs:
        payload["repairs"] = validated.repairs
    data_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    _atomic_write(session.output_dir / DATA_FILE, data_text)             # recovery point: data saved before SQL is rendered
    sql = sd.render_seed_sql(model, validated.records, fp)
    path = session.output_dir / SEED_FILE
    _atomic_write(path, sql)
    if _sha(_read(path) or "") != _sha(sql):
        raise DBAAgentError(f"{SEED_FILE} could not be written correctly; run `seed` again.")
    report = sql_validate.validate(model, validated.records, schema_text, sql, server_settings, connect)
    warnings = row_notes + list(validated.warnings) + [f"coverage not applicable: {c}" for c in validated.coverage_na]
    warnings += [f"coverage uncertain: {c}" for c in validated.coverage_uncertain]
    warnings += [f"semantic check: {c}" for c in validated.semantic_unmet]
    warnings += [f"offline repair: {r['table']}.{r['column']} of row {r['row']} was set to NULL to provide the required NULL "
                 f"example ({r['caveat']})" for r in validated.repairs]
    sql_errors = list(report.get("errors", []))
    report["sql_errors"] = sql_errors                          # SQL execution / static checks only
    if validated.coverage:                      # feasible coverage still missing: not fully validated, Stage 6 stays open
        # `ok` keeps meaning "the SQL checks passed"; coverage is a separate fact (errors keeps both for old readers)
        report["errors"] = sql_errors + [COVERAGE_PREFIX + c for c in validated.coverage]
        report.update(completes_stage=False, coverage_missing=list(validated.coverage))
    report["status_summary"] = status_summary(report, validated, model, schema_rec)
    rec = {"status": "generated", "file": SEED_FILE, "data_file": DATA_FILE, "design_fingerprint": fp,
           "schema_sha256": schema_rec["sha256"], "sha256": _sha(sql), "data_sha256": _sha(data_text),
           "dbms_id": model.dialect, "generated_at": now_iso(), "rows_requested": rows,
           "tables": len(model.tables), "rows_total": sum(len(r) for r in validated.records.values()),
           "row_counts": {t: len(r) for t, r in validated.records.items()}, "reused_saved_data": reused,
           "offline_repairs": [dict(r) for r in validated.repairs],
           "warnings": warnings, "validation": report}
    stage["seed"] = rec
    files = d.setdefault("output_files", [])
    for f in (SEED_FILE, DATA_FILE):
        if f not in files:
            files.append(f)
    completed = bool(report["completes_stage"]) and not legacy     # a legacy design is never marked complete
    if legacy and report["completes_stage"]:
        stage["status"] = "in_progress"
        stage["note"] = ("Stage 6 is incomplete: the files validate, but the approved design predates the current validation "
                         "rules and needs revalidation (`revise` + `approve`): " + legacy[0])
    elif completed:
        session.complete_stage("sql", "done")
        stage["status"] = "complete"
        stage["validation_level"] = report["level"]
        stage["note"] = ("Stage 6 complete: schema.sql and seed.sql were generated and "
                         + ("executed successfully on the selected DBMS (" + str(report.get("executed_dbms")) + ")."
                            if report["level"] == "executed"
                            else "checked statically only; they were NOT executed on the chosen DBMS ("
                                 + str(report.get("selected_dbms") or report.get("dbms")) + ") and are not verified."))
    else:
        stage["status"] = "in_progress"
        if validated.coverage and not sql_errors:
            stage["note"] = ("Stage 6 is incomplete: schema.sql and seed.sql " +
                             ("executed successfully" if report.get("level") == "executed" else "passed the static checks") +
                             ", but sample-data coverage is incomplete (see sql_stage.seed.validation.coverage_missing); "
                             "this is not an SQL failure.")
        else:
            stage["note"] = "Stage 6 is incomplete: seed.sql failed validation (see sql_stage.seed.validation)."
    d["last_error"] = None
    session.save()
    return SeedGenerationResult(True, rec, report, completed, warnings, reused)


def _ask_llm(session, llm, prompts, model, design, rows, min_rows):
    system = prompts.load("seed_system")
    rules = "\n".join(f"- {r}" for r in model.rules) or "- (none recorded)"
    user = prompts.render("seed_user", project=model.project, dbms=design["dbms"]["name"],
                          description=_description(session) or "(not recorded)", rules=rules, rows=rows,
                          min_rows=rows,
                          row_exceptions=("Exceptions where the schema makes the target impossible: " + "; ".join(f"{t} needs only {n}" for t, n in min_rows.items() if n < rows) + ".") if any(n < rows for n in min_rows.values()) else "",
                          schema=sd.describe_schema(model))
    max_attempts = getattr(llm.config, "max_attempts", 3) if hasattr(llm, "config") else 3
    seen = {"n": 0, "last": None}

    def validator(data):
        seen["n"] += 1
        res = sd.validate_records(model, data, min_rows)
        if not res.errors and res.coverage:                    # a missing NULL example is fixed offline, not by another paid call
            res = sd.repair_coverage(model, res)
        problems = list(res.errors)
        if not problems and res.coverage and seen["n"] < max_attempts:     # bounded retries; the last attempt is kept but flagged
            problems = list(res.coverage)
        if problems:
            shown = "; ".join(problems[:12]) + (f" (+{len(problems) - 12} more)" if len(problems) > 12 else "")
            raise InvalidResponseError("The sample data is not acceptable: " + shown)
        seen["last"] = res
        return data
    llm.complete_json("seed", system, user, validator)
    return seen["last"]
