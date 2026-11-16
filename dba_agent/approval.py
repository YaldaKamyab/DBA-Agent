"""Stage 5B - explicit final approval.

Stage 5 is never completed automatically: only `python -m dba_agent approve` (after the checks below pass and the
user says yes) marks it complete and moves the session to the `sql` stage. The approval is bound to a fingerprint of
the design, so Stage 6 can verify it is generating SQL for exactly the approved design (require_approved_design).
Any later accepted revision invalidates the approval (done in revision.commit).
"""
import copy
import hashlib
import json

from . import design_model as dm
from .design_docs import DOC_FILES, LIMITATIONS, check_documents
from .errors import DBAAgentError
from .normalization_analysis import NF3_PROVEN, NF3_UNVERIFIED, NF3_VIOLATED, analyze_design
from .state import now_iso

MAX_LISTED = 15
ACK_PHRASE = "I ACCEPT"        # what the user types at the prompt (`--yes` alone never counts as this acknowledgement)
MECHANICAL_NOTE = (
    "This is a MECHANICAL review: it checks structure, consistency between the documents, and the normal forms of the "
    "DECLARED functional dependencies. It is not a guarantee that the design is semantically correct, complete, or "
    "fit for the real business; a person must still review the requirements, dependencies and rules.")


class ApprovalError(DBAAgentError):
    """The design cannot be approved (or is not approved) - nothing was changed."""


