"""Stage 4 - design documentation (canonical design + the four design documents).

Input : session.data["requirements"] (Stage 2), session.data["dbms_choice"] (Stage 3)
Output: session.data["design"]  canonical design (single source of truth, see design_model.py)
        output/design.json, conceptual-model.md, logical-schema.md, normalization.md, assumptions.md

Two LLM calls (4A), each validated with bounded retries (config.max_attempts):
  design:conceptual  entities, relationships, business rules, assumptions
  design:logical     tables/keys/constraints/indexes/FDs for the chosen DBMS
The intermediate conceptual result is saved, so a failure in the second call does not repeat the first.

The four documents (4B) are rendered by code from the canonical design (design_docs.py) - NO further LLM
calls - and then re-read from disk and compared with the design (design_docs.check_documents).
Stage 4 is marked complete (and the session advances to Stage 5) only if that comparison finds no problem.
A saved design is reused, so a re-run after an interruption never repeats finished LLM calls.
"""
import json
from functools import partial

from .. import design_model as dm
from ..design_docs import DOC_FILES, check_documents, design_warnings, render_all
from ..design_render import render_conceptual_model
from ..errors import InvalidResponseError
from ..normalization_analysis import normalization_problems
from ..state import now_iso
from .interview import CATEGORY_TITLES, active_requirements, open_questions

STAGE = "design"
PARTIAL_OUTPUTS = ["design.json", "conceptual-model.md"]      # written by generate_design (4A) alone
STAGE_OUTPUTS = ["design.json"] + DOC_FILES
MAX_REPORTED_PROBLEMS = 15


def requirement_gaps(requirements):
    """Active unresolved items (resolved ones are excluded) as plain strings."""
    un = (requirements or {}).get("unresolved") or {}
    gaps = [f"No usable answer for category: {CATEGORY_TITLES.get(c, c)}" for c in un.get("uncovered_categories", [])]
    gaps += [q["question"] for q in open_questions(un)]
    gaps += [g["question"] for g in un.get("remaining_gaps", [])]
    gaps += list((requirements or {}).get("open_issues", []))
    seen, out = set(), []
    for g in gaps:
        if g.lower() not in seen:
            seen.add(g.lower())
            out.append(g)
    return out


def _dialect_notes(prompts, dbms_id):
    try:
        return prompts.render(f"design_dialect_{dbms_id}")
    except Exception:                       # unknown/new DBMS id: generic guidance
        return "Use standard SQL types valid for the chosen DBMS."


def validate_conceptual_response(data, gaps):
    out = dm.validate_conceptual(data)
    if gaps and not (out["assumptions"] or out["open_questions"]):
        raise InvalidResponseError(
            "The interview left unresolved requirements. Record each as an 'assumptions' entry (what you assumed and why) "
            "or an 'open_questions' entry; do not silently ignore them.")
    return out


def validate_logical_response(data, conceptual, dbms_id=None, nf_gate=None):
    """nf_gate (optional): called once per validation; returns True while the LLM may still be asked to repair a
    below-3NF design. On the last attempt the design is accepted and the violation is reported as a warning
    (normalization.md / assumptions.md) - it is never hidden and never claimed to be 3NF."""
    may_repair = nf_gate() if nf_gate is not None else False      # counts every attempt, also structurally invalid ones
    physical = dm.validate_physical(data, conceptual, dbms_id)
    low = normalization_problems({"physical": physical})        # target >= 3NF: send violations back for a targeted fix
    if low and may_repair:
        raise InvalidResponseError("The design is below Third Normal Form according to its own functional dependencies "
                                   "(formal 3NF: for every X -> A, X must be a superkey or A must be prime):\n"
                                   + "\n".join(f"- {m}" for m in low[:dm.MAX_REPORTED]))
    rules = dm.apply_rule_enforcement(conceptual["business_rules"], physical, data)
    errs = dm._Errors()
    extra = dm._assumptions(data.get("assumptions"), errs)
    errs.raise_if_any("Invalid 'assumptions':")
    return {"physical": physical, "business_rules": rules, "assumptions": extra}


