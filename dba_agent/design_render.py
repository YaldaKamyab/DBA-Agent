"""Deterministic renderers: canonical design -> Mermaid ER diagram / conceptual-model.md.

Nothing here calls the LLM, so the document can never disagree with design.json.
"""
import re

from .design_model import cardinality_text

_LEFT = {(0, 1): "|o", (1, 1): "||", (0, "N"): "}o", (1, "N"): "}|"}
_RIGHT = {(0, 1): "o|", (1, 1): "||", (0, "N"): "o{", (1, "N"): "|{"}
MERMAID_LINE = re.compile(
    r"^\s{4}[A-Za-z_][A-Za-z0-9_]*\s(?:\|o|\|\||\}o|\}\|)(?:--|\.\.)(?:o\||\|\||o\{|\|\{)\s"
    r"[A-Za-z_][A-Za-z0-9_]*\s:\s\"[^\"\n]*\"$")


def _q(text):
    """Text safe inside a Mermaid double-quoted string."""
    return re.sub(r"\s+", " ", str(text).replace('"', "'")).strip()


def _cell(text):
    return re.sub(r"\s+", " ", str(text).replace("|", "\\|")).strip()


def _key_marks(entity):
    marks = {}
    for ident in entity["identifiers"]:
        for a in ident["attributes"]:
            if ident["primary"]:
                marks[a] = "PK"
            elif len(ident["attributes"]) == 1 and a not in marks:
                marks[a] = "UK"
    return marks


def mermaid_er(design):
    """Mermaid erDiagram of the CONCEPTUAL model (M:N shown directly)."""
    ents = design["conceptual"]["entities"]
    lines = ["erDiagram"]
    for e in ents:
        lines.append(f"    {e['id']} {{")
        marks = _key_marks(e)
        for a in e["attributes"]:
            notes = [n for n in ("optional" if not a["required"] else "", "derived" if a["derived"] else "") if n]
            row = f"        {a['type']} {a['name']}"
            if a["name"] in marks:
                row += " " + marks[a["name"]]
            if notes:
                row += f' "{", ".join(notes)}"'
            lines.append(row)
        lines.append("    }")
    for r in design["conceptual"]["relationships"]:
        a, b = r["participants"]
        if r["type"] == "one_to_many" and a["cardinality"]["max"] == "N":
            a, b = b, a                                  # draw the "one" side first
        ka = (a["cardinality"]["min"], a["cardinality"]["max"])
        kb = (b["cardinality"]["min"], b["cardinality"]["max"])
        line = "--" if r["identifying"] else ".."
        label = _q(r["name"])
        if a["role"] or b["role"]:
            label += f" ({_q(a['role'] or '-')} / {_q(b['role'] or '-')})"
        lines.append(f"    {a['entity']} {_LEFT[ka]}{line}{_RIGHT[kb]} {b['entity']} : \"{label}\"")
    return "\n".join(lines)


def check_mermaid_syntax(text):
    """Structural check of the diagram we generate (the real Mermaid parser is
    only available in some environments). Returns a list of problems."""
    problems, entities, in_block = [], set(), False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "erDiagram":
        problems.append("first line must be 'erDiagram'")
    for n, line in enumerate(lines[1:], 2):
        if in_block:
            if line.strip() == "}":
                in_block = False
            elif not re.match(r"^\s{8}[A-Za-z][A-Za-z0-9_\-\[\]\(\)]* [A-Za-z_][A-Za-z0-9_\-]*( (PK|FK|UK)(, (PK|FK|UK))*)?( \"[^\"\n]*\")?$", line):
                problems.append(f"line {n}: bad attribute row: {line!r}")
        elif re.match(r"^\s{4}[A-Za-z_][A-Za-z0-9_]* \{$", line):
            entities.add(line.split()[0])
            in_block = True
        elif MERMAID_LINE.match(line):
            ends = {line.split()[0], line.split()[2]}
            if not ends <= entities:
                problems.append(f"line {n}: relationship uses undeclared entity: {line!r}")
        else:
            problems.append(f"line {n}: unrecognized: {line!r}")
    if in_block:
        problems.append("unterminated entity block")
    return problems


def relationship_sentences(r, ents):
    names = {e["id"]: e["name"] for e in ents}
    a, b = r["participants"]
    def one(x, y):
        role = f" (as {y['role']})" if y["role"] else ""
        return (f"Each {names[x['entity']]} is associated with {cardinality_text(y['cardinality'])} "
                f"{names[y['entity']]}{role}.")
    return [one(a, b), one(b, a)]


