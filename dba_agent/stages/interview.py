"""Stage 2 - Requirements Interview.

Input : session.data["intake"] (Stage 1 output)
Output: session.data["interview"] (rounds, answers, decisions),
        session.data["requirements"] (structured), output/requirements.md

Flow: round 1 (questions for all 7 categories) -> user answers -> the LLM
reviews the answers and either asks a follow-up round or declares the
interview finished -> requirements.md is generated. State is saved after
every answer, so an interrupted interview resumes where it stopped.
"""
import json
import re
from functools import partial

from ..errors import InvalidResponseError

STAGE = "interview"
NEXT_STAGE = "dbms"
MAX_ROUNDS = 4          # initial round + at most 3 follow-up rounds
SKIPPED = "(skipped)"
# replies that carry no information (an empty Enter is stored as SKIPPED)
NON_ANSWERS = {SKIPPED, "skip", "skipped", "?", "n/a", "na", "idk", "unknown",
               "i don't know", "i dont know", "don't know", "dont know", "no idea"}

# Pure statements of ignorance with only filler words ("I don't know yet", "not sure", "no idea at the moment"). They carry
# no information, so they are MISSING information - never a real answer that later stages could build a business rule on.
# An answer that adds any content ("I don't know exactly, maybe 300") does not match and stays an answer.
_FILLER = r"(?:\s+(?:yet|exactly|really|sorry|honestly|for\s+sure|for\s+now|right\s+now|at\s+the\s+moment|at\s+this\s+point|please))*"
_UNCERTAIN_RE = re.compile(
    r"^(?:i\s+)?(?:(?:really|honestly|just)\s+)?(?:do\s*n[o']?t|dont|do\s+not|cannot|can[o']?t|cant)\s+(?:really\s+)?"
    r"(?:know|say|tell|decide|remember)" + _FILLER + r"$"
    r"|^(?:i\s*(?:am|'m)\s+)?(?:really\s+)?(?:not\s+sure|unsure|undecided|not\s+decided|no\s+idea|not\s+known|"
    r"to\s+be\s+decided|to\s+be\s+determined|tbd|unknown|not\s+yet\s+(?:known|decided))" + _FILLER + r"$", re.IGNORECASE)

# explicit "this does not apply" statements: a deliberate answer, unlike a skip/idk
_NA_RE = re.compile(r"^(?:n/?a|not applicable|does(?:n't| not) apply|not relevant|irrelevant|"
                    r"no such (?:thing|requirement|rule)s?)(?:\b|$)", re.IGNORECASE)
# categories whose questions can never be settled by "not applicable"
NA_NOT_ALLOWED = {"entities_attributes"}

CATEGORIES = [
    ("entities_attributes", "Entities and their attributes"),
    ("relationships_cardinalities", "Relationships and cardinalities"),
    ("data_volume_growth", "Data volume and growth"),
    ("read_write_reporting", "Read/write patterns and reporting needs"),
    ("transactions_concurrency", "Transactions, consistency and concurrency"),
    ("security_roles_privacy", "Security, user roles and privacy"),
    ("business_rules_constraints", "Business rules and constraints"),
]
CATEGORY_IDS = [c[0] for c in CATEGORIES]
CATEGORY_TITLES = dict(CATEGORIES)


# ---------------------------------------------------------------- validation
def _category(value):
    v = str(value or "").strip().lower()
    if v in CATEGORY_TITLES:
        return v
    for cid, title in CATEGORIES:
        if v == title.lower() or v.replace(" ", "_").replace("-", "_") == cid:
            return cid
    raise InvalidResponseError(
        f"Unknown category '{value}'. Use exactly one of: {', '.join(CATEGORY_IDS)}.")


def _questions(data, key="questions"):
    raw = data.get(key)
    if not isinstance(raw, list):
        raise InvalidResponseError(f"'{key}' must be a list.")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            raise InvalidResponseError(f"Every item in '{key}' needs a non-empty 'question'.")
        entry = {"category": _category(item.get("category")),
                 "question": str(item["question"]).strip(),
                 "why": str(item.get("why") or "").strip()}
        links = item.get("resolves")
        if isinstance(links, list):          # optional: ids of earlier questions this one settles
            links = [str(x).strip() for x in links if str(x).strip()]
            if links:
                entry["resolves"] = links
        out.append(entry)
    return out


