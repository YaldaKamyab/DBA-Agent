"""Stage 4B: deterministic renderers for logical-schema.md, normalization.md, assumptions.md
and a consistency checker for all four Stage 4 documents.

Nothing here calls the LLM: every document is rendered from the canonical design
(design.json) and the FD analysis (normalization_analysis.py), so the documents cannot
invent facts. The checker re-reads the files from disk and compares them with the design.

HONEST SCOPE of check_documents(): it detects documents that are missing, altered or out of
step with design.json (columns, types, nullability, keys, foreign keys and their actions,
CHECKs, indexes, relationships, diagrams, normal-form claims, assumptions). It does NOT prove
that the database design itself is correct or that the SQL will run - that needs a real DBMS.
"""
import re

from . import design_model as dm
from .design_render import _cell, check_mermaid_syntax, mermaid_er, render_conceptual_model
from .normalization_analysis import analyze_design, form_check

DOC_FILES = ["conceptual-model.md", "logical-schema.md", "normalization.md", "assumptions.md"]
LIMITATIONS = [
    "The analysis is only as complete as the functional dependencies listed in `design.json`. A dependency that "
    "was not declared cannot be detected, so \"no violation\" means \"no violation among the declared dependencies\" "
    "and is not a proof that the schema is fully normalized.",
    "Keys are taken from the declared PRIMARY KEY / UNIQUE constraints and unique indexes. A key that exists in the "
    "business but is not declared is unknown to the analysis.",
    "First normal form (atomic values) cannot be verified from a schema; only column types that usually hold "
    "multiple values (JSON, ARRAY, XML ...) are flagged.",
    "Functional dependencies are assumptions about the data written by the design step; they should be reviewed by a person.",
    "\"3NF proven\" means: complete candidate-key set, no violation among the declared dependencies. \"3NF unverified\" "
    "(e.g. key enumeration cut off) is neither a proof nor a violation. These automatic checks are a mechanical review, "
    "not a guarantee of semantic correctness.",
]


# ------------------------------------------------------------------ helpers
def _bt(items):
    return ", ".join(f"`{i}`" for i in items)


def _row(*cells):
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def parse_row(line):
    """Cells of a Markdown table row; backticks removed, escaped pipes restored."""
    inner = line.strip()[1:-1]
    return [c.strip().replace("\\|", "|").replace("`", "") for c in re.split(r"(?<!\\)\|", inner)]


def _norm(text):
    """Comparison form of a cell: what parse_row() gives back for what _row() wrote."""
    return re.sub(r"\s+", " ", str(text)).strip().replace("`", "")


def _table_block(title, header, rows):
    out = [f"**{title}**", ""]
    if not rows:
        return out + ["_None._", ""]
    return out + [_row(*header), "|" + "---|" * len(header)] + [_row(*r) for r in rows] + [""]


def _key_cell(t, col):
    marks = []
    if col in t["primary_key"]:
        marks.append("PK")
    if any(col in fk["columns"] for fk in t["foreign_keys"]):
        marks.append("FK")
    if any(u["columns"] == [col] for u in t["unique_constraints"]) or any(
            i["unique"] and not i.get("where") and i["columns"] == [col] for i in t["indexes"]):
        marks.append("UNIQUE")
    return ", ".join(marks) or "-"


def _unique_cell(i):
    """'yes' / 'no', plus the structured condition of a partial index (so the document shows it is NOT a full key)."""
    base = "yes" if i["unique"] else "no"
    return f"{base} (only where {dm.where_text(i['where'])})" if i.get("where") else base


def _trigger_target(tr):
    return ", ".join(tr["operations"]) if tr["kind"] == "append_only" else "UPDATE of " + _bt(tr["columns"])


def _default_cell(c):
    bits = (["IDENTITY"] if c["identity"] else []) + ([f"DEFAULT {c['default']}"] if c["default"] else [])
    return ", ".join(bits) or "-"


