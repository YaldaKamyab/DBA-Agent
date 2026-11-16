"""Stage 3 - DBMS Selection.

Input : session.data["requirements"] (Stage 2 output, incl. unresolved gaps)
Output: session.data["dbms_choice"] (structured, used by Stages 4-6)
        output/dbms-choice.md
"""
import json
import re

from ..errors import InvalidResponseError
from .interview import active_requirements, open_questions

STAGE = "dbms"
NEXT_STAGE = "design"

DBMS_LIST = [
    ("postgresql", "PostgreSQL"),
    ("mysql_mariadb", "MySQL/MariaDB"),
    ("sqlite", "SQLite"),
    ("sql_server", "SQL Server"),
]
DBMS_IDS = [d[0] for d in DBMS_LIST]
DBMS_NAMES = dict(DBMS_LIST)

CRITERIA = [
    ("data_volume", "Data volume and growth"),
    ("concurrency", "Concurrency and multi-user access"),
    ("transactions_integrity", "Transactions and data integrity"),
    ("security", "Security, roles and privacy"),
    ("reporting_analytics", "Reporting and query needs"),
    ("deployment", "Deployment, hosting and cost"),
    ("maintainability", "Maintainability and team skills"),
]
CRITERIA_IDS = [c[0] for c in CRITERIA]
CRITERIA_TITLES = dict(CRITERIA)

_ALIASES = {
    "postgresql": "postgresql", "postgres": "postgresql", "pg": "postgresql",
    "mysql": "mysql_mariadb", "mariadb": "mysql_mariadb", "mysqlmariadb": "mysql_mariadb",
    "sqlite": "sqlite", "sqlite3": "sqlite",
    "sqlserver": "sql_server", "mssql": "sql_server", "microsoftsqlserver": "sql_server",
}


def dbms_id(value):
    key = re.sub(r"[\s_\-/]+", "", str(value or "").lower())
    if key not in _ALIASES:
        raise InvalidResponseError(
            f"Unknown DBMS '{value}'. Use exactly one of: {', '.join(DBMS_IDS)}.")
    return _ALIASES[key]


def _text(value):
    if isinstance(value, list):
        value = "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def _str_list(raw, key, required=False):
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise InvalidResponseError(f"'{key}' must be a list.")
    out = [str(x).strip() for x in raw if str(x).strip()]
    if required and not out:
        raise InvalidResponseError(f"'{key}' must contain at least one item.")
    return out


def _criterion_id(value):
    v = re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())
    if v in CRITERIA_TITLES:
        return v
    for cid, title in CRITERIA:
        if str(value or "").strip().lower() == title.lower():
            return cid
    return str(value or "").strip()          # extra, model-defined criterion