def render_conceptual_model(design):
    c = design["conceptual"]
    ents = c["entities"]
    name = {e["id"]: e["name"] for e in ents}
    out = [f"# Conceptual Model - {design['project_name']}\n",
           f"Target DBMS: **{design['dbms']['name']}** (the conceptual model is DBMS-independent). "
           "This document is generated from the canonical design (`design.json`).\n",
           "## Entities\n"]
    for e in ents:
        out.append(f"### {e['name']} (`{e['id']}`, {e['kind']} entity)\n")
        out.append(e["description"] + "\n")
        keys = []
        for i in e["identifiers"]:
            keys.append(f"{', '.join(i['attributes'])} ({'primary identifier' if i['primary'] else 'alternative identifier'})")
        out.append("**Key attributes:** " + "; ".join(keys) + "\n")
        if e["kind"] == "weak":
            out.append(f"**Identification:** weak entity, identified through relationship `{e['identifying_relationship']}` "
                       "together with its owner.\n")
        out += ["| Attribute | Type | Required | Unique | Description |", "|---|---|---|---|---|"]
        for a in e["attributes"]:
            desc = a["description"]
            if a["derived"]:
                desc = (desc + " Derived: " + a.get("derivation", "")).strip()
            if a.get("allowed_values"):
                desc = (desc + " Allowed values: " + ", ".join(a["allowed_values"]) + ".").strip()
            out.append(f"| `{a['name']}` | {a['type']} | {'yes' if a['required'] else 'no'} | "
                       f"{'yes' if a['unique'] else 'no'} | {_cell(desc)} |")
        out.append("")
    out += ["## Relationships and Cardinalities\n",
            "Cardinality reads across: the entry for an entity says how many of **that** entity relate to one instance "
            "of the other entity. Minimum 0 means optional participation, 1 means mandatory.\n"]
    if not c["relationships"]:
        out.append("_No relationships._\n")
    for r in c["relationships"]:
        a, b = r["participants"]
        typ = {"one_to_one": "one-to-one", "one_to_many": "one-to-many", "many_to_many": "many-to-many"}[r["type"]]
        out.append(f"### {r['name']} (`{r['id']}`): {name[a['entity']]} - {name[b['entity']]}\n")
        if r["description"]:
            out.append(r["description"] + "\n")
        out.append(f"- **Type:** {typ}" + (" (identifying)" if r["identifying"] else ""))
        for p in (a, b):
            role = f" as *{p['role']}*" if p["role"] else ""
            out.append(f"- **{name[p['entity']]}**{role}: {cardinality_text(p['cardinality'])} "
                       f"(min {p['cardinality']['min']}, max {p['cardinality']['max']})")
        out += [f"- {s}" for s in relationship_sentences(r, ents)]
        if r["attributes"]:
            out += ["", "Relationship attributes (stored with each link):", "",
                    "| Attribute | Type | Required | Description |", "|---|---|---|---|"]
            out += [f"| `{x['name']}` | {x['type']} | {'yes' if x['required'] else 'no'} | {_cell(x['description'])} |"
                    for x in r["attributes"]]
        out.append("")
    out += ["## ER Diagram\n",
            "Crow's-foot notation. Many-to-many relationships are drawn directly; their junction tables belong to the "
            "logical schema. Dashed lines are non-identifying relationships, solid lines identifying ones. "
            "`PK` marks the primary identifier, `UK` an alternative identifier.\n",
            "```mermaid", mermaid_er(design), "```\n"]
    out.append("## Business Rules\n")
    if design["business_rules"]:
        for r in design["business_rules"]:
            origin = "stated by the client" if r["source"] == "requirement" else "assumed"
            out.append(f"- **{r['id']}** ({origin}): {r['description']}")
    else:
        out.append("_None recorded._")
    out += ["", "## Assumptions\n"]
    if design["assumptions"]:
        for a in design["assumptions"]:
            why = f" _Rationale: {a['rationale']}_" if a["rationale"] else ""
            rel = f" (related to: {', '.join(a['related_to'])})" if a["related_to"] else ""
            out.append(f"- **{a['id']}:** {a['text']}{why}{rel}")
    else:
        out.append("_No assumptions were necessary._")
    out += ["", "## Remaining Ambiguities\n"]
    items = [f"**{q['id']}:** {q['text']}" + (f" _Impact: {q['impact']}_" if q["impact"] else "")
             for q in design["open_questions"]]
    items += [f"Unresolved from the interview: {g}" for g in design["requirement_gaps"]]
    out += [f"- {i}" for i in items] if items else ["_None._"]
    return "\n".join(out) + "\n"