def validate_questions(data):
    qs = _questions(data)
    missing = [c for c in CATEGORY_IDS if c not in {q["category"] for q in qs}]
    if missing:
        raise InvalidResponseError(
            "The first round must contain at least one question for EVERY category. "
            f"Missing: {', '.join(missing)}.")
    return qs


def validate_review(data, first_round, final=False):
    enough = data.get("enough_information")
    if not isinstance(enough, bool):
        raise InvalidResponseError("'enough_information' must be true or false.")
    reasoning = str(data.get("reasoning") or "").strip()
    if not reasoning:
        raise InvalidResponseError("'reasoning' must explain the decision.")
    follow = _questions(data, "follow_up_questions") if data.get("follow_up_questions") else []
    if final:
        # last allowed round: questions are NOT asked, they are kept as unresolved gaps
        return {"enough_information": enough, "reasoning": reasoning,
                "follow_up_questions": [] if enough else follow[:8]}
    if first_round:
        # at least one follow-up round is mandatory
        if not follow:
            raise InvalidResponseError(
                "At least one follow-up round is required: provide 'follow_up_questions' "
                "that dig into gaps, vague or surprising answers.")
        enough = False
    elif not enough and not follow:
        raise InvalidResponseError(
            "If enough_information is false you must provide 'follow_up_questions'.")
    if enough:
        follow = []
    return {"enough_information": enough, "reasoning": reasoning,
            "follow_up_questions": follow[:8]}


def validate_requirements(data):
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise InvalidResponseError("Missing or empty 'summary'.")
    raw = data.get("categories")
    if not isinstance(raw, list):
        raise InvalidResponseError("'categories' must be a list.")
    found = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = _category(item.get("id"))
        findings = item.get("findings")
        if isinstance(findings, str):
            findings = [findings]
        if isinstance(findings, list):
            found[cid] = [str(f).strip() for f in findings if str(f).strip()]
    missing = [c for c in CATEGORY_IDS if not found.get(c)]
    if missing:
        raise InvalidResponseError(
            f"'categories' must contain non-empty 'findings' for every category. Missing: {', '.join(missing)}.")
    issues = data.get("open_issues", [])
    if not isinstance(issues, list):
        raise InvalidResponseError("'open_issues' must be a list.")
    return {"summary": summary,
            "categories": [{"id": c, "findings": found[c]} for c in CATEGORY_IDS],
            "open_issues": [str(i).strip() for i in issues if str(i).strip()]}


# ------------------------------------------------------------------- helpers
def categories_text():
    return "\n".join(f"- {cid}: {title}" for cid, title in CATEGORIES)


def transcript(rounds):
    lines = []
    for r in rounds:
        lines.append(f"## Round {r['number']} ({r['kind'].replace('_', '-')})")
        for q in r["questions"]:
            lines.append(f"[{q['category']}] Q: {q['question']}")
            ans = q['answer'] if q['answer'] is not None else '(not answered yet)'
            if answer_kind(q['answer']) == "not_applicable":
                ans += "  [explicitly not applicable - a deliberate answer, not missing information]"
            lines.append(f"A: {ans}")
        if r.get("review"):
            lines.append(f"(Interviewer's decision after this round: {r['review']['reasoning']})")
        lines.append("")
    return "\n".join(lines).strip()


def is_answered(answer):
    text = (answer or "").strip().lower()
    if not text or text in NON_ANSWERS:
        return False
    return not _UNCERTAIN_RE.match(text.strip(" .!?,;:\t"))


def answer_kind(answer):
    """'answered' (real information), 'not_applicable' (user explicitly says the
    question does not apply) or 'missing' (empty, skipped, idk, unanswered)."""
    text = (answer or "").strip()
    if text and _NA_RE.match(text):          # checked first: "n/a - single user" is a deliberate statement
        return "not_applicable"
    return "answered" if is_answered(answer) else "missing"


def is_resolved(answer):
    return answer_kind(answer) != "missing"


def uncovered_categories(rounds):
    """Categories in which the user has given no usable answer at all. A
    deliberate 'not applicable' covers a category (it is information), except
    for categories listed in NA_NOT_ALLOWED."""
    covered = set()
    for r in rounds:
        for q in r["questions"]:
            kind = answer_kind(q["answer"])
            if kind == "answered" or (kind == "not_applicable" and q["category"] not in NA_NOT_ALLOWED):
                covered.add(q["category"])
    return [c for c in CATEGORY_IDS if c not in covered]