def validate_choice(data):
    selected = dbms_id(data.get("selected"))
    summary = _text(data.get("summary"))
    justification = _text(data.get("justification"))
    if not summary or not justification:
        raise InvalidResponseError("'summary' and 'justification' must be non-empty.")
    key_reqs = _str_list(data.get("key_requirements"), "key_requirements", required=True)

    raw_rows = data.get("comparison")
    if not isinstance(raw_rows, list):
        raise InvalidResponseError("'comparison' must be a list of criteria rows.")
    rows = {}
    for item in raw_rows:
        if not isinstance(item, dict) or not isinstance(item.get("ratings"), dict):
            raise InvalidResponseError("Every 'comparison' row needs 'criterion' and a 'ratings' object.")
        cid = _criterion_id(item.get("criterion"))
        ratings = {}
        for k, v in item["ratings"].items():
            ratings[dbms_id(k)] = _text(v)
        missing = [d for d in DBMS_IDS if not ratings.get(d)]
        if missing:
            raise InvalidResponseError(
                f"Comparison row '{cid}' lacks ratings for: {', '.join(missing)}.")
        rows[cid] = {"criterion": cid, "title": CRITERIA_TITLES.get(cid, cid),
                     "relevance": _text(item.get("relevance")), "ratings": ratings}
    missing = [c for c in CRITERIA_IDS if c not in rows]
    if missing:
        raise InvalidResponseError(f"'comparison' must contain rows for: {', '.join(missing)}.")
    ordered = [rows[c] for c in CRITERIA_IDS] + [r for c, r in rows.items() if c not in CRITERIA_IDS]

    rejected = {}
    raw_rej = data.get("rejected")
    if not isinstance(raw_rej, list):
        raise InvalidResponseError("'rejected' must be a list.")
    for item in raw_rej:
        if not isinstance(item, dict):
            raise InvalidResponseError("Every 'rejected' item must be an object with 'dbms' and 'reasons'.")
        d = dbms_id(item.get("dbms"))
        if d == selected:
            raise InvalidResponseError("The selected DBMS must not appear in 'rejected'.")
        reasons = _str_list(item.get("reasons"), "reasons", required=True)
        rejected[d] = reasons
    missing = [d for d in DBMS_IDS if d != selected and d not in rejected]
    if missing:
        raise InvalidResponseError(f"'rejected' must explain every other DBMS. Missing: {', '.join(missing)}.")

    return {
        "selected_id": selected,
        "selected": DBMS_NAMES[selected],
        "summary": summary,
        "key_requirements": key_reqs,
        "justification": justification,
        "comparison": ordered,
        "rejected": [{"dbms_id": d, "dbms": DBMS_NAMES[d], "reasons": rejected[d]}
                     for d in DBMS_IDS if d != selected],
        "caveats": _str_list(data.get("caveats"), "caveats"),
        "design_implications": _str_list(data.get("design_implications"), "design_implications"),
    }


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def apply_override(choice, new_id, request, rationale, revision_number, timestamp):
    """Return the DBMS choice after the USER overrode the recommendation (Stage 5 revision).

    Nothing is falsified: the first analysis is kept untouched under "original" (marked superseded in the
    document) and every override is listed in "overrides". The top-level fields describe the CURRENT choice.
    """
    base = choice.get("original") or {k: v for k, v in choice.items() if k not in ("overrides", "original")}
    overrides = list(choice.get("overrides") or [])
    overrides.append({"revision": revision_number, "timestamp": timestamp, "from_id": choice["selected_id"],
                      "to_id": new_id, "request": request, "rationale": rationale})
    rejected = []
    for d in DBMS_IDS:
        if d == new_id:
            continue
        if d == base.get("selected_id") and d != new_id:
            reasons = [f"Originally recommended; replaced by the user's request in revision {revision_number}."]
        else:
            reasons = list(next((r["reasons"] for r in base.get("rejected", []) if r["dbms_id"] == d), [])) or \
                [f"Not selected (see the original analysis)."]
        rejected.append({"dbms_id": d, "dbms": DBMS_NAMES[d], "reasons": reasons})
    return {"selected_id": new_id, "selected": DBMS_NAMES[new_id],
            "summary": f"{DBMS_NAMES[new_id]} was chosen by the user (override in revision {revision_number}); "
                       f"the agent originally recommended {base.get('selected', '?')}.",
            "key_requirements": list(base.get("key_requirements", [])),
            "justification": f"User-requested override: \"{request}\". {rationale}",
            "comparison": base.get("comparison", []), "rejected": rejected, "caveats": [],
            "design_implications": [f"The physical design was rewritten for {DBMS_NAMES[new_id]} in revision "
                                    f"{revision_number} (see changelog.md and logical-schema.md)."],
            "overrides": overrides, "original": base}


