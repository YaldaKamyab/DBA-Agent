"""Command-line interface.

  python -m dba_agent new [--file desc.txt]
  python -m dba_agent resume [SESSION_ID]
  python -m dba_agent list
  python -m dba_agent logs [SESSION_ID] [--full]
  python -m dba_agent revise [SESSION_ID] [--request TEXT] [--yes]
  python -m dba_agent review|approve|versions [SESSION_ID]
  python -m dba_agent restore [SESSION_ID] --version N
  python -m dba_agent recover [SESSION_ID] [--discard] [--yes]
"""
import argparse
import sys
from pathlib import Path

from . import workflow
from .config import load_config
from .errors import ConfigError, DBAAgentError, InvalidResponseError, MalformedResponseError, ProviderError, SessionError
from .llm import LLMClient
from .schema_sql import DIALECT_NAMES
from .logger import LLMCallLogger
from .prompts import PromptLoader
from .ui import UI
from .state import latest_session_id, list_sessions, load_session, new_session

END_MARK = "."


def read_description(input_fn=input, out=print):
    out("Describe the system you need a database for.")
    out(f"Finish with a line containing only '{END_MARK}' (or Ctrl-D / Ctrl-Z):")
    lines = []
    while True:
        try:
            line = input_fn()
        except EOFError:
            break
        if line.strip() == END_MARK:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def build_runtime(session, config):
    llm = LLMClient(config, LLMCallLogger(session.log_path))
    return llm, PromptLoader(config.prompts_dir)


def print_file(session, filename, out=print):
    path = session.output_dir / filename
    if path.is_file():
        out(f"\n--- {filename} (saved to {path}) ---")
        out(path.read_text(encoding="utf-8"))


def print_intake(session, out=print):
    print_file(session, "intake-summary.md", out)


# stage -> output file shown after the stage completes
STAGE_OUTPUTS = {"intake": "intake-summary.md", "interview": "requirements.md",
                "dbms": "dbms-choice.md"}


def print_design_summary(session, out=print):
    info = session.data.get("design_docs") or {}
    out(f"\nStage 4 documents written to {session.output_dir}:")
    for name in info.get("files", []):
        out(f"  - {name}")
    for w in info.get("warnings", []):
        out(f"WARNING: {w}")


def run_current(session, config, ui, description=None):
    """Run the stage the session is at. Returns 'ok', 'error' or 'paused'."""
    stage = session.current_stage
    if stage == "intake" and description is not None:
        session.data["description"] = description
        session.save()
    llm, prompts = build_runtime(session, config)
    ui.out(f"Running stage '{stage}' with {config.provider}/{config.model} ...")
    try:
        workflow.run_stage(stage, session, llm, prompts, ui)
    except DBAAgentError as e:
        session.data["last_error"] = {"stage": stage, "message": str(e)}
        session.save()  # input/answers are kept, so resume can retry
        ui.out(f"ERROR: {e}")
        ui.out(f"Session '{session.session_id}' was saved. Fix the problem and run: "
               f"python -m dba_agent resume {session.session_id}")
        return "error"
    except (EOFError, KeyboardInterrupt):
        session.save()
        ui.out(f"\nPaused during stage '{stage}'. Progress is saved. Continue with: "
               f"python -m dba_agent resume {session.session_id}")
        return "paused"
    ui.out(f"Stage '{stage}' done.")
    return "ok"


def _report_commit_state(session, out):
    """Resume/review hint: an interrupted or inconsistent Stage 5 commit is reported, never silently repaired."""
    from . import revision
    if not session.data.get("design"):
        return True
    st = revision.commit_status(session)
    if st["state"] == "clean":
        return True
    out("WARNING: a Stage 5 revision commit was interrupted or the saved files are inconsistent "
        f"({'recoverable' if st['state'] == 'pending_recoverable' else 'RECOVERY REQUIRED'}). Run "
        f"`python -m dba_agent recover {session.session_id}`.")
    for p in st["problems"]:
        out(f"  - {p}")
    return False