def apply_coverage_guard(review, rounds):
    """Code-level check: 'enough information' is impossible while a category has
    no real answer. Overrides the model and re-asks the skipped questions."""
    uncovered = uncovered_categories(rounds)
    if not review["enough_information"] or not uncovered:
        return review
    follow = []
    for cat in uncovered:
        skipped = next(q for r in rounds for q in r["questions"] if q["category"] == cat)
        follow.append({"category": cat, "why": "No substantive answer has been given for this category.",
                       "question": f"(Skipped earlier, still needed) {skipped['question']}",
                       "resolves": [skipped["id"]]})
    titles = ", ".join(CATEGORY_TITLES[c] for c in uncovered)
    return {"enough_information": False, "overridden": True, "follow_up_questions": follow,
            "reasoning": f"{review['reasoning']} [Code check: no substantive answer for: {titles}; "
                         "'enough information' was overridden and the skipped questions are asked again.]"}


def skipped_listing(rounds):
    """Unanswered questions with ids, so the reviewer can declare which ones a
    follow-up question settles ('resolves')."""
    res = _resolution_map(rounds)
    lines = [f"- {q['id']} [{q['category']}] {q['question']}"
             for r in rounds for q in r["questions"]
             if answer_kind(q["answer"]) == "missing" and q["id"] not in res and q["answer"] is not None]
    return "\n".join(lines) or "(none)"


def _resolution_map(rounds):
    """question id -> id of the answered later question that settled it.

    A later question settles earlier ones via its `resolves` links (set by the
    coverage guard or declared by the reviewing model), but only if the later
    question itself got a usable answer. Links are followed transitively
    (Q3 settles Q2, Q2 re-asked Q1)."""
    by_id = {q["id"]: q for r in rounds for q in r["questions"]}
    resolved = {}
    for r in rounds:
        for q in r["questions"]:
            if not is_resolved(q["answer"]):
                continue
            stack = list(q.get("resolves") or [])
            seen = set()
            while stack:
                rid = stack.pop()
                if rid in seen or rid not in by_id or rid == q["id"]:
                    continue
                seen.add(rid)
                resolved.setdefault(rid, q["id"])
                stack.extend(by_id[rid].get("resolves") or [])
    return resolved


def compute_unresolved(rounds):
    """Everything the interview could not settle; kept in state and requirements.md.

    skipped_questions     - HISTORY of every question that got no usable answer,
                            each marked resolved/resolved_by (stays in the record).
    open_questions        - the ACTIVE list: skipped and never settled later.
    not_applicable_questions - answered with an explicit 'not applicable'
                            (deliberate; not missing information).
    """
    last = rounds[-1]["review"] or {}
    resolution = _resolution_map(rounds)
    skipped, not_applicable = [], []
    for r in rounds:
        for q in r["questions"]:
            kind = answer_kind(q["answer"])
            if kind == "not_applicable":
                not_applicable.append({"round": r["number"], "id": q["id"], "category": q["category"],
                                       "question": q["question"], "answer": q["answer"]})
            elif kind == "missing":
                by = resolution.get(q["id"])
                skipped.append({"round": r["number"], "id": q["id"], "category": q["category"],
                                "question": q["question"], "resolved": by is not None, "resolved_by": by})
    return {
        "skipped_questions": skipped,
        "open_questions": [q for q in skipped if not q["resolved"]],
        "not_applicable_questions": not_applicable,
        "uncovered_categories": uncovered_categories(rounds),
        "remaining_gaps": last.get("remaining_gaps", []),
        "limit_reached": bool(last.get("forced")),
        "insufficient": bool(uncovered_categories(rounds)),
    }


def open_questions(unresolved):
    """Active unresolved questions. Falls back to the history list for states
    saved before `open_questions` existed."""
    un = unresolved or {}
    if "open_questions" in un:
        return un["open_questions"]
    return un.get("skipped_questions", [])


def active_requirements(requirements):
    """Copy of the requirements for prompts: the `unresolved` block keeps only ACTIVE
    gaps (resolved skipped questions stay in state/transcript but are not shown to
    later stages as problems)."""
    out = dict(requirements or {})
    un = out.get("unresolved")
    if isinstance(un, dict):
        out["unresolved"] = {"open_questions": open_questions(un),
                             "uncovered_categories": un.get("uncovered_categories", []),
                             "remaining_gaps": un.get("remaining_gaps", []),
                             "not_applicable_questions": un.get("not_applicable_questions", [])}
    return out