def _override_section(choice):
    ov = choice["overrides"]
    lines = ["## User-Requested DBMS Override\n",
             f"The agent's recommendation was changed by the user. Current DBMS: **{choice['selected']}**.\n"]
    for o in ov:
        lines.append(f"- Revision {o['revision']} ({o['timestamp']}): {DBMS_NAMES.get(o['from_id'], o['from_id'])} -> "
                     f"{DBMS_NAMES.get(o['to_id'], o['to_id'])}. Request: \"{o['request']}\". {o['rationale']}")
    lines += ["", "The comparison table and the superseded original analysis below were produced BEFORE the override "
                  "and were not re-evaluated for the new choice.\n"]
    return lines


def _superseded_section(choice):
    o = choice["original"]
    lines = ["", f"## Superseded Original Recommendation: {o.get('selected', '?')}\n",
             "_This was the agent's analysis before the user override. It is kept for the record and is no longer the decision._\n",
             o.get("summary", ""), "", o.get("justification", ""), ""]
    for r in o.get("rejected", []):
        lines += [f"- Originally rejected: {r['dbms']} - " + "; ".join(r["reasons"])]
    return lines + [""]


def render_markdown(choice, requirements, project_name):
    sel = choice["selected_id"]
    head = ["Criterion", "Relevance for this system"] + [
        DBMS_NAMES[d] + (" (selected)" if d == sel else "") for d in DBMS_IDS]
    lines = [f"# DBMS Selection - {project_name}\n",
             f"## Selected DBMS: {choice['selected']}\n", choice.get("summary", "") + "\n"]
    if choice.get("overrides"):
        lines += _override_section(choice)
    lines += ["## Requirements That Drove the Decision\n"]
    lines += [f"- {r}" for r in choice.get("key_requirements", [])]
    lines += ["", "## Justification\n", choice.get("justification", "") + "\n", "## Comparison Table\n",
              "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in choice.get("comparison", []):
        cells = [f"**{_cell(row['title'])}**", _cell(row["relevance"])] + [
            _cell(row["ratings"][d]) for d in DBMS_IDS]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "## Why the Other Options Were Rejected\n"]
    for r in choice.get("rejected", []):
        lines.append(f"### {r['dbms']}\n")
        lines += [f"- {x}" for x in r["reasons"]]
        lines.append("")
    if choice["caveats"]:
        lines += ["## Caveats and Mitigations\n"] + [f"- {c}" for c in choice["caveats"]] + [""]
    if choice["design_implications"]:
        lines += ["## Implications for the Design\n"] + [f"- {c}" for c in choice["design_implications"]] + [""]
    if choice.get("overrides"):
        lines += _superseded_section(choice)
    un = (requirements or {}).get("unresolved") or {}
    gaps = ([f"No usable answer for: {c}" for c in un.get("uncovered_categories", [])]
            + [q["question"] for q in open_questions(un)]
            + [g["question"] for g in un.get("remaining_gaps", [])])
    if gaps:
        lines += ["## Unresolved Requirements That Could Affect This Choice\n",
                  "The decision above was made despite these open points (see requirements.md):\n"]
        lines += [f"- {g}" for g in gaps] + [""]
    return "\n".join(lines)


def run(session, llm, prompts):
    reqs = session.data.get("requirements")
    if not reqs:
        raise InvalidResponseError("Stage 2 (requirements) must be completed first.", retryable=False)
    dbms_list = "\n".join(f"- {i}: {n}" for i, n in DBMS_LIST)
    criteria = "\n".join(f"- {i}: {t}" for i, t in CRITERIA)
    system = prompts.render("dbms_system", dbms_list=dbms_list, criteria=criteria)
    user = prompts.render(
        "dbms_user", project_name=session.data["intake"]["project_name"],
        requirements=json.dumps(active_requirements(reqs), indent=2, ensure_ascii=False))
    choice = llm.complete_json("dbms:selection", system, user, validator=validate_choice)
    session.data["dbms_choice"] = choice
    session.write_output("dbms-choice.md",
                         render_markdown(choice, reqs, session.data["intake"]["project_name"]))
    session.complete_stage(STAGE, NEXT_STAGE)
    session.data["last_error"] = None
    session.save()
    return choice