def cmd_recover(args, input_fn=input, out=print):
    """Detect and repair an interrupted Stage 5 commit (offline, no LLM). Without options it completes an interrupted,
    already accepted revision when that is certain; --discard abandons it instead. Never approves anything."""
    from . import revision
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    out(f"Session {session.session_id}")
    st = revision.commit_status(session)
    if st["state"] == "clean":
        out("Nothing to recover: no interrupted revision, and design.json matches the saved design.")
        return 0
    for p in st["problems"]:
        out(f"  PROBLEM: {p}")
    if args.discard:
        if not st["pending"]:
            out("There is no interrupted revision to discard; nothing was changed.")
            return 2
        if not args.yes:
            out("Discard the interrupted revision and keep the last committed design? [y/N]")
            try:
                ok = input_fn().strip().lower() in ("y", "yes")
            except EOFError:
                ok = False
            if not ok:
                out("Nothing was changed.")
                return 1
        rec = revision.discard_pending(session)
        out(f"Discarded the interrupted revision ({rec.get('summary') or rec.get('request')}); the committed design, its "
            "history and any approval are unchanged.")
        return 0
    if st["state"] == "recovery_required":
        out("RECOVERY REQUIRED: the evidence is contradictory, so nothing was changed. " +
            ("Use `recover --discard` to abandon the interrupted revision." if st["pending"] else
             "Check the files by hand or use `restore`."))
        return 2
    try:
        rec = revision.finish_pending(session)
    except DBAAgentError as e:
        out(f"CANNOT RECOVER: {e}")
        return 2
    out(f"Completed the interrupted revision {rec['number']}: {rec['summary']}.")
    if rec.get("approval_invalidated"):
        out("  NOTE: the previous approval is no longer valid; run `approve` again when you are satisfied.")
    return 0


def _report_sql_state(session, out):
    """Print schema.sql/seed.sql integrity problems and the next command. Returns True when something is not current."""
    from .stages import sql_seed
    if not session.data.get("sql_stage"):
        return False
    sts = sql_seed.sql_files_status(session)
    bad = {k: v for k, v in sts.items() if v["state"] != "current"}
    if not bad:
        return False
    sid = session.session_id
    out("Stage 6 is NOT complete - the SQL files are not current:")
    for name, v in bad.items():
        out(f"  {name}.sql: {v['state']} - {v['detail']}")
    if "schema" in bad:
        force = " --force" if bad["schema"]["state"] == "modified" else ""
        out(f"Next: python -m dba_agent schema {sid}{force}  (then `seed {sid}`)")
    else:
        force = " --force" if bad["seed"]["state"] == "modified" else ""
        out(f"Next: python -m dba_agent seed {sid}{force}")
    return True


def drive(session, config, ui, description=None):
    """Workflow controller: run consecutive stages from session.current_stage.

    Stops when a stage fails or is paused, when the user declines to start the
    next stage, or when the next stage is not implemented. Returns an exit code.
    """
    while True:
        stage = session.current_stage
        if not workflow.is_implemented(stage):
            ui.out(f"Stages 5 and 6 are implemented but run through dedicated commands, not `resume`. Completed so far: "
                   f"{', '.join(session.data.get('completed_stages', [])) or 'none'}.")
            sid = session.session_id
            _report_commit_state(session, ui.out)
            if stage == "revision":
                ui.out(f"Stage 5: review the design with `review {sid}`, change it with `revise {sid}`, and approve "
                       f"it with `approve {sid}` (python -m dba_agent <command>). Approval is never automatic.")
            elif stage == "sql" and session.data.get("approval"):
                ui.out(f"The design is approved. Stage 6: generate schema.sql with `schema {sid}`, then seed.sql with "
                       f"`seed {sid}` (python -m dba_agent <command>).")
                _report_sql_state(session, ui.out)
            elif stage == "done":
                if _report_sql_state(session, ui.out):
                    return 0
                ui.out("All six stages are complete: schema.sql and seed.sql are in the output folder "
                       f"({session.output_dir}). Check them with `seed {sid} --status`.")
            return 0
        if workflow.should_confirm(stage, session) and not ui.confirm(
                f"Continue to stage '{stage}'?"):
            ui.out(f"Stopped. Resume later with: python -m dba_agent resume {session.session_id}")
            return 0
        result = run_current(session, config, ui, description if stage == "intake" else None)
        description = None
        if result == "error":
            return 2
        if result == "paused":
            return 0
        if stage in STAGE_OUTPUTS:
            print_file(session, STAGE_OUTPUTS[stage], ui.out)
        elif stage == "design":
            print_design_summary(session, ui.out)


def cmd_new(args, config, input_fn=input, out=print):
    if args.file:
        try:
            description = Path(args.file).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise SessionError(f"Cannot read description file: {e}")
    else:
        description = read_description(input_fn, out)
    if not description:
        out("Empty description - nothing to do.")
        return 1
    session = new_session(config.sessions_dir,
                          {"provider": config.provider, "model": config.model})
    out(f"Session: {session.session_id}  ({session.root})")
    return drive(session, config, UI(input_fn, out), description)