def _origin(t):
    return (f"Implements entity `{t['source']['id']}`." if t["kind"] == "entity"
            else f"Junction table for the many-to-many relationship `{t['source']['id']}`.")


def _impl_text(r):
    i = r["implemented_by"]
    if i["kind"] == "junction_table":
        return f"junction table `{i['table']}` (foreign keys {', '.join(i['foreign_keys'])})"
    return f"foreign key `{i['foreign_keys'][0]}` on table `{i['table']}` (columns {', '.join(i['columns'])})"


# ------------------------------------------- relational schema diagram (Mermaid)
def _mtype(data_type):
    t = re.sub(r"[^A-Za-z0-9_\-\[\]\(\)]", "_", re.sub(r"\s+", "_", data_type.strip()))
    t = re.sub(r"_+", "_", t).strip("_")
    return t if re.match(r"^[A-Za-z_]", t) else "T_" + t


def mermaid_relational(design):
    tables = design["physical"]["tables"]
    lines = ["erDiagram"]
    for t in tables:
        lines.append(f"    {t['name']} {{")
        for c in t["columns"]:
            keys = [k.replace("UNIQUE", "UK") for k in _key_cell(t, c["name"]).split(", ") if k != "-"]
            row = f"        {_mtype(c['data_type'])} {c['name']}"
            if keys:
                row += " " + ", ".join(keys)
            notes = [n for n in ("nullable" if c["nullable"] else "", "identity" if c["identity"] else "") if n]
            if notes:
                row += f' "{", ".join(notes)}"'
            lines.append(row)
        lines.append("    }")
    for t in tables:
        for fk in t["foreign_keys"]:
            nullable = any(c["nullable"] for c in t["columns"] if c["name"] in fk["columns"])
            parent = "|o" if nullable else "||"
            child = "o|" if dm._is_unique(t, fk["columns"]) else "o{"
            line = "--" if set(fk["columns"]) <= set(t["primary_key"]) else ".."
            lines.append(f"    {fk['ref_table']} {parent}{line}{child} {t['name']} : \"{fk['name']}\"")
    return "\n".join(lines)


# ------------------------------------------------------------ logical-schema.md
def render_logical_schema(design):
    phys = design["physical"]
    out = [f"# Logical Schema - {design['project_name']}\n",
           f"Target DBMS: **{design['dbms']['name']}**. Data types, constraints and indexes below are the ones "
           "`schema.sql` will use. This document is generated from the canonical design (`design.json`).\n",
           "## Tables\n"]
    for t in phys["tables"]:
        out += [f"### `{t['name']}`\n", _origin(t) + (" " + t["description"] if t["description"] else ""), ""]
        out += _table_block("Columns", ["Column", "Data type", "Nullable", "Default", "Key", "Description"],
                            [[f"`{c['name']}`", f"`{c['data_type']}`", "NULL" if c["nullable"] else "NOT NULL",
                              _default_cell(c), _key_cell(t, c["name"]), c["description"] or "-"] for c in t["columns"]])
        out += [f"**Primary key:** `{t['primary_key_name'] or '(unnamed)'}` ({_bt(t['primary_key'])})", ""]
        out += _table_block("Foreign keys", ["Name", "Columns", "References", "ON DELETE", "ON UPDATE", "Relationship"],
                            [[f"`{f['name']}`", _bt(f["columns"]), f"`{f['ref_table']}` ({_bt(f['ref_columns'])})",
                              f["on_delete"], f["on_update"], f["relationship"] or "-"] for f in t["foreign_keys"]])
        out += _table_block("Unique constraints", ["Name", "Columns"],
                            [[f"`{u['name']}`", _bt(u["columns"])] for u in t["unique_constraints"]])
        out += _table_block("Check constraints", ["Name", "Expression", "Description"],
                            [[f"`{c['name']}`", f"`{c['expression']}`", c["description"] or "-"] for c in t["check_constraints"]])
        out += _table_block("Indexes", ["Name", "Columns", "Unique", "Purpose"],
                            [[f"`{i['name']}`", _bt(i["columns"]), _unique_cell(i), i["purpose"] or "-"]
                             for i in t["indexes"]])
        if t.get("triggers"):                                 # block only when used: old documents stay unchanged
            out += _table_block("Triggers", ["Name", "Kind", "Applies to", "Description"],
                                [[f"`{x['name']}`", x["kind"].replace("_", " "), _trigger_target(x), x["description"] or "-"]
                                 for x in t["triggers"]])
    out += ["## Relationship Implementation\n",
            "How each relationship of the conceptual model is stored. Many-to-many relationships become junction tables.\n"]
    rels = design["conceptual"]["relationships"]
    out += ([_row("Relationship", "Type", "Implemented by"), "|---|---|---|"] +
            [_row(f"`{r['id']}`", r["type"].replace("_", "-"), _impl_text(r)) for r in rels]) if rels else ["_No relationships._"]
    out += ["", "## Business Rule Enforcement\n"]
    rules = design["business_rules"]
    if rules:
        out += [_row("Rule", "Mechanism", "Implemented by", "Note", "Description"), "|---|---|---|---|---|"]
        out += [_row(f"`{r['id']}`", r["enforcement"]["mechanism"], ", ".join(r["enforcement"]["implemented_by"]) or "-",
                     r["enforcement"]["note"] or "-", r["description"]) for r in rules]
    else:
        out.append("_No business rules recorded._")
    out += ["", "## Relational Schema Diagram\n",
            "One box per table. `PK` primary key, `FK` foreign key, `UK` single-column unique key. Solid lines: the foreign key is "
            "part of the child's primary key; dashed lines: it is not. A circle on the parent side means the foreign key is nullable.\n",
            "```mermaid", mermaid_relational(design), "```\n"]
    return "\n".join(out)


