"""Workflow registry. Add Stages 5-6 by registering them here."""
from .stages import dbms, design, intake, interview

STAGE_ORDER = ["intake", "interview", "dbms", "design", "revision", "sql"]

# stage name -> run(session, llm, prompts[, ui])
IMPLEMENTED = {
    "intake": intake.run,
    "interview": interview.run,
    "dbms": dbms.run,
    "design": design.run,
}
INTERACTIVE = {"interview"}        # these stages receive a `ui` argument

# stage name -> "has this stage already begun?" (used to decide whether
# `resume` should ask before starting it)
STARTED = {
    "interview": lambda s: bool(s.data.get("interview")),
    "dbms": lambda s: bool(s.data.get("dbms_choice")),
    "design": lambda s: bool(s.data.get("design") or s.data.get("design_progress")),
}


def is_implemented(stage):
    return stage in IMPLEMENTED


def should_confirm(stage, session):
    """Ask before *starting* a stage that is not the first one."""
    if stage == STAGE_ORDER[0]:
        return False
    return not STARTED.get(stage, lambda s: False)(session)


def run_stage(stage, session, llm, prompts, ui=None):
    if stage not in IMPLEMENTED:
        raise NotImplementedError(f"Stage '{stage}' is not implemented yet.")
    if stage in INTERACTIVE:
        return IMPLEMENTED[stage](session, llm, prompts, ui)
    return IMPLEMENTED[stage](session, llm, prompts)