def cmd_resume(args, config, input_fn=input, out=print):
    sid = args.session_id or latest_session_id(config.sessions_dir)
    session = load_session(config.sessions_dir, sid)
    ui = UI(input_fn, out)
    from .stages import sql_seed
    sql_seed.refresh_completion(session)     # reopen Stage 6 (and save) if schema.sql / seed.sql are no longer current
    out(f"Resumed session {sid} (current stage: {session.current_stage}, "
        f"completed: {', '.join(session.data.get('completed_stages', [])) or 'none'})")
    if session.data.get("last_error"):
        out(f"Last error: {session.data['last_error'].get('message')}")
    description = None
    if session.current_stage == "intake":
        description = session.data.get("description")
        if not description:
            description = read_description(input_fn, out)
            if not description:
                out("Empty description - nothing to do.")
                return 1
        else:
            out("Retrying intake with the saved description.")
    elif workflow.should_confirm(session.current_stage, session):
        print_intake(session, out)      # context before starting the next stage
    return drive(session, config, ui, description)


def resolve_sessions_dir(args):
    if args.sessions_dir:
        return args.sessions_dir
    try:
        return load_config(args.config).sessions_dir
    except ConfigError:
        return "sessions"


def cmd_design_draft(args, config, out=print):
    """Stage 4A preview: canonical design.json + conceptual-model.md for a session that
    finished Stage 3. Does NOT write the other documents, complete Stage 4 or change the
    current stage; `resume` runs the complete Stage 4."""
    from .stages import design
    sid = args.session_id or latest_session_id(config.sessions_dir)
    session = load_session(config.sessions_dir, sid)
    llm, prompts = build_runtime(session, config)
    out(f"Stage 4A (design model) for session {sid} with {config.provider}/{config.model} ...")
    try:
        design.generate_design(session, llm, prompts, force=args.force)
    except DBAAgentError as e:
        session.data["last_error"] = {"stage": "design", "message": str(e)}
        session.save()
        out(f"ERROR: {e}")
        return 2
    session.data["last_error"] = None
    session.save()
    out(f"Wrote {', '.join(design.PARTIAL_OUTPUTS)} to {session.output_dir}. "
        "Stage 4 is NOT complete (run `resume` to produce the other documents); current stage unchanged: "
        f"{session.current_stage}.")
    print_file(session, "conceptual-model.md", out)
    return 0


def print_revision(record, out=print):
    a = record["affected"]
    out(f"Revision {record['number']} accepted: {record['summary']}")
    if record.get("approval_invalidated"):
        out("  NOTE: the previous approval is no longer valid; run `approve` again when you are satisfied.")
    if a.get("dbms"):
        out(f"  DBMS: {a['dbms']['from']} -> {a['dbms']['to']}")
    for label, key in (("entities", "entities"), ("relationships", "relationships"), ("tables", "tables"),
                       ("constraints", "constraints"), ("business rules", "business_rules"),
                       ("assumptions", "assumptions"), ("open questions", "open_questions")):
        parts = [f"{k} {', '.join(v)}" for k, v in (a.get(key) or {}).items() if v]
        if parts:
            out(f"  {label}: " + "; ".join(parts))
    out("  regenerated: " + ", ".join(record["files_regenerated"]) + ", changelog.md")
    for t in record["assumptions"]:
        out(f"  ASSUMPTION: {t}")
    for w in record["warnings"]:
        out(f"  WARNING: {w}")


def make_confirm(input_fn, out, auto=False):
    """confirm(preview, record) for revision.revise: show the proposal, then accept/reject. Default answer is NO.
    auto (--yes) skips the question for ordinary proposals only. A suspicious proposal (possible unintended removals
    or changes outside the request's likely scope) is NEVER accepted by --yes: the person is asked explicitly."""
    def confirm(preview, record):
        out("\n--- Proposed revision (nothing has been changed yet) ---")
        out(preview)
        suspicious = (record.get("unexpected_removals") or
                      (None if record.get("restored_from") is not None else record.get("structural_removals"))
                      or record.get("unrelated_removals") or record.get("suspicious_changes")
                      or record.get("weakened_protections"))   # weakening is never auto-accepted, restore included; `restore --version N` names its own removals
        if auto and not suspicious:
            return True
        if auto:
            out("Not accepted automatically (--yes) because of the warning above; explicit confirmation is required.")
        out("\nAccept this revision? [y/N]" + (" (it contains possibly unintended changes)" if suspicious else ""))
        try:
            return input_fn().strip().lower() in ("y", "yes")
        except EOFError:
            out("No answer - treated as 'no'." + ("" if suspicious else " Use --yes to accept without asking."))
            return False
    return confirm