# ------------------------------------------------------------ normalization.md
def _fd_line(det, dep):
    return f"{', '.join(f'`{c}`' for c in det)} -> {', '.join(f'`{c}`' for c in dep)}"


def render_normalization(design, analysis=None):
    analysis = analysis or analyze_design(design)
    phys = design["physical"]
    out = [f"# Normalization Analysis - {design['project_name']}\n",
           "Functional dependencies (FDs) come from the design step; keys come from the declared PRIMARY KEY / UNIQUE "
           "constraints. For each table the attribute closure of every determinant was computed to find candidate keys "
           "and to test 2NF, 3NF and BCNF. See *Limitations* at the end.\n",
           "## Summary\n", _row("Table", "Candidate keys", "Highest normal form", "Status"), "|---|---|---|---|"]
    for t in phys["tables"]:
        a = analysis[t["name"]]
        out.append(_row(f"`{t['name']}`", "; ".join(f"({', '.join(k)})" for k in a["candidate_keys"]) or "none",
                        a["highest"], a["status"]))
    out += ["", "## Per-Table Analysis\n"]
    for t in phys["tables"]:
        a = analysis[t["name"]]
        out += [f"### `{t['name']}`\n", f"**Columns:** {_bt(a['columns'])}", "", "**Declared functional dependencies:**", ""]
        out += [f"- {_fd_line(l, r)}" for l, r in a["fds"]] or ["- _none_"]
        out += ["", f"**Candidate keys:** " + ("; ".join(f"({', '.join(k)})" for k in a["candidate_keys"]) or "none"),
                f"**Prime attributes:** {_bt(a['prime']) or 'none'}",
                f"**Non-prime attributes:** {_bt(a['nonprime']) or 'none'}", ""]
        flagged = a["non_atomic_columns"]
        out.append("- **1NF:** " + (f"check needed - column(s) {_bt(flagged)} may hold several values per cell." if flagged
                                     else "assumed satisfied (scalar column types; atomicity cannot be proven from a schema)."))
        for form in ("2NF", "3NF", "BCNF"):
            state = form_check(a, form)
            out.append(f"- **{form}:** {state}")
        out.append(f"- **3NF verdict:** {a['nf3_status']}" + (" (not proven and not refuted: see the uncertain findings below)"
                                                            if a["nf3_status"] == "unverified" else ""))
        out.append("")
        for f in a["findings"]:
            out.append(f"- Finding ({f['form']}): {f['message']}")
        out += [f"- Note: {n}" for n in a["notes"]]
        out += [f"**Conclusion:** highest normal form detected: **{a['highest']}** ({a['status']}).", ""]
    out += ["## Deliberate Denormalizations\n"]
    if phys["denormalizations"]:
        for d in phys["denormalizations"]:
            out.append(f"- **`{d['table']}`**: {d['description'] or 'denormalized design'} _Justification: {d['justification']}_")
    else:
        out.append("_None. The schema is not deliberately denormalized._")
    unjustified = [n for n, a in analysis.items() if a["unjustified"]]
    unverified = [n for n, a in analysis.items() if not a["unjustified"] and a["uncertain"]]
    out += ["", "## Warnings\n"]
    out += ([f"- Table `{n}` is below 3NF according to the declared dependencies and has no justification in the design." for n in unjustified]
            + [f"- Table `{n}`: the normal-form verdict could not be fully confirmed ({len(analysis[n]['uncertain'])} uncertain finding(s) above)."
               for n in unverified]
            or ["_None._"])
    out += ["", "## Limitations\n"] + [f"- {x}" for x in LIMITATIONS]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- assumptions.md