def _new_round(number, questions, known_ids=()):
    known = set(known_ids)
    return {
        "number": number,
        "kind": "initial" if number == 1 else "follow_up",
        "questions": [
            {"id": f"r{number}q{i}", "category": q["category"], "question": q["question"],
             "why": q.get("why", ""), "answer": None,
             "resolves": [x for x in q.get("resolves", []) if x in known]}
            for i, q in enumerate(questions, 1)
        ],
        "review": None,
    }


def _ask_pending(session, rnd, ui):
    pending = [q for q in rnd["questions"] if q["answer"] is None]
    if not pending:
        return
    total = len(rnd["questions"])
    ui.out(f"\n=== Interview round {rnd['number']} ({rnd['kind'].replace('_', '-')}): "
           f"{total} questions, {len(pending)} unanswered ===")
    ui.out("Answer on one line. Press Enter to skip a question. Ctrl-C/Ctrl-D pauses (progress is saved).")
    for q in pending:
        n = rnd["questions"].index(q) + 1
        ui.out(f"\n[{n}/{total}] ({CATEGORY_TITLES[q['category']]})\n{q['question']}")
        answer = ui.input().strip()
        q["answer"] = answer or SKIPPED
        session.save()


# --------------------------------------------------------------------- stage
def _conduct(session, llm, prompts, ui):
    iv = session.data["interview"]
    rounds = iv["rounds"]
    intake_json = json.dumps(session.data["intake"], indent=2, ensure_ascii=False)
    while True:
        if not rounds:
            qs = llm.complete_json(
                "interview:questions",
                prompts.render("interview_questions_system", categories=categories_text()),
                prompts.render("interview_questions_user", intake=intake_json,
                               categories=categories_text()),
                validator=validate_questions)
            rounds.append(_new_round(1, qs))
            session.save()
        cur = rounds[-1]
        _ask_pending(session, cur, ui)
        if cur["review"] is not None:       # reviewed earlier: interview is finished
            return
        n = len(rounds)
        final = n >= MAX_ROUNDS
        review_user = prompts.render("interview_review_user", intake=intake_json,
                                     categories=categories_text(), transcript=transcript(rounds),
                                     round_number=n, max_rounds=MAX_ROUNDS)
        review_user += "\n\n" + prompts.render("interview_review_skipped_note",
                                               skipped=skipped_listing(rounds))
        if final:       # last round: ask the model which gaps remain, but do not ask the user
            review_user += "\n\n" + prompts.render("interview_review_final_note")
        review = llm.complete_json(
            "interview:review",
            prompts.render("interview_review_system", categories=categories_text()),
            review_user,
            validator=partial(validate_review, first_round=(n == 1), final=final))
        if not final:
            review = apply_coverage_guard(review, rounds)
        stored = {"enough_information": review["enough_information"], "reasoning": review["reasoning"]}
        if review.get("overridden"):
            stored["overridden"] = True
        if final:
            if not review["enough_information"] or uncovered_categories(rounds):
                stored["forced"] = True
                stored["remaining_gaps"] = review["follow_up_questions"]
                stored["reasoning"] += (f" [Round limit ({MAX_ROUNDS}) reached; interview closed "
                                        "with unresolved requirements.]")
            cur["review"] = stored
            session.save()
            return
        cur["review"] = stored
        if not review["enough_information"]:
            known = [q["id"] for r in rounds for q in r["questions"]]
            rounds.append(_new_round(n + 1, review["follow_up_questions"], known))
        session.save()                      # review + next round saved together
        if review["enough_information"]:
            return


def carry_intake_statements(reqs, intake):
    """Copy the requirements and ambiguities extracted at intake VERBATIM into the requirements (code, no LLM), so a
    requirement the client stated cannot silently vanish when the interview summary paraphrases or omits it. Later stages
    see them in their prompts and requirements.md lists them. Only added when the intake has any."""
    stated = [(f"[{r['type']}] " if r.get("type") else "") + r["description"] for r in intake.get("requirements") or []]
    unclear = [a["description"] for a in intake.get("ambiguities") or []]
    if stated:
        reqs["intake_requirements"] = stated
    if unclear:
        reqs["intake_ambiguities"] = unclear
    return reqs