def cmd_revise(args, config, input_fn=input, out=print):
    """Stage 5: apply natural-language change requests (one with --request, else a prompt loop). Every proposal is
    shown with a deterministic diff and must be accepted (or use --yes). Approval is a separate command."""
    from . import revision
    sid = args.session_id or latest_session_id(config.sessions_dir)
    session = load_session(config.sessions_dir, sid)
    if not session.data.get("design") or not session.data.get("design_docs"):
        out("This session has no completed Stage 4 design yet; finish it with `resume` first.")
        return 2
    llm, prompts = build_runtime(session, config)
    code = 0
    try:
        if session.data.get("revision_pending"):
            out("Finishing the interrupted revision ...")
            print_revision(revision.finish_pending(session), out)
        requests = [args.request] if args.request else None
        while True:
            if requests is not None:
                if not requests:
                    break
                req = requests.pop(0)
            else:
                out("Describe a change to the design (empty line to stop):")
                try:
                    req = input_fn().strip()
                except EOFError:
                    break
                if not req:
                    break
            try:
                record = revision.revise(session, llm, prompts, req, make_confirm(input_fn, out, args.yes))
                if record is None:
                    out("Revision declined. The design and its documents are unchanged.")
                else:
                    print_revision(record, out)
            except ProviderError as e:
                out(f"API ERROR - no revision was evaluated; the design and its documents are unchanged: {e}")
                code = 2
                if e.kind in ("auth", "forbidden", "quota"):
                    break                                  # asking again cannot help until the configuration is fixed
            except MalformedResponseError as e:
                out(f"INVALID MODEL RESPONSE - the reply could not be used; the design and its documents are unchanged: {e}")
                code = 2
            except (revision.RevisionRejected, InvalidResponseError) as e:
                out(f"REVISION REJECTED (the design and its documents are unchanged): {e}")
                code = 2
            except DBAAgentError as e:
                out(f"ERROR (the design and its documents are unchanged): {e}")
                code = 2
    except KeyboardInterrupt:
        out("\nInterrupted. Progress is saved; run `revise` again to finish any pending revision.")
        return 130
    except DBAAgentError as e:
        out(f"ERROR: {e}")
        return 2
    out(f"When you are satisfied, approve the design with: python -m dba_agent approve {session.session_id}")
    return code


def print_check(result, out=print):
    for p in result["problems"]:
        out(f"  PROBLEM: {p}")
    for w in result["warnings"]:
        out(f"  WARNING: {w}")
    out("  Limits of the automatic checks:")
    for x in result["limitations"]:
        out(f"   - {x}")


def cmd_review(args, out=print):
    """Show the current design overview and whether it could be approved (no changes)."""
    from . import approval
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    if not session.data.get("design"):
        out("This session has no completed Stage 4 design yet; finish it with `resume` first.")
        return 2
    out(f"Session {session.session_id}")
    for line in approval.design_summary(session):
        out("  " + line)
    _report_commit_state(session, out)
    result = approval.check_approvable(session)
    if not result["problems"]:
        out("Ready for approval.")
    elif not result["structural_problems"] and not result.get("blocked_normalization") and result["acknowledgements_required"]:
        out("Approvable ONLY with an explicit normalization acknowledgement (see the items below):")
    else:
        out("NOT ready for approval:")
    print_check(result, out)
    return 0


