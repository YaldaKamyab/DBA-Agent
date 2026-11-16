"""Stage 1 - System Intake.

Input : free-text system description (session.data["description"])
Output: session.data["intake"] (structured dict) + output/intake-summary.md
"""
from ..errors import InvalidResponseError

STAGE = "intake"
NEXT_STAGE = "interview"

_NAMED_LISTS = ("actors", "entities", "processes")
_TEXT_LISTS = ("requirements", "ambiguities")


def _as_named(item):
    if isinstance(item, str):
        return {"name": item.strip(), "description": ""}
    if isinstance(item, dict):
        name = str(item.get("name") or item.get("title") or "").strip()
        if not name:
            return None
        out = {"name": name, "description": str(item.get("description") or "").strip()}
        for key in ("responsibilities", "key_attributes", "actors"):
            val = item.get(key)
            if isinstance(val, str):
                val = [val]
            if isinstance(val, list):
                out[key] = [str(v).strip() for v in val if str(v).strip()]
        return out
    return None


def _as_text(item):
    if isinstance(item, str):
        return {"description": item.strip()} if item.strip() else None
    if isinstance(item, dict):
        desc = str(item.get("description") or item.get("issue") or item.get("question") or "").strip()
        if not desc:
            return None
        out = {"description": desc}
        for key in ("type", "why_it_matters", "suggested_question"):
            if item.get(key):
                out[key] = str(item[key]).strip()
        return out
    return None


def validate_intake(data):
    """Normalise and validate the model's JSON. Raises InvalidResponseError."""
    result = {
        "project_name": str(data.get("project_name") or "").strip() or "Untitled system",
        "summary": str(data.get("summary") or "").strip(),
    }
    if not result["summary"]:
        raise InvalidResponseError("Missing or empty 'summary'.")
    for key in _NAMED_LISTS:
        raw = data.get(key)
        if not isinstance(raw, list):
            raise InvalidResponseError(f"'{key}' must be a list.")
        result[key] = [x for x in (_as_named(i) for i in raw) if x]
    for key in _TEXT_LISTS:
        raw = data.get(key, [])
        if not isinstance(raw, list):
            raise InvalidResponseError(f"'{key}' must be a list.")
        result[key] = [x for x in (_as_text(i) for i in raw) if x]
    if not result["entities"]:
        raise InvalidResponseError("'entities' must contain at least one entity.")
    if not result["actors"]:
        raise InvalidResponseError("'actors' must contain at least one actor.")
    return result


def _bullets_named(items, extra_key=None, extra_label=None):
    if not items:
        return "_None identified._\n"
    lines = []
    for it in items:
        line = f"- **{it['name']}**"
        if it.get("description"):
            line += f" - {it['description']}"
        lines.append(line)
        if extra_key and it.get(extra_key):
            lines.append(f"  - {extra_label}: " + ", ".join(it[extra_key]))
    return "\n".join(lines) + "\n"


def render_markdown(intake, description):
    quoted = "\n".join("> " + ln if ln.strip() else ">" for ln in description.strip().splitlines())
    parts = [
        f"# Intake Summary - {intake['project_name']}\n",
        "## Overview\n",
        intake["summary"] + "\n",
        "## Actors and User Roles\n",
        _bullets_named(intake["actors"], "responsibilities", "Responsibilities"),
        "## Potential Entities\n",
        _bullets_named(intake["entities"], "key_attributes", "Candidate attributes"),
        "## Main Processes and Operations\n",
        _bullets_named(intake["processes"], "actors", "Actors involved"),
        "## Important Requirements\n",
    ]
    if intake["requirements"]:
        for r in intake["requirements"]:
            tag = f"[{r['type']}] " if r.get("type") else ""
            parts.append(f"- {tag}{r['description']}")
        parts.append("")
    else:
        parts.append("_None identified._\n")
    parts.append("## Missing or Ambiguous Information\n")
    if intake["ambiguities"]:
        for a in intake["ambiguities"]:
            line = f"- {a['description']}"
            if a.get("why_it_matters"):
                line += f" _(why it matters: {a['why_it_matters']})_"
            parts.append(line)
        parts.append("")
    else:
        parts.append("_None identified._\n")
    parts += ["## Original Description\n", quoted + "\n"]
    return "\n".join(parts)


def run(session, llm, prompts):
    description = (session.data.get("description") or "").strip()
    if not description:
        raise InvalidResponseError("No system description provided.", retryable=False)
    system = prompts.render("intake_system")
    user = prompts.render("intake_user", description=description)
    intake = llm.complete_json(STAGE, system, user, validator=validate_intake)
    session.data["intake"] = intake
    session.write_output("intake-summary.md", render_markdown(intake, description))
    session.complete_stage(STAGE, NEXT_STAGE)
    session.data["last_error"] = None
    session.save()
    return intake