def design_warnings(design, analysis=None):
    analysis = analysis or analyze_design(design)
    warns = [f"Table `{n}` is below 3NF according to the declared dependencies and has no justification in the design."
             for n, a in analysis.items() if a["unjustified"]]
    warns += [f"Table `{n}`: {f['message']}" for n, a in analysis.items() for f in a["findings"] if f["form"] in ("KEY", "UNCERTAIN")]
    return warns


def assumptions_sections(design, analysis=None):
    """{section title: [bullet lines]} - shared by the renderer and the checker."""
    sections = {}
    sections["Assumptions"] = [f"**{a['id']}:** {a['text']}" + (f" _Rationale: {a['rationale']}_" if a["rationale"] else "") +
                               (f" (related to: {', '.join(a['related_to'])})" if a["related_to"] else "")
                               for a in design["assumptions"]]
    rules = design["business_rules"]
    sections["Assumed Business Rules"] = [f"**{r['id']}:** {r['description']}" for r in rules if r["source"] == "assumption"]
    sections["Rules Not Fully Enforced by the Schema"] = [
        f"**{r['id']}** ({r['enforcement']['mechanism']}): {r['description']} _How: {r['enforcement']['note']}_"
        for r in rules if r["enforcement"]["mechanism"] != "schema"]
    sections["Remaining Ambiguities"] = [f"**{q['id']}:** {q['text']}" + (f" _Impact: {q['impact']}_" if q["impact"] else "")
                                         for q in design["open_questions"]]
    sections["Unresolved Requirements from the Interview"] = list(design["requirement_gaps"])
    sections["Design Review Warnings"] = design_warnings(design, analysis)
    return sections


def render_assumptions(design, analysis=None):
    out = [f"# Assumptions and Open Issues - {design['project_name']}\n",
           "Everything the design step had to assume, plus what is still unclear. Generated from `design.json`.\n"]
    empty = {"Assumptions": "_No assumptions were necessary._", "Assumed Business Rules": "_None._",
             "Rules Not Fully Enforced by the Schema": "_None. Every business rule is enforced by the schema._",
             "Remaining Ambiguities": "_None._", "Unresolved Requirements from the Interview": "_None._",
             "Design Review Warnings": "_None._"}
    for title, items in assumptions_sections(design, analysis).items():
        out += [f"## {title}\n"] + ([f"- {i}" for i in items] or [empty[title]]) + [""]
    return "\n".join(out)