def cmd_approve(args, input_fn=input, out=print):
    """Explicit final approval of the design (Stage 5 -> sql). Never happens automatically."""
    from . import approval
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    if not session.data.get("design"):
        out("This session has no completed Stage 4 design yet; finish it with `resume` first.")
        return 2
    out(f"Session {session.session_id}")
    for line in approval.design_summary(session):
        out("  " + line)
    ack = bool(getattr(args, "acknowledge_normalization", False))
    if approval.is_approved_unchanged(session):                 # historical approval: integrity check only, no re-gating
        try:
            ap, _ = approval.approve(session)
        except DBAAgentError as e:
            out(f"CANNOT APPROVE: {e}")
            return 2
        out(f"Design was already approved (version {ap['design_version']}, fingerprint {ap['design_fingerprint'][:12]}, "
            f"{ap['approved_at']}).")
        return 0
    result = approval.check_approvable(session)                 # not acknowledged yet: tells what needs acknowledging
    needs_ack = bool(result["acknowledgements_required"])
    if ack and needs_ack:
        result = approval.check_approvable(session, acknowledged=True)
    print_check(result, out)
    structural = result["structural_problems"]
    if structural:
        out("CANNOT APPROVE: fix the problems above (use `revise`) and try again.")
        return 2
    if result.get("blocked_normalization") and not approval.is_approved_unchanged(session):
        out("CANNOT APPROVE: a known 3NF violation has no justified denormalization exception. It cannot be accepted with "
            "`--yes` or an acknowledgement. Correct the design with `revise`, or document a justified denormalization "
            "exception, then approve again. Nothing was changed.")
        return 2
    if needs_ack and not ack:
        # a design with an unverified 3NF verdict or a documented denormalization exception normalization violation or an unverified 3NF verdict: explicit, separate consent
        out("NORMALIZATION ACKNOWLEDGEMENT REQUIRED (`--yes` alone does not count):")
        for item in result["acknowledgements_required"]:
            out(f"  - {item['message']}")
        out("Approving means you accept these as they are. Prefer fixing them with `revise` (or documenting a justified "
            "denormalization).")
        if not ack:
            out(f"Type {approval.ACK_PHRASE} to approve anyway (anything else cancels):")
            try:
                ack = input_fn().strip() == approval.ACK_PHRASE
            except EOFError:
                ack = False
        if not ack:
            out("CANNOT APPROVE: the normalization acknowledgement was not given. Nothing was changed. Fix the design with "
                "`revise`, or re-run `approve --acknowledge-normalization` to accept the risks explicitly.")
            return 2
    if not args.yes and not (ack and needs_ack):
        out("Approve this design and move on to SQL generation? [y/N]")
        try:
            answer = input_fn().strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            out("Not approved. Use `revise` to request changes, or run `approve` again later.")
            return 1
    try:
        ap, new = approval.approve(session, acknowledge_normalization=ack)
    except DBAAgentError as e:
        out(f"CANNOT APPROVE: {e}")
        return 2
    out(("Design approved" if new else "Design was already approved") +
        f" (version {ap['design_version']}, fingerprint {ap['design_fingerprint'][:12]}, {ap['approved_at']}).")
    out("Stage 5 is complete; the next stage is 'sql'. Generate schema.sql with `schema`, then seed.sql with `seed` "
        "(Stage 6). Any further accepted revision will cancel this approval.")
    return 0


def cmd_schema(args, out=print):
    """Stage 6A: generate schema.sql for the approved design (deterministic, no LLM); seed.sql is Stage 6B (`seed`)."""
    from .stages import sql_schema
    from .schema_sql import SchemaGenerationError
    from .approval import ApprovalError
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    out(f"Session {session.session_id}")
    if args.status:
        from .stages import sql_seed as _ss
        _ss.refresh_completion(session)
        st = sql_schema.schema_status(session)
        out(f"schema.sql: {st['state']} - {st['detail']}")
        return 0 if st["state"] == "current" else 1
    try:
        res = sql_schema.generate(session, force=args.force)
    except ApprovalError as e:
        out(f"CANNOT GENERATE SQL: {e}")
        out("Fix the problem (`revise`/`restore`, then `approve`) and run `schema` again. Nothing was written.")
        return 2
    except SchemaGenerationError as e:
        out(f"CANNOT GENERATE SQL: {e}")
        out("The approved design cannot be translated safely; change it with `revise`, approve it again, then retry.")
        return 2
    rec = res["record"]
    out((f"Generated {sql_schema.SCHEMA_FILE}" if res["changed"] else f"{sql_schema.SCHEMA_FILE} is already up to date") +
        f" for {rec['dbms_id']} ({rec['tables']} tables, design fingerprint {rec['design_fingerprint'][:12]}).")
    out(f"  file: {session.output_dir / sql_schema.SCHEMA_FILE}")
    for n in res["notes"]:
        out(f"  NOTE: {n}")
    enf = rec.get("rule_enforcement")
    if enf:
        out(f"  Business rules enforced by schema.sql (mapping verified against generated objects): {len(enf['database'])}; "
            f"mapping NOT fully verified (not counted): {len(enf.get('unverified', []))}; NOT enforced (application logic "
            f"required): {len(enf['application'])} - listed in the schema.sql header.")
    out("Not verified: the SQL has not been executed on a real DBMS by this command.")
    out(f"Stage 6 is NOT complete yet: next run `seed {session.session_id}` to generate and validate seed.sql.")
    return 0