def generate_design(session, llm, prompts, force=False):
    """Create (or reuse) the canonical design and write design.json + conceptual-model.md.
    Does not complete Stage 4. Returns the design dict."""
    d = session.data
    if not d.get("requirements"):
        raise InvalidResponseError("Stage 2 (requirements) must be completed first.", retryable=False)
    if not d.get("dbms_choice"):
        raise InvalidResponseError("Stage 3 (DBMS selection) must be completed first.", retryable=False)
    if force:
        d.pop("design", None)
        d.pop("design_progress", None)
    reqs, dbms = d["requirements"], d["dbms_choice"]
    gaps = requirement_gaps(reqs)
    if not d.get("design"):
        progress = d.setdefault("design_progress", {})
        req_json = json.dumps(active_requirements(reqs), indent=2, ensure_ascii=False)
        dbms_json = json.dumps({k: dbms[k] for k in ("selected", "selected_id", "design_implications", "caveats") if k in dbms},
                               indent=2, ensure_ascii=False)
        gaps_json = json.dumps(gaps, indent=2, ensure_ascii=False)
        if "conceptual" not in progress:
            progress["conceptual"] = llm.complete_json(
                "design:conceptual", prompts.render("design_conceptual_system"),
                prompts.render("design_conceptual_user", project_name=d["intake"]["project_name"],
                               requirements=req_json, dbms=dbms_json, gaps=gaps_json),
                validator=partial(validate_conceptual_response, gaps=gaps))
            session.save()
        conceptual = progress["conceptual"]
        left = [max(int(getattr(getattr(llm, "config", None), "max_attempts", 1) or 1), 1)]

        def nf_gate():                                  # True while another repair attempt remains
            left[0] -= 1
            return left[0] > 0
        logical = llm.complete_json(
            "design:logical",
            prompts.render("design_logical_system", dialect=_dialect_notes(prompts, dbms["selected_id"]),
                           dbms_name=dbms["selected"]),
            prompts.render("design_logical_user", requirements=req_json, dbms=dbms_json,
                           conceptual=json.dumps(conceptual, indent=2, ensure_ascii=False)),
            validator=partial(validate_logical_response, conceptual=conceptual,
                              dbms_id=dbms["selected_id"], nf_gate=nf_gate))
        design = dm.build_design(
            d["intake"]["project_name"], dbms, conceptual, logical["physical"], logical["business_rules"],
            extra_assumptions=logical["assumptions"], requirement_gaps=gaps,
            meta={"generated_at": now_iso(), "llm": d.get("llm", {}), "stage_part": "4A"})
        design = dm.link_relationships(dm.validate_design(design))      # final whole-design check
        d["design"] = design
        d.pop("design_progress", None)
        session.save()
    design = d["design"]
    session.write_output("design.json", json.dumps(design, indent=2, ensure_ascii=False) + "\n")
    session.write_output("conceptual-model.md", render_conceptual_model(design))
    session.save()                  # NOTE: generate_design never completes Stage 4; run() does, after validation
    return design


def run(session, llm, prompts):
    """Complete Stage 4: design (reused if saved) -> four documents -> validation -> advance to Stage 5."""
    d = session.data
    if d.get("design"):                                   # a saved design must still be valid
        try:
            dm.link_relationships(dm.validate_design(d["design"]))
        except InvalidResponseError as e:
            raise InvalidResponseError(
                f"The saved design is invalid ({e}). Regenerate it with: python -m dba_agent design-draft "
                f"{session.session_id} --force", retryable=False)
    design = generate_design(session, llm, prompts)
    docs = render_all(design)
    for name in DOC_FILES:
        session.write_output(name, docs[name])
    session.save()
    on_disk = {n: (session.output_dir / n).read_text(encoding="utf-8") if (session.output_dir / n).is_file() else None
               for n in DOC_FILES}
    problems = check_documents(design, on_disk)
    if problems:
        shown = "\n".join(f"- {p}" for p in problems[:MAX_REPORTED_PROBLEMS])
        more = f"\n- ... and {len(problems) - MAX_REPORTED_PROBLEMS} more" if len(problems) > MAX_REPORTED_PROBLEMS else ""
        raise InvalidResponseError("The Stage 4 documents are not consistent with design.json:\n" + shown + more,
                                   retryable=False)
    d["design_docs"] = {"validated_at": now_iso(), "files": list(STAGE_OUTPUTS), "warnings": design_warnings(design)}
    d["last_error"] = None
    session.complete_stage(STAGE, "revision")
    session.save()
    return design