# ------------------------------------------------------------------ all documents
def render_all(design):
    analysis = analyze_design(design)
    return {"conceptual-model.md": render_conceptual_model(design),
            "logical-schema.md": render_logical_schema(design),
            "normalization.md": render_normalization(design, analysis),
            "assumptions.md": render_assumptions(design, analysis)}


# ====================================================================== checker
def _sections(text, level="### "):
    """{heading text: [lines]} for headings of the given level."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith(level):
            cur = line[len(level):].strip().strip("`")
            out[cur] = []
        elif line.startswith("## ") and level == "### ":
            cur = None
        elif cur is not None:
            out[cur].append(line)
    return out


def _blocks(lines):
    """Split a table section into {bold label: [row cells]} using the '**Label**' lines."""
    blocks, cur = {}, None
    for line in lines:
        m = re.match(r"^\*\*([^*]+)\*\*$", line)
        if m:
            cur = m.group(1)
            blocks[cur] = []
        elif cur and line.startswith("| `"):
            blocks[cur].append(parse_row(line))
    return blocks


def _mermaid(text):
    m = re.search(r"```mermaid\n(.*?)\n```", text, re.DOTALL)
    return m.group(1) if m else None


def _diagram_entities(diagram):
    ents, cur = {}, None
    for line in diagram.splitlines()[1:]:
        m = re.match(r"^\s{4}(\w+) \{$", line)
        if m:
            cur = ents.setdefault(m.group(1), [])
        elif line.strip() == "}":
            cur = None
        elif cur is not None and line.startswith("        "):
            cur.append(line.split()[1])
    return ents


def _diff(label, expected, actual, problems, fmt=str):
    expected, actual = list(expected), list(actual)
    for x in expected:
        if x not in actual:
            problems.append(f"{label}: expected {fmt(x)} is missing or differs from design.json.")
    for x in actual:
        if x not in expected:
            problems.append(f"{label}: {fmt(x)} is not in design.json.")


def _check_conceptual(design, text, problems):
    c = design["conceptual"]
    secs = _sections(text)
    ids = {h.split("(`")[1].split("`")[0] for h in secs if "(`" in h}
    expected = {e["id"] for e in c["entities"]} | {r["id"] for r in c["relationships"]}
    _diff("conceptual-model.md sections", sorted(expected), sorted(ids), problems)
    d = _mermaid(text)
    if d is None:
        problems.append("conceptual-model.md: no Mermaid ER diagram.")
        return
    problems += [f"conceptual-model.md diagram: {p}" for p in check_mermaid_syntax(d)]
    if d != mermaid_er(design):
        _diff("conceptual-model.md diagram entities",
              {e["id"]: [a["name"] for a in e["attributes"]] for e in c["entities"]}.items(),
              _diagram_entities(d).items(), problems)
        n_rel = len([ln for ln in d.splitlines() if re.search(r"--|\.\.", ln) and ":" in ln])
        if n_rel != len(c["relationships"]):
            problems.append(f"conceptual-model.md diagram: {n_rel} relationship line(s), design has {len(c['relationships'])}.")


def _check_logical(design, text, problems):
    secs = _sections(text)
    tables = design["physical"]["tables"]
    _diff("logical-schema.md tables", [t["name"] for t in tables], list(secs), problems)
    for t in tables:
        if t["name"] not in secs:
            continue
        b = _blocks(secs[t["name"]])
        w = f"logical-schema.md table `{t['name']}`"
        _diff(f"{w} columns",
              [(c["name"], c["data_type"], "NULL" if c["nullable"] else "NOT NULL", _norm(_default_cell(c)), _key_cell(t, c["name"]))
               for c in t["columns"]],
              [(r[0], r[1], r[2], r[3], r[4]) for r in b.get("Columns", [])], problems)
        pk = re.search(r"\*\*Primary key:\*\* `([^`]*)` \((.*)\)", "\n".join(secs[t["name"]]))
        pk_cols = re.findall(r"`([^`]+)`", pk.group(2)) if pk else None
        if pk_cols != t["primary_key"] or (pk and pk.group(1) != (t["primary_key_name"] or "(unnamed)")):
            problems.append(f"{w}: primary key differs from design.json {t['primary_key']}.")
        _diff(f"{w} foreign keys",
              [(f["name"], ", ".join(f["columns"]), f"{f['ref_table']} ({', '.join(f['ref_columns'])})", f["on_delete"], f["on_update"],
                f["relationship"] or "-") for f in t["foreign_keys"]],
              [tuple(r[:6]) for r in b.get("Foreign keys", [])], problems)
        _diff(f"{w} unique constraints", [(u["name"], ", ".join(u["columns"])) for u in t["unique_constraints"]],
              [tuple(r[:2]) for r in b.get("Unique constraints", [])], problems)
        _diff(f"{w} check constraints", [(c["name"], _norm(c["expression"])) for c in t["check_constraints"]],
              [(r[0], r[1]) for r in b.get("Check constraints", [])], problems)
        _diff(f"{w} indexes", [(i["name"], ", ".join(i["columns"]), _norm(_unique_cell(i))) for i in t["indexes"]],
              [tuple(r[:3]) for r in b.get("Indexes", [])], problems)
        _diff(f"{w} triggers", [(x["name"], x["kind"].replace("_", " "), _norm(_trigger_target(x))) for x in t.get("triggers", [])],
              [tuple(r[:3]) for r in b.get("Triggers", [])], problems)
    # relationship and rule tables
    rows = [parse_row(ln) for ln in text.splitlines() if ln.startswith("| `")]
    rel = {r["id"]: r for r in design["conceptual"]["relationships"]}
    got = {r[0]: r for r in rows if r[0] in rel}
    for rid, r in rel.items():
        if rid not in got:
            problems.append(f"logical-schema.md: relationship `{rid}` is missing from the implementation table.")
        elif got[rid][2] != _norm(_impl_text(r)):
            problems.append(f"logical-schema.md: relationship `{rid}` implementation differs from design.json.")
    for r in design["business_rules"]:
        row = next((x for x in rows if x[0] == r["id"] and len(x) == 5), None)
        if row is None or row[1] != r["enforcement"]["mechanism"] or row[2] != (", ".join(r["enforcement"]["implemented_by"]) or "-"):
            problems.append(f"logical-schema.md: enforcement of business rule `{r['id']}` is missing or differs from design.json.")
    d = _mermaid(text)
    if d is None:
        problems.append("logical-schema.md: no relational schema Mermaid diagram.")
        return
    problems += [f"logical-schema.md diagram: {p}" for p in check_mermaid_syntax(d)]
    ents = _diagram_entities(d)
    _diff("logical-schema.md diagram tables", [t["name"] for t in tables], list(ents), problems)
    for t in tables:
        if t["name"] in ents:
            _diff(f"logical-schema.md diagram columns of `{t['name']}`", [c["name"] for c in t["columns"]], ents[t["name"]], problems)
    n_fk = sum(len(t["foreign_keys"]) for t in tables)
    n_lines = len([ln for ln in d.splitlines() if re.match(r"^\s{4}\w+ \S+ \w+ : \"", ln) or re.match(r"^\s{4}\w+ [|}][o|](--|\.\.)", ln)])
    if n_lines != n_fk:
        problems.append(f"logical-schema.md diagram: {n_lines} relationship line(s), design has {n_fk} foreign key(s).")


def _check_normalization(design, text, problems):
    analysis = analyze_design(design)
    rows = [parse_row(ln) for ln in text.splitlines() if ln.startswith("| `")]
    summary = {r[0]: r for r in rows if len(r) == 4}
    for name, a in analysis.items():
        want = ("; ".join(f"({', '.join(k)})" for k in a["candidate_keys"]) or "none", a["highest"], a["status"])
        got = tuple(summary[name][1:]) if name in summary else None
        if got != want:
            problems.append(f"normalization.md summary for `{name}`: found {got}, design.json analysis gives {want}.")
    secs = _sections(text)
    for name, a in analysis.items():
        body = secs.get(name)
        if body is None:
            problems.append(f"normalization.md: no analysis section for table `{name}`.")
            continue
        joined = "\n".join(body)
        for form in ("2NF", "3NF", "BCNF"):
            m = re.search(rf"^- \*\*{form}:\*\* (\w+)", joined, re.M)
            if not m or m.group(1) != form_check(a, form):
                problems.append(f"normalization.md `{name}`: {form} verdict is '{m.group(1) if m else 'missing'}', analysis says {form_check(a, form)}.")
        fds = sorted(ln[2:] for ln in body if ln.startswith("- ") and " -> " in ln
                      and not ln.startswith(("- Finding (", "- Note:", "- **")))   # findings may mention "->"
        if fds != sorted(_fd_line(l, r) for l, r in a["fds"]):
            problems.append(f"normalization.md `{name}`: listed functional dependencies differ from design.json.")
        m = re.search(r"^\*\*Candidate keys:\*\* (.*)$", joined, re.M)
        if not m or m.group(1) != ("; ".join(f"({', '.join(k)})" for k in a["candidate_keys"]) or "none"):
            problems.append(f"normalization.md `{name}`: candidate keys differ from the analysis of design.json.")
        m = re.search(r"^\*\*Columns:\*\* (.*)$", joined, re.M)
        if not m or m.group(1) != _bt(a["columns"]):
            problems.append(f"normalization.md `{name}`: column list differs from design.json.")
    _diff("normalization.md", sorted(analysis), sorted(secs), problems)
    dn = [ln for ln in text.splitlines() if ln.startswith("- **`")]
    _diff("normalization.md denormalizations", [f"{d['table']}|{d['justification']}" for d in design["physical"]["denormalizations"]],
          [f"{re.match(r'- \*\*`([^`]+)`', ln).group(1)}|{re.search(r'_Justification: (.*)_$', ln).group(1) if '_Justification:' in ln else ''}" for ln in dn],
          problems)
    if "## Limitations" not in text:
        problems.append("normalization.md: the Limitations section is missing.")


def _check_assumptions(design, text, problems):
    expected = assumptions_sections(design)
    secs = _sections(text, "## ")
    for title, items in expected.items():
        if title not in secs:
            problems.append(f"assumptions.md: section '{title}' is missing.")
            continue
        got = [ln[2:] for ln in secs[title] if ln.startswith("- ")]
        _diff(f"assumptions.md '{title}'", items, got, problems)


def check_documents(design, docs):
    """Compare the four Stage 4 documents with the canonical design.

    docs: {filename: text} (a missing file is simply absent). Returns a list of problem strings
    (empty list = no difference found). See the module docstring for the limits of this check.
    """
    problems = []
    for name in DOC_FILES:
        text = docs.get(name)
        if text is None or not text.strip():
            problems.append(f"{name}: file is missing or empty.")
    present = {n: docs[n] for n in DOC_FILES if docs.get(n, "").strip()}
    checks = {"conceptual-model.md": _check_conceptual, "logical-schema.md": _check_logical,
              "normalization.md": _check_normalization, "assumptions.md": _check_assumptions}
    expected = render_all(design)
    for name, fn in checks.items():
        if name not in present:
            continue
        found = []
        fn(design, present[name], found)
        if not found and present[name] != expected[name]:           # safety net: any other change
            exp, act = expected[name].splitlines(), present[name].splitlines()
            first = next((a for a, e in zip(act + [""] * len(exp), exp + [""] * len(act)) if a != e), "")
            found.append(f"{name}: content differs from the rendering of design.json (first differing line: {first!r}).")
        problems += found
    for t in design["physical"]["tables"]:                                 # cross-document
        for name in ("logical-schema.md", "normalization.md"):
            if name in present and f"`{t['name']}`" not in present[name]:
                problems.append(f"{name}: table `{t['name']}` is not mentioned.")
    return problems