def _print_validation(rep, out):
    """Generated / static / executed / unverified are reported separately; unexecuted SQL is never called verified."""
    level = rep["level"]
    sel = rep.get("selected_dbms") or rep.get("dbms")
    name = DIALECT_NAMES.get(sel, sel)
    out(f"Selected DBMS: {name}")
    out("  SQL generation:    done (schema.sql + seed.sql)")
    out(f"  Static checks:     {rep.get('static_validation', 'passed')}")
    if level == "executed":
        out(f"  SQL execution:     EXECUTED on {rep['executed_on']} - succeeded, {len(rep['checks'])} check(s) passed.")
        out(f"  Verified on {name}: yes")
    elif level == "unverified":
        out(f"  SQL execution:     NOT EXECUTED on {name} ({str(rep.get('unavailable_kind') or 'not attempted').replace('_', ' ')})")
        out("  " + rep["reason"])
        out(f"  Verified on {name}: NO - static checks are NOT proof that the SQL runs on {name}.")
    else:
        if rep.get("executed_on"):
            out(f"  SQL execution:     FAILED on {rep['executed_on']}:")
        else:
            out("  Static checks FAILED (SQL was not executed):")
        for e in rep["errors"][:8]:
            out(f"  - {e}")
        out(f"  Verified on {name}: NO")
    for c in rep["checks"]:
        if c["ok"] and level == "executed":
            out(f"  ok  {c['name']}")


def _print_status_summary(sm, out):
    """Stage 6B facts, kept apart: generation / SQL execution / referential integrity / business rules / coverage / stage."""
    if not sm:
        return
    br = sm["business_rules"]
    out("Stage 6B status:")
    out(f"  seed generation:        {sm['seed_generation']}")
    if sm.get("selected_dbms"):
        out(f"  selected DBMS:          {DIALECT_NAMES.get(sm['selected_dbms'], sm['selected_dbms'])}"
            f"; executed on: {DIALECT_NAMES.get(sm['executed_dbms'], sm['executed_dbms']) if sm.get('executed_dbms') else 'none'}")
        out(f"  static validation:      {sm.get('static_validation')}")
    out(f"  SQL execution:          {sm['sql_execution']}")
    out(f"  referential integrity:  {sm['referential_integrity']}")
    out(f"  business rules:         {br['status']} ({br['free_text_rules']} free-text rule(s) are not interpreted; "
        f"{len(br['declared_participation_unmet'])} declared participation gap(s))")
    out(f"  sample-data coverage:   {sm['coverage']}"
        + (f" ({len(sm['coverage_missing'])} feasible requirement(s) missing)" if sm["coverage_missing"] else "")
        + (f"; {len(sm['coverage_uncertain'])} undecidable" if sm.get("coverage_uncertain") else ""))
    out(f"  Stage 6 overall:        {sm['stage6']}"
        + (" (SQL generation only - not execution-verified)" if sm["stage6"] == "complete" and sm.get("execution") not in (None, "executed_ok") else ""))