def render_requirements(session):
    req = session.data["requirements"]
    intake = session.data["intake"]
    iv = session.data["interview"]
    un = iv.get("unresolved") or {}
    parts = [f"# Requirements - {intake['project_name']}\n"]
    if un.get("insufficient") or un.get("limit_reached"):
        why = []
        if un.get("uncovered_categories"):
            why.append("no usable answers for: " + ", ".join(CATEGORY_TITLES[c] for c in un["uncovered_categories"]))
        if un.get("limit_reached"):
            why.append(f"the interview hit its round limit ({MAX_ROUNDS}) with open questions")
        parts += ["> **WARNING - INCOMPLETE REQUIREMENTS:** " + "; ".join(why)
                  + ". Later design stages must treat the items under 'Unresolved Requirements' as unknown.\n"]
    parts += ["## Summary\n", req["summary"] + "\n"]
    for c in req["categories"]:
        parts.append(f"## {CATEGORY_TITLES[c['id']]}\n")
        parts += [f"- {f}" for f in c["findings"]]
        parts.append("")
    if req.get("intake_requirements"):
        parts += ["## Requirements Stated in the Description (verbatim from intake)\n"] + [f"- {r}" for r in req["intake_requirements"]] + [""]
    if req.get("intake_ambiguities"):
        parts += ["## Ambiguities Flagged at Intake\n",
                  "_Each must be settled by an interview answer above or remain an open question / assumption in the design._\n"]
        parts += [f"- {a}" for a in req["intake_ambiguities"]] + [""]
    parts.append("## Open Issues\n")
    parts += [f"- {i}" for i in req["open_issues"]] if req["open_issues"] else ["_None._"]
    parts += ["", "## Unresolved Requirements\n"]
    open_qs = open_questions(un)
    if open_qs or un.get("remaining_gaps") or un.get("uncovered_categories"):
        for c in un.get("uncovered_categories", []):
            parts.append(f"- **No usable answer for category:** {CATEGORY_TITLES[c]}")
        for q in open_qs:
            parts.append(f"- Unanswered (round {q['round']}, {CATEGORY_TITLES[q['category']]}): {q['question']}")
        for g in un.get("remaining_gaps", []):
            parts.append(f"- Open gap identified at the round limit ({CATEGORY_TITLES[g['category']]}): {g['question']}")
    else:
        parts.append("_None._")
    last = iv["rounds"][-1]["review"]
    parts += ["", "## Interview Outcome\n",
              f"- Rounds conducted: {len(iv['rounds'])}",
              f"- Decision to stop: {last['reasoning']}", "", "## Interview Transcript\n"]
    for r in iv["rounds"]:
        parts.append(f"### Round {r['number']} ({r['kind'].replace('_', '-')})\n")
        for i, q in enumerate(r["questions"], 1):
            parts.append(f"{i}. **[{CATEGORY_TITLES[q['category']]}]** {q['question']}")
            parts.append(f"   - Answer: {q['answer']}")
        parts.append("")
    return "\n".join(parts)


def run(session, llm, prompts, ui):
    if not session.data.get("intake"):
        raise InvalidResponseError("Stage 1 (intake) must be completed first.", retryable=False)
    iv = session.data.setdefault("interview", {"status": "in_progress", "rounds": []})
    if iv["status"] != "complete":
        _conduct(session, llm, prompts, ui)
        iv["status"] = "complete"
        iv["unresolved"] = compute_unresolved(iv["rounds"])
        session.save()
    if "unresolved" not in iv:              # sessions saved before this field existed
        iv["unresolved"] = compute_unresolved(iv["rounds"])
    if not session.data.get("requirements"):
        user = prompts.render("interview_requirements_user",
                              intake=json.dumps(session.data["intake"], indent=2, ensure_ascii=False),
                              categories=categories_text(), transcript=transcript(iv["rounds"]))
        un = iv["unresolved"]
        if open_questions(un) or un["remaining_gaps"]:
            active = {"open_questions": open_questions(un), "uncovered_categories": un["uncovered_categories"],
                      "remaining_gaps": un["remaining_gaps"]}
            user += "\n\n" + prompts.render(
                "interview_unresolved_note", unresolved=json.dumps(active, indent=2, ensure_ascii=False))
        reqs = llm.complete_json(
            "interview:requirements",
            prompts.render("interview_requirements_system", categories=categories_text()),
            user, validator=validate_requirements)
        reqs["unresolved"] = un             # machine-readable gaps for later stages
        carry_intake_statements(reqs, session.data["intake"])
        session.data["requirements"] = reqs
        session.save()
    session.write_output("requirements.md", render_requirements(session))
    session.complete_stage(STAGE, NEXT_STAGE)
    session.data["last_error"] = None
    session.save()
    return session.data["requirements"]