def design_fingerprint(design):
    """SHA-256 of the design content. `meta` (timestamps/revision counter) is excluded on purpose, so restoring an
    earlier version gives the same fingerprint as that version had."""
    payload = {k: v for k, v in design.items() if k not in ("meta", "_revision")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


_PLACEHOLDER_JUSTIFICATIONS = {"n/a", "na", "none", "tbd", "todo", "-", "performance", "speed", "reporting", "for speed",
                               "for performance", "denormalized", "deliberate", "intentional"}


def substantive_justification(text):
    """A denormalization exception needs a real reason: at least three words and not a bare placeholder."""
    t = " ".join(str(text or "").lower().split()).strip(" .")
    return len(t.split()) >= 3 and t not in _PLACEHOLDER_JUSTIFICATIONS


def normalization_readiness(design):
    """Separate, mutually exclusive facts about the normalization of every table (no LLM, no writes):
    proven_3nf (complete keys, no violation among the declared dependencies), unverified_3nf (not proven, not refuted),
    known_violations (below 3NF, NO substantive justification in the design - these can NEVER be approved by a generic
    acknowledgement: correct the design or document a justified denormalization), documented_exceptions (below 3NF WITH a
    justified denormalization; approvable only with the user's explicit approval of the exception).
    Each item: {"table", "message"} (exceptions also carry "justifications")."""
    r = {"proven_3nf": [], "unverified_3nf": [], "known_violations": [], "documented_exceptions": []}
    for name, a in analyze_design(design).items():
        if a["nf3_status"] == NF3_VIOLATED:
            just = [d["justification"] for d in design["physical"].get("denormalizations", [])
                    if d["table"] == name and substantive_justification(d.get("justification"))]
            if a["unjustified"] or not just:
                r["known_violations"].append({"table": name, "message": (
                    f"Table `{name}` is below 3NF (a KNOWN violation of the declared dependencies) and has no documented, "
                    f"justified denormalization. It does NOT satisfy 3NF and cannot be approved with an acknowledgement: "
                    f"correct the design with `revise`, or document a justified denormalization exception for it.")})
            else:
                r["documented_exceptions"].append({"table": name, "justifications": just, "message": (
                    f"Table `{name}` is below 3NF (known violation) and is accepted ONLY as a documented denormalization "
                    f"exception: {'; '.join(just)}")})
        elif a["nf3_status"] == NF3_UNVERIFIED:
            why = "; ".join(a["nf3_unresolved"][:2]) or "the 3NF verdict is not certain"
            r["unverified_3nf"].append({"table": name, "message": (
                f"Table `{name}`: 3NF is UNVERIFIED (not proven and not refuted): {why}")})
        else:
            r["proven_3nf"].append({"table": name, "message": f"Table `{name}`: 3NF proven for the declared dependencies."})
    return r


def enforcement_findings(design):
    """Business rules not enforced by the schema itself: {"outside_schema": [...], "unsupported": [...]}.
    `unsupported`: a trigger rule on a DBMS for which schema.sql cannot generate it (nothing enforces it in the SQL)."""
    from .design_model import TRIGGER_DIALECTS
    dbms_id = (design.get("dbms") or {}).get("id")
    out = {"outside_schema": [], "unsupported": []}
    for r in design.get("business_rules", []):
        mech = (r.get("enforcement") or {}).get("mechanism")
        if mech in (None, "schema"):
            continue
        out["outside_schema"].append(f"{r['id']} is enforced by {mech}, not by the generated schema.")
        if mech == "trigger" and dbms_id not in TRIGGER_DIALECTS:
            out["unsupported"].append(f"{r['id']} requires a trigger, which schema.sql cannot generate for {dbms_id}.")
    return out


def check_approvable(session, acknowledged=False):
    """-> {"problems": [...] (block approval), "warnings": [...] (do not), "limitations": [...],
           "structural_problems": [...] (problems that are not normalization acknowledgements),
           "acknowledgements_required": [{"kind", "table", "message"}] (empty once `acknowledged`),
           "readiness": {...}}. No LLM, no writes.

    Kept apart on purpose: structural validity (problems), proven 3NF, unverified 3NF, known violations, documented
    exceptions, unresolved requirements and enforcement outside the schema. Policy: proven 3NF -> normal approval;
    UNVERIFIED 3NF -> approvable with the user's explicit acknowledgement (acknowledged=True); a documented, justified
    denormalization exception -> approvable only with that explicit approval too; a known UNJUSTIFIED violation is a
    problem that `acknowledged` never clears (`blocked_normalization`): the design must be corrected or the exception
    documented."""
    d, problems, warnings = session.data, [], []
    design = d.get("design")
    if not design or not d.get("design_docs"):
        msg = "Stage 4 is not complete: there is no design to approve (run `resume`)."
        return {"problems": [msg], "structural_problems": [msg], "warnings": [], "blocked_normalization": [],
                "limitations": list(LIMITATIONS),
                "acknowledgements_required": [], "readiness": {}}
    if d.get("revision_pending"):
        problems.append("An interrupted revision is pending; run `revise` (or `recover`) to finish it before approving.")
    try:
        dm.link_relationships(dm.validate_design(copy.deepcopy(design)))
    except DBAAgentError as e:
        problems.append(f"The design is not valid: {e}")
    out = session.output_dir
    on_disk = _read(out / "design.json")
    try:
        if on_disk is None or json.loads(on_disk) != design:
            problems.append("output/design.json is missing or differs from the saved design.")
    except json.JSONDecodeError:
        problems.append("output/design.json is not valid JSON.")
    docs = {n: _read(out / n) for n in DOC_FILES}
    missing = [n for n, t in docs.items() if t is None]
    if missing:
        problems.append("Missing documents: " + ", ".join(missing))
    elif not problems:
        problems += [f"Document check: {p}" for p in check_documents(design, docs)[:MAX_LISTED]]
    choice = d.get("dbms_choice") or {}
    if choice.get("selected_id") != design["dbms"]["id"]:
        problems.append(f"Saved DBMS selection ({choice.get('selected_id')}) differs from the design's DBMS "
                        f"({design['dbms']['id']}).")
    text = _read(out / "dbms-choice.md")
    if text is None:
        problems.append("dbms-choice.md is missing.")
    elif f"## Selected DBMS: {design['dbms']['name']}" not in text:
        problems.append(f"dbms-choice.md does not name {design['dbms']['name']} as the selected DBMS.")
    structural = list(problems)
    analysis = analyze_design(design)
    ready = normalization_readiness(design)
    blocked = [dict(kind="known_violation", table=i["table"], message=i["message"]) for i in ready["known_violations"]]
    problems += [i["message"] for i in blocked]               # never unlocked by `acknowledged`; not a structural problem
    need = [dict(kind="unverified_3nf", table=i["table"], message=i["message"]) for i in ready["unverified_3nf"]]
    need += [dict(kind="denormalization_exception", table=i["table"], message=i["message"])
             for i in ready["documented_exceptions"]]
    for item in need:
        if acknowledged:
            warnings.append(("DENORMALIZATION EXCEPTION APPROVED by the user: " if item["kind"] == "denormalization_exception"
                             else "ACKNOWLEDGED by the user: ") + item["message"])
        else:
            problems.append("Acknowledgement required: " + item["message"])
    for name, a in analysis.items():
        for f in a["findings"]:
            if f["form"] in ("KEY", "UNCERTAIN"):
                warnings.append(f"Table `{name}`: {f['message']}")
    enf = enforcement_findings(design)
    warnings += [f"Unsupported enforcement: {x}" for x in enf["unsupported"]]
    unsupported_ids = {u.split(" ")[0] for u in enf["unsupported"]}
    warnings += [f"Enforcement outside the schema: {x}" for x in enf["outside_schema"] if x.split(" ")[0] not in unsupported_ids]
    if design.get("requirement_gaps"):
        warnings.append(f"{len(design['requirement_gaps'])} unresolved requirement(s) from the interview remain.")
    if design["open_questions"]:
        warnings.append(f"{len(design['open_questions'])} open question(s) remain in assumptions.md.")
    required = [] if acknowledged else need
    return {"problems": problems, "structural_problems": structural, "warnings": warnings, "blocked_normalization": blocked,
            "limitations": list(LIMITATIONS) + [MECHANICAL_NOTE], "acknowledgements_required": required,
            "readiness": dict(ready, unresolved_requirements=len(design.get("requirement_gaps") or []),
                              open_questions=len(design.get("open_questions") or []),
                              unsupported_enforcement=enf["unsupported"], enforcement_outside_schema=enf["outside_schema"])}


def design_summary(session):
    """Short human-readable overview of the current design (used by `review` and `approve`)."""
    d = session.data
    design = d["design"]
    ents = [e["name"] for e in design["conceptual"]["entities"]]
    tabs = design["physical"]["tables"]
    lines = [f"Project: {design['project_name']}", f"DBMS: {design['dbms']['name']}",
             f"Entities ({len(ents)}): {', '.join(ents)}",
             f"Relationships: {len(design['conceptual']['relationships'])}",
             f"Tables ({len(tabs)}): {', '.join(t['name'] for t in tabs)}",
             f"Business rules: {len(design['business_rules'])}",
             f"Accepted revisions: {len(d.get('revisions', []))}",
             f"Fingerprint: {design_fingerprint(design)[:12]}",
             "Documents: " + ", ".join(DOC_FILES + ['dbms-choice.md', 'changelog.md']) + f"  (in {session.output_dir})"]
    ap = d.get("approval")
    lines.append(f"Approval: approved at {ap['approved_at']} (version {ap['design_version']})" if ap else "Approval: not approved yet")
    return lines


def is_approved_unchanged(session):
    """True when the session holds an approval of exactly the current design (a historical approval is never re-gated)."""
    d = session.data
    ap = d.get("approval")
    return bool(ap and ap.get("status") == "approved" and d.get("design")
                and ap.get("design_fingerprint") == design_fingerprint(d["design"]))


def approve(session, acknowledge_normalization=False):
    """Run the checks and record the approval. Raises ApprovalError (nothing changed) if the design is not approvable.
    A NEW approval of a design with an unverified 3NF verdict or a documented denormalization exception needs
    acknowledge_normalization=True (the explicit user acknowledgement); a known unjustified 3NF violation can never be
    approved (correct it or document a justified exception). An existing approval of the unchanged design is
    never invalidated or re-gated because the analysis became stricter later. Returns (approval record, newly_approved)."""
    d = session.data
    fp = design_fingerprint(d["design"])
    ap = d.get("approval")
    already = bool(ap and ap.get("status") == "approved" and ap.get("design_fingerprint") == fp)
    if already and ap.get("artifact_hashes") is not None:
        # historical approval with recorded hashes: only INTEGRITY is checked. A newer, stricter analysis/validator never
        # invalidates it (integrity_report keeps that as `revalidation`, not as a problem) and never demands an acknowledgement.
        blocking = integrity_report(session)["blocking"]
        if blocking:
            raise ApprovalError("The approved design cannot be trusted:\n" + "\n".join(f"- {p}" for p in blocking))
        return ap, False
    result = check_approvable(session, acknowledged=acknowledge_normalization or already)
    problems = result["structural_problems"] if already else result["problems"]
    if problems:
        raise ApprovalError("The design cannot be approved yet:\n" + "\n".join(f"- {p}" for p in problems))
    if already:
        if ap.get("artifact_hashes") is None:            # approval made before integrity hashes: re-verified above
            ap["artifact_hashes"] = artifact_hashes(session)
            session.save()
        return ap, False
    if session.current_stage != "revision":
        raise ApprovalError(f"Stage 5 is not active (current stage: {session.current_stage}).")
    ap = {"status": "approved", "approved_at": now_iso(), "design_version": len(d.get("revisions", [])),
          "design_fingerprint": fp, "dbms_id": d["design"]["dbms"]["id"],
          "normalization_warnings": result["warnings"], "limitations_acknowledged": result["limitations"],
          "normalization_review": {
              "mechanical_review_only": True,
              "acknowledged_by_user": bool(acknowledge_normalization),
              "known_violations_acknowledged": [],           # a known unjustified violation can never be approved
              "denormalization_exceptions_approved": [{"table": i["table"], "justifications": i["justifications"]}
                                                      for i in result["readiness"]["documented_exceptions"]],
              "unverified_3nf_acknowledged": [i["message"] for i in result["readiness"]["unverified_3nf"]] if acknowledge_normalization else [],
              "documented_exceptions": [i["message"] for i in result["readiness"]["documented_exceptions"]],
              "proven_3nf_tables": [i["table"] for i in result["readiness"]["proven_3nf"]]},
          "artifact_hashes": artifact_hashes(session)}
    d["approval"] = ap
    d["revision_stage"] = {"status": "approved", "revisions_accepted": len(d.get("revisions", [])),
                           "note": "Approved explicitly by the user."}
    d["last_error"] = None
    session.complete_stage("revision", "sql")
    session.save()
    return ap, True


# ------------------------------------------------------------------ approved-design integrity (Stage 5B fix)
DBMS_CHOICE_MD = "dbms-choice.md"
GUARDED_FILES = ["design.json"] + DOC_FILES + [DBMS_CHOICE_MD]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def artifact_hashes(session):
    """SHA-256 of every guarded file on disk ({name: hex or None if missing}). Read-only."""
    out = {}
    for n in GUARDED_FILES:
        t = _read(session.output_dir / n)
        out[n] = None if t is None else _sha(t)
    return out


# States of integrity_report()["state"]
NOT_APPROVED, TAMPERED, UNVERIFIED, LEGACY, INTACT = ("not_approved", "tampered", "unverified",
                                                       "legacy_revalidation_required", "intact")


def integrity_report(session):
    """Two separate questions, never mixed (read-only, never repairs or rewrites anything):

    1. ARTIFACT INTEGRITY - is what is on disk exactly what the user approved? Checked against the evidence recorded at
       approval time: approved fingerprint, design.json, SHA-256 of design.json / the Stage 4 documents / dbms-choice.md.
       Any failure is `blocking` (actual tampering, missing files, a pending revision ...).
    2. COMPLIANCE WITH CURRENT RULES - do the (unchanged) documents/design still satisfy today's validators and normalization
       analyzer? Failures are `revalidation` findings. They are reported only when integrity is proven by the recorded
       hashes: an unchanged historical approval is not tampered just because a newer rule disagrees with it, but it is also
       never treated as validated against the newer rules.

    -> {"state": not_approved|tampered|unverified|legacy_revalidation_required|intact, "blocking": [...], "revalidation": [...]}"""
    d = session.data
    ap = d.get("approval")
    if not ap or ap.get("status") != "approved":
        return {"state": NOT_APPROVED, "revalidation": [],
                "blocking": ["The design has not been approved. Run `python -m dba_agent approve` first."]}
    design = d.get("design")
    if not design:
        return {"state": TAMPERED, "revalidation": [], "blocking": ["The session has no design (Stage 4 is incomplete)."]}
    blocking, compliance = [], []
    if ap.get("design_fingerprint") != design_fingerprint(design):
        blocking.append("The current design in the session differs from the approved version (it was revised or the "
                        "session state was edited); review and approve it again.")
    if d.get("revision_pending"):
        blocking.append("A revision is pending; finish it with `revise` (or `recover`), then approve the result.")
    design_valid = True
    try:
        dm.link_relationships(dm.validate_design(copy.deepcopy(design)))
    except DBAAgentError as e:
        design_valid = False
        compliance.append(f"The approved design no longer passes the current design validation: {e}")
    dbms_id = ((design.get("dbms") or {}).get("id"))
    dbms_name = ((design.get("dbms") or {}).get("name"))
    choice = d.get("dbms_choice") or {}
    if choice.get("selected_id") != dbms_id or ap.get("dbms_id") not in (None, dbms_id):
        blocking.append(f"DBMS mismatch: design={dbms_id}, saved choice={choice.get('selected_id')}, "
                        f"approved={ap.get('dbms_id')}.")
    out = session.output_dir
    raw = _read(out / "design.json")
    if raw is None:
        blocking.append("output/design.json is missing (restore it from versions/ with `restore`, or re-run `revise`).")
    else:
        try:
            on_disk = json.loads(raw)
            if on_disk != design:
                blocking.append("output/design.json was modified after approval (it differs from the approved design).")
            elif design_fingerprint(on_disk) != ap.get("design_fingerprint"):
                blocking.append("output/design.json does not match the approved fingerprint.")
        except json.JSONDecodeError:
            blocking.append("output/design.json is corrupted (not valid JSON).")
    hashes = ap.get("artifact_hashes")
    now = artifact_hashes(session)
    for n in GUARDED_FILES:
        if now[n] is None:
            blocking.append(f"{n} is missing.")
        elif hashes is not None and hashes.get(n) != now[n]:
            blocking.append(f"{n} was modified after approval (its content no longer matches what was approved).")
    if hashes is None:
        blocking.append("This approval predates file-integrity hashes; run `python -m dba_agent approve` again to "
                        "re-verify and re-record it (nothing is changed except the approval record).")
    docs = {n: _read(out / n) for n in DOC_FILES}
    if design_valid and all(t is not None for t in docs.values()):
        compliance += [f"Document check: {p}" for p in check_documents(design, docs)[:MAX_LISTED]]
    text = _read(out / DBMS_CHOICE_MD)
    if text is not None and dbms_name and f"## Selected DBMS: {dbms_name}" not in text:
        compliance.append(f"{DBMS_CHOICE_MD} does not name {dbms_name} as the selected DBMS.")
    if blocking:
        # integrity is not proven, so a disagreement cannot be attributed to newer rules: report everything as blocking
        state = UNVERIFIED if hashes is None and len(blocking) == 1 else TAMPERED
        return {"state": state, "blocking": blocking + compliance, "revalidation": []}
    return {"state": LEGACY if compliance else INTACT, "blocking": [], "revalidation": compliance}


def integrity_problems(session, allow_legacy=False):
    """Everything that makes the approved design untrustworthy, as actionable messages (see integrity_report).
    allow_legacy=False (default, used before generating anything new): an unchanged historical approval that fails the
    current validation rules still blocks. allow_legacy=True (status / recovery of existing artifacts): only integrity
    failures are returned."""
    rep = integrity_report(session)
    return rep["blocking"] if allow_legacy else rep["blocking"] + rep["revalidation"]


def legacy_findings(session):
    """Findings of a historical approval whose artifacts are intact but fail the current rules ([] otherwise)."""
    return integrity_report(session)["revalidation"]


def verify_approved_design(session, allow_legacy=False):
    """For Stage 6: -> (approval record, deep copy of the verified approved design). Raises ApprovalError listing every
    problem if anything about the approval or the files on disk is not trustworthy. Read-only.
    allow_legacy=True tolerates ONLY the `legacy_revalidation_required` state (integrity proven, newer rules disagree);
    callers must then not generate new SQL from the design or mark anything complete."""
    rep = integrity_report(session)
    problems = rep["blocking"] + ([] if allow_legacy else rep["revalidation"])
    if problems:
        extra = ("\n(This approval is intact but predates the current validation rules; change the design with `revise` "
                 "and approve it again to revalidate it.)" if not rep["blocking"] else "")
        raise ApprovalError("The approved design cannot be used for SQL generation:\n" +
                            "\n".join(f"- {p}" for p in problems) + extra)
    return session.data["approval"], copy.deepcopy(session.data["design"])


def require_approved_design(session):
    """Returns the approval record, or raises ApprovalError (see verify_approved_design)."""
    return verify_approved_design(session)[0]