def cmd_seed(args, out=print):
    """Stage 6B: generate seed.sql (LLM proposes records, Python validates, INSERTs are deterministic), validate both
    SQL files and complete Stage 6."""
    from .approval import ApprovalError
    from .seed_data import SeedError
    from .stages import sql_schema, sql_seed
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    out(f"Session {session.session_id}")
    if args.status:
        sql_seed.refresh_completion(session)     # reopen Stage 6 (and save) if schema.sql / seed.sql are no longer current
        sts = sql_seed.sql_files_status(session)
        for name in ("schema", "seed"):
            out(f"{name}.sql: {sts[name]['state']} - {sts[name]['detail']}")
        done = "sql" in session.data.get("completed_stages", [])
        _print_status_summary(((session.data.get("sql_stage") or {}).get("seed") or {}).get("validation", {}).get("status_summary"), out)
        out("Stage 6: " + ("COMPLETE" if done else "not complete"))
        return 0 if all(v["state"] == "current" for v in sts.values()) and done else 1
    if args.rows is not None and args.rows < 1:
        out(f"--rows must be at least 1 (got {args.rows}). Nothing was written.")
        return 2
    config = load_config(args.config)
    if args.sessions_dir:
        config.sessions_dir = args.sessions_dir
    llm, prompts = build_runtime(session, config)
    try:
        res = sql_seed.generate(session, llm, prompts, rows=config.seed_rows if args.rows is None else args.rows, force=args.force,
                                regenerate=args.regenerate, server_settings=config.validation)
    except ApprovalError as e:
        out(f"CANNOT GENERATE seed.sql: {e}")
        out("Fix the problem (`revise`/`restore`, then `approve` and `schema`) and run `seed` again. Nothing was written.")
        return 2
    except sql_schema.SchemaStateError as e:
        out(f"CANNOT GENERATE seed.sql: {e}")
        out(f"Run `schema {session.session_id}` (add --force if it was modified) and then `seed` again. Nothing was written.")
        return 2
    except SeedError as e:
        out(f"CANNOT SEED THIS SCHEMA: {e}")
        out("Nothing was written. Change the design with `revise`, approve it again, then run `schema` and `seed`.")
        return 2
    rec = res.record
    if not res.changed:
        out(f"{sql_seed.SEED_FILE} is already up to date (use --force to regenerate it).")
    else:
        out(f"Generated {sql_seed.SEED_FILE} for {rec['dbms_id']}: {rec['rows_total']} rows in {rec['tables']} tables "
            f"({'saved sample data reused, no LLM call' if res.reused_data else 'sample data proposed by the LLM and validated'}).")
        out(f"  file: {session.output_dir / sql_seed.SEED_FILE}")
        out("  rows: " + ", ".join(f"{t}={n}" for t, n in rec["row_counts"].items()))
    _print_validation(res.report, out)
    for w in res.warnings[:10]:
        out(f"  WARNING: {w}")
    for r in rec.get("offline_repairs") or []:
        out(f"  REPAIRED offline (no LLM call): {r['table']}.{r['column']} of row {r['row']} set to NULL to provide the "
            f"required NULL example; all constraints were re-validated. Review it: {r['caveat']}.")
    _print_status_summary(res.report.get("status_summary"), out)
    coverage_only = bool(rec.get("validation", {}).get("coverage_missing")) and not sql_seed.sql_error_list(res.report)
    if rec.get("validation", {}).get("coverage_missing"):
        out("Sample-data coverage is INCOMPLETE (feasible requirements still unmet after the LLM retries):")
        for c in rec["validation"]["coverage_missing"][:10]:
            out(f"  - {c}")
        out("  Run `seed --regenerate` (optionally with a larger --rows) to ask the LLM again.")
    if res.completed:
        if res.report["level"] == "executed":
            out("Stage 6 is COMPLETE (SQL generated and executed successfully on "
                f"{DIALECT_NAMES.get(res.report.get('selected_dbms'), res.report.get('selected_dbms'))}).")
        else:
            nm = DIALECT_NAMES.get(res.report.get("selected_dbms"), res.report.get("selected_dbms"))
            out(f"Stage 6 is COMPLETE as SQL generation (validation level: unverified): the SQL passed static checks only "
                f"and was NOT executed or verified on {nm}.")
        return 0
    if coverage_only:
        out("Stage 6 is NOT complete: the SQL itself is fine, only the sample-data coverage above is incomplete. "
            "`seed` re-uses the saved data (no LLM call) and repairs what it can offline; `seed --regenerate` asks the LLM again.")
        return 1
    out("Stage 6 is NOT complete: fix the problems above, then run `seed --force`.")
    return 1


def cmd_versions(args, out=print):
    from . import versions
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    current = len(session.data.get("revisions", []))
    found = versions.list_versions(session)
    if not found:
        out("No version snapshots yet (the first one is created when a revision is accepted).")
    for m in found:
        out(f"v{m['number']:03d}  {m['timestamp']}  {m.get('summary') or ''}" + ("   <- current" if m["number"] == current else ""))
    return 0


def cmd_restore(args, input_fn=input, out=print):
    """Bring back an earlier version (as a new revision; nothing is deleted)."""
    from . import revision
    sdir = resolve_sessions_dir(args)
    session = load_session(sdir, args.session_id or latest_session_id(sdir))
    try:
        record = revision.restore_version(session, args.version, make_confirm(input_fn, out, args.yes))
    except DBAAgentError as e:
        out(f"RESTORE FAILED (the design and its documents are unchanged): {e}")
        return 2
    if record is None:
        out("Restore declined. The design and its documents are unchanged.")
        return 1
    print_revision(record, out)
    return 0


def cmd_list(args, out=print):
    sessions = list_sessions(resolve_sessions_dir(args))
    if not sessions:
        out("No sessions yet.")
        return 0
    for s in sessions:
        llm = s.get("llm", {})
        out(f"{s['session_id']}  stage={s['current_stage']:<10} "
            f"model={llm.get('provider', '?')}/{llm.get('model', '?')}  "
            f"updated={s.get('updated_at', '?')}")
    return 0


def cmd_logs(args, out=print):
    import json
    sdir = resolve_sessions_dir(args)
    sid = args.session_id or latest_session_id(sdir)
    session = load_session(sdir, sid)
    entries = LLMCallLogger(session.log_path).read()
    out(f"{len(entries)} LLM call(s) in {session.log_path}")
    for e in entries:
        status = f"ERROR: {e['error']}" if e.get("error") else "ok"
        out(f"[{e['timestamp']}] stage={e['stage']} attempt={e['attempt']} "
            f"{e['provider']}/{e['model']} {e.get('duration_s')}s {status}")
        if args.full:
            out(json.dumps(e, indent=2, ensure_ascii=False))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="dba_agent", description="LLM-powered database design assistant")
    p.add_argument("--config", help="path to config JSON (default: ./config.json or $DBA_AGENT_CONFIG)")
    p.add_argument("--sessions-dir", help="override sessions directory")
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("new", help="start a new session")
    n.add_argument("--file", help="read the system description from a text file")
    r = sub.add_parser("resume", help="resume a session (default: latest)")
    r.add_argument("session_id", nargs="?")
    dd = sub.add_parser("design-draft", help="Stage 4A preview: design.json + conceptual-model.md "
                                             "only (use `resume` for the complete Stage 4)")
    dd.add_argument("session_id", nargs="?")
    dd.add_argument("--force", action="store_true", help="regenerate even if a design already exists")
    rv = sub.add_parser("revise", help="Stage 5A: change the design with natural-language requests")
    rv.add_argument("session_id", nargs="?")
    rv.add_argument("--request", help="a single change request (default: interactive prompt)")
    rv.add_argument("--yes", action="store_true", help="accept proposals without asking (still refuses proposals "
                                                       "that remove things a request only meant to add)")
    rw = sub.add_parser("review", help="show the current design overview and whether it can be approved")
    rw.add_argument("session_id", nargs="?")
    ap = sub.add_parser("approve", help="Stage 5: explicitly approve the design (then the session moves to `sql`)")
    ap.add_argument("session_id", nargs="?")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--acknowledge-normalization", action="store_true",
                    help="explicitly accept known unjustified normalization violations / unverified 3NF verdicts "
                         "(`--yes` alone never does)")
    vs = sub.add_parser("versions", help="list the saved design versions")
    vs.add_argument("session_id", nargs="?")
    rs = sub.add_parser("restore", help="restore an earlier design version (recorded as a new revision)")
    rs.add_argument("session_id", nargs="?")
    rs.add_argument("--version", type=int, required=True, help="version number from `versions`")
    rs.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sc = sub.add_parser("schema", help="Stage 6A: generate schema.sql for the approved design (no LLM)")
    sc.add_argument("session_id", nargs="?")
    sc.add_argument("--force", action="store_true", help="regenerate even if schema.sql is up to date")
    sc.add_argument("--status", action="store_true", help="only report whether schema.sql is valid for the approved design")
    sd_ = sub.add_parser("seed", help="Stage 6B: generate + validate seed.sql and complete Stage 6")
    sd_.add_argument("session_id", nargs="?")
    sd_.add_argument("--rows", type=int, help="sample rows per main table (default: config `seed_rows`, 8)")
    sd_.add_argument("--force", action="store_true", help="ask the LLM again and regenerate even if seed.sql is current")
    sd_.add_argument("--regenerate", action="store_true", help="ask the LLM again for new sample data")
    sd_.add_argument("--status", action="store_true", help="only report whether schema.sql and seed.sql are current")
    rc = sub.add_parser("recover", help="detect/repair an interrupted Stage 5 commit (offline)")
    rc.add_argument("session_id", nargs="?")
    rc.add_argument("--discard", action="store_true", help="abandon the interrupted revision instead of completing it")
    rc.add_argument("--yes", action="store_true", help="do not ask for confirmation (with --discard)")
    sub.add_parser("list", help="list sessions")
    lg = sub.add_parser("logs", help="show LLM call log of a session (default: latest)")
    lg.add_argument("session_id", nargs="?")
    lg.add_argument("--full", action="store_true", help="print full inputs/outputs")
    return p


def main(argv=None, input_fn=input, out=print):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            return cmd_list(args, out)
        if args.command == "logs":
            return cmd_logs(args, out)
        if args.command == "review":
            return cmd_review(args, out)
        if args.command == "approve":
            return cmd_approve(args, input_fn, out)
        if args.command == "schema":
            return cmd_schema(args, out)
        if args.command == "seed":
            return cmd_seed(args, out)
        if args.command == "versions":
            return cmd_versions(args, out)
        if args.command == "restore":
            return cmd_restore(args, input_fn, out)
        if args.command == "recover":
            return cmd_recover(args, input_fn, out)
        config = load_config(args.config)
        if args.sessions_dir:
            config.sessions_dir = args.sessions_dir
        if args.command == "design-draft":
            return cmd_design_draft(args, config, out)
        if args.command == "revise":
            return cmd_revise(args, config, input_fn, out)
        if args.command == "new":
            return cmd_new(args, config, input_fn, out)
        return cmd_resume(args, config, input_fn, out)
    except DBAAgentError as e:
        out(f"ERROR: {e}")
        return 2
    except KeyboardInterrupt:
        out("\nInterrupted. Session state is saved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
