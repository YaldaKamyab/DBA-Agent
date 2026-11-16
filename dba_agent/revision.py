"""Stage 5A - revision engine.

A natural-language change request is interpreted by the LLM (one call, prompts/revision_*.md), which
returns the complete revised canonical design. Code then:

  1. validates the WHOLE candidate (design_model.validate_design + link_relationships, DBMS id/types, no-op check);
  2. computes what changed by diffing old and new design (the changelog never relies on the LLM's own claims
     about affected items);
  3. re-renders all four Stage 4 documents from the candidate (design_docs.render_all - never edited by hand),
     checks them in memory, writes them atomically, reads them back and runs check_documents again;
  4. writes changelog.md (rendered from the full revision history in session state) and commits
     design / history to session state.

design.json is the single source of truth. Failure at any point before the commit leaves design, documents and
history untouched; a failure while writing files restores the previous files. An interrupted revision is kept in
session.data["revision_pending"] (request + validated candidate) and finished by the next `revise` call.
"""
import copy
import json
import os
import re
from pathlib import Path

from . import design_model as dm, versions
from .approval import design_fingerprint
from .design_docs import DOC_FILES, check_documents, design_warnings, render_all
from .errors import DBAAgentError, InvalidResponseError, MalformedResponseError, ProviderError
from .normalization_analysis import analyze_design
from .stages import dbms as dbms_stage
from .stages.dbms import DBMS_IDS, DBMS_NAMES, dbms_id
from .stages.design import _dialect_notes
from .state import atomic_write_text, now_iso

CHANGELOG = "changelog.md"
DESIGN_JSON = "design.json"
DBMS_CHOICE_MD = "dbms-choice.md"
REGENERATED = [DESIGN_JSON] + DOC_FILES
CHANGE_TYPES = ("add_entity", "remove_entity", "add_attribute", "remove_attribute", "change_attribute",
                "change_cardinality", "add_relationship", "remove_relationship", "add_business_rule",
                "change_constraint", "switch_dbms", "other")
MAX_PROBLEMS = 15
_SECTIONS = ("conceptual", "physical", "business_rules", "assumptions", "open_questions")


class RecoveryRequired(DBAAgentError):
    """The saved state and the files on disk disagree in a way that cannot be repaired with certainty. Nothing is
    changed or guessed; the message says what to check (see `python -m dba_agent recover`)."""


class RevisionRejected(DBAAgentError):
    """The request was understood but cannot / should not be applied (design unchanged)."""


# ------------------------------------------------------------------ interpretation
def _strs(v):
    v = v if isinstance(v, list) else ([] if v in (None, "") else [v])
    return [str(x).strip() for x in v if str(x).strip()]


def build_candidate(current, response):
    """Assemble + validate the revised design from the LLM response. Raises InvalidResponseError
    (fed back to the LLM for a retry) or RevisionRejected (LLM says the request cannot be applied)."""
    if not isinstance(response, dict):
        raise InvalidResponseError("The answer must be a JSON object.")
    rev = response.get("revision")
    if not isinstance(rev, dict):
        raise InvalidResponseError("Missing 'revision' object (applicable, change_types, summary, rationale, ...).")
    if rev.get("applicable") is False:
        raise RevisionRejected("The request cannot be applied: " + (str(rev.get("reason") or "").strip() or "no reason given."))
    summary, rationale = str(rev.get("summary") or "").strip(), str(rev.get("rationale") or "").strip()
    if not summary or not rationale:
        raise InvalidResponseError("'revision.summary' and 'revision.rationale' are required.")
    types = [t for t in _strs(rev.get("change_types"))]
    bad = [t for t in types if t not in CHANGE_TYPES]
    if bad or not types:
        raise InvalidResponseError(f"'revision.change_types' must be a non-empty list of: {', '.join(CHANGE_TYPES)} (got {bad or 'nothing'}).")
    missing = [k for k in _SECTIONS if k not in response]
    if missing:
        raise InvalidResponseError(f"Missing design sections: {', '.join(missing)}. Return the COMPLETE revised design.")
    cand = copy.deepcopy(current)
    for k in _SECTIONS:
        cand[k] = copy.deepcopy(response[k])
    new_dbms = response.get("dbms") or current["dbms"]
    if not isinstance(new_dbms, dict):
        raise InvalidResponseError("'dbms' must be an object {id, name}.")
    new_id = dbms_id(new_dbms.get("id"))                       # InvalidResponseError for unknown ids
    cand["dbms"] = {"id": new_id, "name": DBMS_NAMES[new_id]}
    switched = new_id != current["dbms"]["id"]
    if switched and "switch_dbms" not in types:
        raise InvalidResponseError("'dbms' changed but 'switch_dbms' is not in revision.change_types (or the DBMS changed by mistake).")
    if "switch_dbms" in types and not switched:
        raise InvalidResponseError("'switch_dbms' was reported but 'dbms.id' is unchanged; set the new DBMS and rewrite all types.")
    cand = dm.link_relationships(dm.validate_design(cand))     # whole-design check (types are checked for cand's DBMS)
    if switched:
        _check_dbms_switch(current, cand)
        if cand["physical"] == current["physical"] and cand["assumptions"] == current["assumptions"]:
            raise InvalidResponseError("A DBMS switch must rewrite the physical design (data types, identity/auto-increment, "
                                       "CHECK expressions, indexes) and record dialect assumptions; only the DBMS name changed.")
    if _core(cand) == _core(current):
        raise InvalidResponseError("The returned design is identical to the current design; the requested change was not applied.")
    cand["_revision"] = {"change_types": types, "summary": summary, "rationale": rationale,
                         "assumptions": _strs(rev.get("assumptions")), "warnings": _strs(rev.get("warnings"))}
    return cand


def _core(design):
    return {k: design[k] for k in ("dbms", "conceptual", "physical", "business_rules", "assumptions", "open_questions")}


def _check_dbms_switch(old, new):
    """After a switch no column may keep a type spelled only in ANOTHER supported DBMS (e.g. SERIAL/JSONB left in a
    SQL Server design), and a switch must not silently drop business rules. (Type validity for the new DBMS itself
    and foreign-key type compatibility are already checked by dm.validate_design.)"""
    errs = [f"{t['name']}.{c['name']}: type {c['data_type']!r} is not a {new['dbms']['name']} type."
            for t in new["physical"]["tables"] for c in t["columns"] if _foreign_type(c["data_type"], new["dbms"]["id"])]
    if errs:
        raise InvalidResponseError("DBMS switch is incomplete:\n" + "\n".join(f"- {e}" for e in errs[:MAX_PROBLEMS]))
    lost = {r["id"] for r in old["business_rules"]} - {r["id"] for r in new["business_rules"]}
    if lost:
        raise InvalidResponseError(f"A DBMS switch must not remove business rules (removed: {', '.join(sorted(lost))}).")


# type spellings that exist ONLY in one dialect (not valid in the other supported ones)
_ONLY = {"postgresql": ("serial", "bigserial", "smallserial", "timestamptz", "jsonb", "bytea"),
         "mysql_mariadb": ("mediumint", "longtext", "mediumtext", "enum(", "datetime("),
         "sql_server": ("nvarchar", "nchar", "ntext", "uniqueidentifier", "datetime2", "datetimeoffset")}


def _foreign_type(data_type, dbms):
    """True when the type is spelled the way only a DIFFERENT supported DBMS spells it."""
    t = data_type.strip().lower()
    if dbms == "sqlite":
        return False
    mine = _ONLY.get(dbms, ())
    if any(t.startswith(w) for w in mine):
        return False
    if "unsigned" in t and dbms != "mysql_mariadb":
        return True
    return any(t.startswith(w) for other, ws in _ONLY.items() if other != dbms for w in ws)


# ------------------------------------------------------------------ diff
def _named(items, key="name"):
    return {i[key]: i for i in items}


def _constraints(design):
    out = {}
    for t in design["physical"]["tables"]:
        if t["primary_key"]:
            out[t["primary_key_name"]] = ("primary key", t["name"], t["primary_key"])
        # the complete structured item is compared (not a hand-picked subset), so a change nested anywhere inside a
        # constraint - FK action, CHECK expression, conditional index `where`/`conditional`, trigger body - is a difference
        for fk in t["foreign_keys"]:
            out[fk["name"]] = ("foreign key", t["name"], fk)
        for u in t["unique_constraints"]:
            out[u["name"]] = ("unique", t["name"], u)
        for c in t["check_constraints"]:
            out[c["name"]] = ("check", t["name"], c)
        for i in t["indexes"]:
            out[i["name"]] = ("index", t["name"], i)
        for tr in t.get("triggers", []):
            out[tr["name"]] = ("trigger", t["name"], tr)
    return out


def _fd_label(f):
    return f"{f['table']}: ({', '.join(sorted(f['determinant']))}) -> ({', '.join(f['dependent'])})"


def _fd_map(design):
    """{key: fd}. Functional dependencies have no ids: the key is table + determinant set (a second FD with the same
    table/determinant gets '#2', '#3' ... in order of appearance, so duplicates are still compared one by one)."""
    out, seen = {}, {}
    for f in design["physical"].get("functional_dependencies", []):
        base = f"{f['table']}: ({', '.join(sorted(f['determinant']))})"
        seen[base] = seen.get(base, 0) + 1
        out[base if seen[base] == 1 else f"{base}#{seen[base]}"] = f
    return out


def _denorm_map(design):
    out, seen = {}, {}
    for d in design["physical"].get("denormalizations", []):
        base = f"{d['table']}: {d.get('description', '')}"
        seen[base] = seen.get(base, 0) + 1
        out[base if seen[base] == 1 else f"{base}#{seen[base]}"] = d
    return out


_TABLE_STRUCT = {"columns", "foreign_keys", "unique_constraints", "check_constraints", "indexes", "triggers",
                 "primary_key", "primary_key_name"}       # reported through columns / constraints


def _physical_extras(old, new):
    """Functional dependencies, denormalizations and table-level metadata (description, source, kind, ...)."""
    def sec(a, b):
        return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
                "modified": sorted(k for k in set(a) & set(b) if a[k] != b[k])}
    of, nf = _fd_map(old), _fd_map(new)
    fds = sec(of, nf)
    detail = {}
    for k in fds["modified"]:
        lost = [c for c in of[k]["dependent"] if c not in nf[k]["dependent"]]
        gained = [c for c in nf[k]["dependent"] if c not in of[k]["dependent"]]
        detail[k] = {"lost": lost, "gained": gained, "table": of[k]["table"]}
    od, nd = _denorm_map(old), _denorm_map(new)
    ot, nt = _named(old["physical"]["tables"]), _named(new["physical"]["tables"])
    meta = {}
    for tn in sorted(set(ot) & set(nt)):
        keys = sorted(k for k in set(ot[tn]) | set(nt[tn]) if k not in _TABLE_STRUCT and ot[tn].get(k) != nt[tn].get(k))
        if keys:
            meta[tn] = keys
    return {"functional_dependencies": fds, "fd_detail": detail, "denormalizations": sec(od, nd), "table_fields": meta}


def diff_designs(old, new):
    """Deterministic description of what a revision changed (used for the changelog)."""
    def section(a, b, key="id"):
        am, bm = _named(a, key), _named(b, key)
        return {"added": sorted(set(bm) - set(am)), "removed": sorted(set(am) - set(bm)),
                "modified": sorted(k for k in set(am) & set(bm) if am[k] != bm[k])}
    oe, ne = old["conceptual"]["entities"], new["conceptual"]["entities"]
    ents = section(oe, ne)
    attr_changes = {}
    for eid in ents["modified"]:
        oa, na = _named(next(e for e in oe if e["id"] == eid)["attributes"]), _named(next(e for e in ne if e["id"] == eid)["attributes"])
        attr_changes[eid] = {"added": sorted(set(na) - set(oa)), "removed": sorted(set(oa) - set(na)),
                             "modified": sorted(k for k in set(oa) & set(na) if oa[k] != na[k])}
    rels = section(old["conceptual"]["relationships"], new["conceptual"]["relationships"])
    rel_detail = {}
    orl, nrl = _named(old["conceptual"]["relationships"], "id"), _named(new["conceptual"]["relationships"], "id")
    for rid in rels["modified"]:
        rel_detail[rid] = {"from": _rel_text(orl[rid]), "to": _rel_text(nrl[rid])}
    tabs = section(old["physical"]["tables"], new["physical"]["tables"], "name")
    cols = {}
    ot, nt = _named(old["physical"]["tables"]), _named(new["physical"]["tables"])
    for tn in tabs["modified"]:
        oc, nc = _named(ot[tn]["columns"]), _named(nt[tn]["columns"])
        ch = {"added": sorted(set(nc) - set(oc)), "removed": sorted(set(oc) - set(nc)),
              "modified": sorted(k for k in set(oc) & set(nc) if oc[k] != nc[k])}
        if any(ch.values()):
            cols[tn] = ch
    oc_, nc_ = _constraints(old), _constraints(new)
    cons = {"added": sorted(set(nc_) - set(oc_)), "removed": sorted(set(oc_) - set(nc_)),
            "modified": sorted(k for k in set(oc_) & set(nc_) if oc_[k] != nc_[k])}
    rules = section(old["business_rules"], new["business_rules"])
    ent_fields = {}                                   # entity-level changes other than attributes (identifiers, kind, ...)
    oem, nem = _named(oe, "id"), _named(ne, "id")
    for eid in ents["modified"]:
        keys = sorted(k for k in set(oem[eid]) | set(nem[eid]) if k != "attributes" and oem[eid].get(k) != nem[eid].get(k))
        if keys:
            ent_fields[eid] = keys
    return {**_physical_extras(old, new), "entities": ents, "entity_fields": ent_fields,
            "assumptions": section(old["assumptions"], new["assumptions"]),
            "open_questions": section(old["open_questions"], new["open_questions"]), "attributes": attr_changes, "relationships": rels, "relationship_detail": rel_detail,
            "tables": tabs, "columns": cols, "constraints": cons, "business_rules": rules,
            "constraint_info": {n: {"kind": (nc_.get(n) or oc_.get(n))[0], "table": (nc_.get(n) or oc_.get(n))[1]}
                                for n in cons["added"] + cons["removed"] + cons["modified"]},
            "assumptions_added": sorted({a["id"] for a in new["assumptions"]} - {a["id"] for a in old["assumptions"]}),
            "dbms": ({"from": old["dbms"]["name"], "to": new["dbms"]["name"]} if old["dbms"]["id"] != new["dbms"]["id"] else None)}


def _rel_text(r):
    return ", ".join(f"{p['entity']}{('/' + p['role']) if p.get('role') else ''} {dm.cardinality_text(p['cardinality'])}"
                     for p in r["participants"]) + f" ({r['type']})"


# ------------------------------------------------------------------ preview (Stage 5B)
ADD_ONLY_TYPES = {"add_entity", "add_attribute", "add_relationship", "add_business_rule", "switch_dbms"}
MAX_PREVIEW_ITEMS = 12


def unexpected_removals(types, diff):
    """Things a revision removed although its declared change types only ADD things (or switch the DBMS).
    Catches e.g. 'add pet weight' that silently drops an owner's phone number."""
    if not set(types) <= ADD_ONLY_TYPES:
        return []
    out = [f"entity `{x}`" for x in diff["entities"]["removed"]]
    out += [f"attribute `{e}.{a}`" for e, ch in diff["attributes"].items() for a in ch["removed"]]
    out += [f"relationship `{x}`" for x in diff["relationships"]["removed"]]
    out += [f"table `{x}`" for x in diff["tables"]["removed"]]
    out += [f"column `{t}.{c}`" for t, ch in diff["columns"].items() for c in ch["removed"]]
    out += [f"business rule `{x}`" for x in diff["business_rules"]["removed"]]
    out += [f"functional dependency `{x}`" for x in diff.get("functional_dependencies", {}).get("removed", [])]
    out += [f"functional dependency `{k}` loses dependent(s) {', '.join(d['lost'])}"
            for k, d in sorted(diff.get("fd_detail", {}).items()) if d["lost"]]
    out += [f"denormalization `{x}`" for x in diff.get("denormalizations", {}).get("removed", [])]
    out += [f"assumption `{x}`" for x in diff.get("assumptions", {}).get("removed", [])]
    out += [f"open question `{x}`" for x in diff.get("open_questions", {}).get("removed", [])]
    return out


# What each declared change type is EXPECTED to touch. Anything outside the union of the declared types' scopes is
# reported as "possibly out of scope". This is a heuristic on the declared change types, NOT proof of the user's intent:
# a flagged change may be perfectly legitimate, and an unflagged one may still be unwanted - the user decides.
_ALL_CONSTRAINTS = {"constraint+", "constraint-", "constraint~"}
SCOPE = {
    "add_entity": {"entity+", "relationship+", "rule+", "rule~", "constraint+"},
    "remove_entity": {"entity-", "attribute-", "relationship-", "relationship~", "rule-", "rule~"} | _ALL_CONSTRAINTS,
    "add_attribute": {"attribute+", "rule+", "rule~", "constraint+", "constraint~"},
    "remove_attribute": {"attribute-", "rule-", "rule~", "constraint-", "constraint~"},
    "change_attribute": {"attribute~", "rule~", "constraint~", "constraint+", "constraint-"},
    "change_cardinality": {"relationship~", "rule~"} | _ALL_CONSTRAINTS,
    "add_relationship": {"relationship+", "rule+", "rule~", "constraint+"},
    "remove_relationship": {"relationship-", "rule-", "rule~", "constraint-", "constraint~"},
    "add_business_rule": {"rule+", "rule~", "constraint+", "constraint~"},
    "change_constraint": {"rule~", "rule+", "attribute~"} | _ALL_CONSTRAINTS,
    "switch_dbms": {"dbms", "rule~"} | _ALL_CONSTRAINTS,
}
_NOTES = {"assumption+", "assumption~", "question+", "question~", "entity~", "fd+", "fd~", "denorm+"}      # recording/rewording notes accompanies any change
_REMOVES_NOTES = {"assumption-", "question-", "fd-", "denorm-"}
_META = {"tablemeta~", "denorm~", "fd-"}                      # edits/dependency loss: not part of an additive request
for _t in SCOPE:
    SCOPE[_t] = (SCOPE[_t] | _NOTES | (_REMOVES_NOTES if _t.startswith("remove_") else set())
                 | (_META if not _t.startswith("add_") else set()))
_VERB = {"+": "added", "-": "removed", "~": "modified"}


def _observed(diff):
    """[(category code, human text)] for every difference the deterministic diff found."""
    out = []
    if diff["dbms"]:
        out.append(("dbms", f"DBMS {diff['dbms']['from']} -> {diff['dbms']['to']}"))
    for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
        out += [(f"entity{sign}", f"{_VERB[sign]} entity `{x}`") for x in diff["entities"][k] if k != "modified"]
        out += [(f"relationship{sign}", f"{_VERB[sign]} relationship `{x}`") for x in diff["relationships"][k]]
        out += [(f"rule{sign}", f"{_VERB[sign]} business rule `{x}`") for x in diff["business_rules"][k]]
        out += [(f"assumption{sign}", f"{_VERB[sign]} assumption `{x}`") for x in diff.get("assumptions", {}).get(k, [])]
        out += [(f"question{sign}", f"{_VERB[sign]} open question `{x}`") for x in diff.get("open_questions", {}).get(k, [])]
        out += [(f"constraint{sign}", f"{_VERB[sign]} {diff['constraint_info'][x]['kind']} `{x}` on `{diff['constraint_info'][x]['table']}`")
                for x in diff["constraints"][k]]
    for e, ch in diff["attributes"].items():
        for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
            out += [(f"attribute{sign}", f"{_VERB[sign]} attribute `{e}.{a}`") for a in ch[k]]
    out += [("fd+", f"added functional dependency `{x}`") for x in diff.get("functional_dependencies", {}).get("added", [])]
    out += [("fd-", f"removed functional dependency `{x}`") for x in diff.get("functional_dependencies", {}).get("removed", [])]
    for k in diff.get("functional_dependencies", {}).get("modified", []):
        det = diff["fd_detail"][k]
        out.append(("fd-" if det["lost"] else "fd~", f"modified functional dependency `{k}`" +
                    (f" (lost dependent(s) {', '.join(det['lost'])})" if det["lost"] else "")))
    for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
        out += [(f"denorm{sign}", f"{_VERB[sign]} denormalization `{x}`") for x in diff.get("denormalizations", {}).get(k, [])]
    out += [("tablemeta~", f"modified table `{t}` ({', '.join(f)})") for t, f in sorted(diff.get("table_fields", {}).items())]
    out += [("entity~", f"modified entity `{e}` ({', '.join(f)})") for e, f in sorted(diff.get("entity_fields", {}).items())]
    return out


def suspicious_changes(types, diff, skip_removals=False):
    """Changes outside the likely scope of the declared change types (see SCOPE). [] when no scope can be inferred
    (`other`, unknown types). skip_removals: removals already reported by unexpected_removals()."""
    types = list(types)
    if not types or "other" in types or any(t not in SCOPE for t in types):
        return []
    allowed = set().union(*(SCOPE[t] for t in types))
    found = [text for code, text in _observed(diff) if code not in allowed]
    if skip_removals:
        dup = ("removed entity", "removed attribute", "removed relationship", "removed business rule",
               "removed assumption", "removed open question", "removed functional dependency",
               "removed denormalization", "modified functional dependency")
        found = [t for t in found if not t.startswith(dup)]
    return found


def unrelated_removals(old, new, diff):
    """Removals of rules, assumptions, open questions, functional dependencies or denormalizations that the structural
    removals found in the diff (entities, relationships, tables, columns, attributes) do NOT explain. Independent of the
    LLM's declared change types - 'other' or 'remove_attribute' cannot authorize wiping unrelated notes.
    'Explained' = refers to (or mentions) something this same revision removed. Returns texts like unexpected_removals()."""
    import re
    gone_cols = {t: set(ch["removed"]) for t, ch in diff["columns"].items() if ch["removed"]}
    gone_tables = set(diff["tables"]["removed"])
    tokens = {x.lower() for x in diff["entities"]["removed"] + diff["relationships"]["removed"] + sorted(gone_tables)}
    tokens |= {c.lower() for cols in gone_cols.values() for c in cols}
    tokens |= {a.lower() for ch in diff["attributes"].values() for a in ch["removed"]}
    removed_entity_ids = set(diff["entities"]["removed"])
    for e in old["conceptual"]["entities"]:
        if e["id"] in removed_entity_ids:
            tokens.add(e["name"].lower())
    tokens |= {x.lower() for x in diff["business_rules"]["removed"] + diff["constraints"]["removed"]}
    tokens.discard("")

    def mentions(text):
        words = set(re.findall(r"[a-z0-9]+", str(text).lower().replace("_", " ")))
        return any(set(re.findall(r"[a-z0-9]+", t.replace("_", " "))) <= words for t in tokens)

    out = []
    om = lambda items: {i["id"]: i for i in items}
    for x in diff["assumptions"]["removed"]:
        a = om(old["assumptions"])[x]
        if not (({r.lower() for r in a.get("related_to", [])} & tokens) or mentions(a["text"])):
            out.append(f"assumption `{x}`")
    for x in diff["open_questions"]["removed"]:
        if not mentions(om(old["open_questions"])[x]["text"]):
            out.append(f"open question `{x}`")
    for x in diff["business_rules"]["removed"]:
        r = om(old["business_rules"])[x]
        refs = set(r.get("applies_to") or []) | set((r.get("enforcement") or {}).get("implemented_by") or [])
        if not (refs & (removed_entity_ids | set(diff["constraints"]["removed"]) | tokens)
                or any(r_.lower() in tokens for r_ in refs) or mentions(r["description"])):
            out.append(f"business rule `{x}`")
    ofd = _fd_map(old)
    for k in diff["functional_dependencies"]["removed"]:
        f = ofd[k]
        if f["table"] not in gone_tables and not ((set(f["determinant"]) | set(f["dependent"])) & gone_cols.get(f["table"], set())):
            out.append(f"functional dependency `{k}`")
    for k, d in sorted(diff["fd_detail"].items()):
        lost = [c for c in d["lost"] if c not in gone_cols.get(d["table"], set())]
        if lost:
            out.append(f"functional dependency `{k}` loses dependent(s) {', '.join(lost)}")
    odn = _denorm_map(old)
    for k in diff["denormalizations"]["removed"]:
        if odn[k]["table"] not in gone_tables:
            out.append(f"denormalization `{k}`")
    return out


def structural_removals(diff, already=()):
    """Every structural deletion found by comparing the old and new design models - entities, attributes, relationships,
    tables, columns, constraints/indexes/triggers - whatever change_types the LLM declared. `already`: texts reported
    elsewhere (unexpected removals, or 'removed ...' out-of-scope findings), which are not repeated."""
    out = [f"entity `{x}`" for x in diff["entities"]["removed"]]
    out += [f"attribute `{e}.{a}`" for e, ch in sorted(diff["attributes"].items()) for a in ch["removed"]]
    out += [f"relationship `{x}`" for x in diff["relationships"]["removed"]]
    out += [f"table `{x}`" for x in diff["tables"]["removed"]]
    out += [f"column `{t}.{c}`" for t, ch in sorted(diff["columns"].items()) for c in ch["removed"]]
    gone = set(diff["tables"]["removed"])
    out += [f"{diff['constraint_info'][n]['kind']} `{n}` on `{diff['constraint_info'][n]['table']}`"
            for n in diff["constraints"]["removed"] if diff["constraint_info"][n]["table"] not in gone]
    seen = set(already)
    return [x for x in out if x not in seen and "removed " + x not in seen]


# ------------------------------------------------------------------ weakened protections (Batch 6A)
# Rank of the protection a foreign-key action gives the parent row (higher = stricter). Moving DOWN the ranking
# (e.g. RESTRICT -> CASCADE / SET NULL) removes protection that the old design guaranteed.
_FK_ACTION_RANK = {"RESTRICT": 3, "NO ACTION": 3, "CASCADE": 2, "SET DEFAULT": 1, "SET NULL": 1}


def _cond_set(item):
    return {tuple(sorted((k, str(v)) for k, v in c.items())) for c in (item.get("where") or [])}


def _unique_keys(t):
    """[(columns set, condition set)] of every uniqueness guarantee of a physical table (PK, UNIQUE, unique index)."""
    keys = [(set(t["primary_key"]), set())] if t.get("primary_key") else []
    keys += [(set(u["columns"]), set()) for u in t["unique_constraints"] if u["columns"]]
    keys += [(set(i["columns"]), _cond_set(i)) for i in t["indexes"] if i.get("unique") and i["columns"]]
    return keys


def _uniqueness_kept(new_t, cols, conds):
    """Is uniqueness of `cols` (among rows matching `conds`) still guaranteed? A key on a SUBSET of the columns, and a
    key whose row condition is the same or broader, is at least as strong."""
    return any(kc <= cols and kconds <= conds for kc, kconds in _unique_keys(new_t))


def weakened_protections(old, new):
    """Structured-data check, independent of the declared change types: protections of the OLD design that the NEW
    design no longer guarantees - UNIQUE / unique index / primary key, foreign keys (and their ON DELETE/UPDATE
    strictness), CHECK constraints and triggers (append-only / immutable columns), plus conceptual `unique` flags.
    A constraint that merely moved to another name is NOT weakening. Tables and columns that were removed are reported
    as structural removals, not repeated here. Limitation: a CHECK whose expression text was edited cannot be proven
    weaker from text alone and is not reported. Returns human-readable strings ([] when nothing was weakened)."""
    out = []
    nt = _named(new["physical"]["tables"])
    gone_tables = set(_named(old["physical"]["tables"])) - set(nt)
    norm = lambda e: " ".join(str(e).lower().split())
    for ot in old["physical"]["tables"]:
        tn = ot["name"]
        if tn not in nt:
            continue
        n = nt[tn]
        gone = {c["name"] for c in ot["columns"]} - {c["name"] for c in n["columns"]}
        if ot.get("primary_key") and not (set(ot["primary_key"]) & gone) and \
                not _uniqueness_kept(n, set(ot["primary_key"]), set()):
            out.append(f"primary key `{ot.get('primary_key_name') or tn}` on `{tn}` ({', '.join(ot['primary_key'])}) "
                       f"is removed or no longer unique")
        for u in ot["unique_constraints"]:
            if u["columns"] and not (set(u["columns"]) & gone) and not _uniqueness_kept(n, set(u["columns"]), set()):
                out.append(f"unique constraint `{u['name']}` on `{tn}` ({', '.join(u['columns'])}) is removed or weakened")
        for i in ot["indexes"]:
            if i.get("unique") and i["columns"] and not (set(i["columns"]) & gone) and \
                    not _uniqueness_kept(n, set(i["columns"]), _cond_set(i)):
                now = next((x for x in n["indexes"] if x["name"] == i["name"]), None)
                how = ("unique: true -> false" if now is not None and not now.get("unique") else
                       "uniqueness condition narrowed" if now is not None else "removed")
                out.append(f"unique index `{i['name']}` on `{tn}` ({', '.join(i['columns'])}) is weakened ({how})")
        for fk in ot["foreign_keys"]:
            if set(fk["columns"]) & gone or fk["ref_table"] in gone_tables:
                continue
            cand = [x for x in n["foreign_keys"] if x["columns"] == fk["columns"] and x["ref_table"] == fk["ref_table"]
                    and x["ref_columns"] == fk["ref_columns"]]
            if not cand:
                out.append(f"foreign key `{fk['name']}` on `{tn}` ({', '.join(fk['columns'])} -> {fk['ref_table']}) is removed or redirected")
                continue
            for k in ("on_delete", "on_update"):
                r0, r1 = _FK_ACTION_RANK.get(fk.get(k), 3), min(_FK_ACTION_RANK.get(x.get(k), 3) for x in cand)
                if r1 < r0:
                    out.append(f"foreign key `{fk['name']}` on `{tn}`: {k.upper().replace('_', ' ')} "
                               f"{fk.get(k)} -> {cand[0].get(k)} is less protective")
        for c in ot["check_constraints"]:
            words = set(re.findall(r"[a-z0-9_]+", c["expression"].lower()))
            if words & {g.lower() for g in gone}:          # its column was removed: reported as a column removal
                continue
            if not any(x["name"] == c["name"] or norm(x["expression"]) == norm(c["expression"]) for x in n["check_constraints"]):
                out.append(f"check constraint `{c['name']}` on `{tn}` ({c['expression']}) is removed")
        for tr in ot.get("triggers", []):
            news = [x for x in n.get("triggers", []) if x["kind"] == tr["kind"]]
            if tr["kind"] == "append_only":
                kept = set().union(*[set(x.get("operations", [])) for x in news]) if news else set()
                lost = sorted(set(tr.get("operations", [])) - kept)
            else:
                kept = set().union(*[set(x.get("columns", [])) for x in news]) if news else set()
                lost = sorted(set(tr.get("columns", [])) - kept - gone)
            if lost:
                out.append(f"trigger `{tr['name']}` on `{tn}` ({tr['kind']}) no longer protects: {', '.join(lost)}")
    ne = _named(new["conceptual"]["entities"], "id")
    for e in old["conceptual"]["entities"]:
        if e["id"] in ne:
            na = _named(ne[e["id"]]["attributes"])
            out += [f"attribute `{e['id']}.{a['name']}` is no longer unique (unique: true -> false)"
                    for a in e["attributes"] if a.get("unique") and a["name"] in na and not na[a["name"]].get("unique")]
    return out


def _brief(d, keys):
    return ", ".join(f"{k}: {d.get(k)}" for k in keys if k in d)


def _changed_keys(a, b):
    return "; ".join(f"{k}: {a.get(k)!r} -> {b.get(k)!r}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k))[:200]


def detail_lines(old, new, diff):
    """Deterministic, human-readable list of what differs between two designs (no LLM involved)."""
    L = []

    def add(text):
        L.append("  " + text)

    if diff["dbms"]:
        add(f"DBMS: {diff['dbms']['from']} -> {diff['dbms']['to']}")
    for k in ("added", "removed"):
        for x in diff["entities"][k]:
            add(f"{'+' if k == 'added' else '-'} entity {x}")
    oe, ne = _named(old["conceptual"]["entities"], "id"), _named(new["conceptual"]["entities"], "id")
    for eid, ch in diff["attributes"].items():
        oa, na = _named(oe[eid]["attributes"]), _named(ne[eid]["attributes"])
        for a in ch["added"]:
            add(f"+ attribute {eid}.{a} ({_brief(na[a], ('type', 'required', 'unique'))})")
        for a in ch["removed"]:
            add(f"- attribute {eid}.{a} ({_brief(oa[a], ('type', 'required', 'unique'))})")
        for a in ch["modified"]:
            add(f"~ attribute {eid}.{a}: {_changed_keys(oa[a], na[a])}")
    orl, nrl = _named(old["conceptual"]["relationships"], "id"), _named(new["conceptual"]["relationships"], "id")
    for rid in diff["relationships"]["added"]:
        add(f"+ relationship {rid}: {_rel_text(nrl[rid])}")
    for rid in diff["relationships"]["removed"]:
        add(f"- relationship {rid}: {_rel_text(orl[rid])}")
    for rid, ch in diff["relationship_detail"].items():
        add(f"~ relationship {rid}: {ch['from']} -> {ch['to']}")
    ot, nt = _named(old["physical"]["tables"]), _named(new["physical"]["tables"])
    for t in diff["tables"]["added"]:
        add(f"+ table {t} ({len(nt[t]['columns'])} columns)")
    for t in diff["tables"]["removed"]:
        add(f"- table {t}")
    for t, ch in diff["columns"].items():
        oc, nc = _named(ot[t]["columns"]), _named(nt[t]["columns"])
        for c in ch["added"]:
            add(f"+ column {t}.{c} ({nc[c]['data_type']}{'' if nc[c].get('nullable', True) is False else ', nullable'})")
        for c in ch["removed"]:
            add(f"- column {t}.{c} ({oc[c]['data_type']})")
        if diff["dbms"]:
            continue
        for c in ch["modified"]:
            add(f"~ column {t}.{c}: {_changed_keys(oc[c], nc[c])}")
    if diff["dbms"]:
        n = sum(len(ch["modified"]) for ch in diff["columns"].values())
        add(f"~ {n} column definition(s) changed to the new dialect (types, defaults, identity columns)")
    for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
        for c in diff["constraints"][k]:
            info = diff["constraint_info"][c]
            add(f"{sign} {info['kind']} {c} on {info['table']}")
    for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
        for r in diff["business_rules"][k]:
            add(f"{sign} business rule {r}")
    for e, f in sorted(diff.get("entity_fields", {}).items()):
        add(f"~ entity {e}: {', '.join(f)} changed")
    ofd, nfd = _fd_map(old), _fd_map(new)
    for k, sign in (("added", "+"), ("removed", "-")):
        for x in diff.get("functional_dependencies", {}).get(k, []):
            add(f"{sign} functional dependency {_fd_label((nfd if k == 'added' else ofd)[x])}")
    for k in diff.get("functional_dependencies", {}).get("modified", []):
        d = diff["fd_detail"][k]
        bits = [f"- dependent {c}" for c in d["lost"]] + [f"+ dependent {c}" for c in d["gained"]]
        add(f"~ functional dependency {k}: " + (", ".join(bits) or "note changed"))
    odn, ndn = _denorm_map(old), _denorm_map(new)
    for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
        for x in diff.get("denormalizations", {}).get(k, []):
            add(f"{sign} denormalization {x}")
    for t, f in sorted(diff.get("table_fields", {}).items()):
        add(f"~ table {t}: {', '.join(f)} changed")
    for label, key, items in (("assumption", "assumptions", "assumptions"), ("open question", "open_questions", "open_questions")):
        om, nm = _named(old[items], "id"), _named(new[items], "id")
        for k, sign in (("added", "+"), ("removed", "-"), ("modified", "~")):
            for x in diff.get(key, {}).get(k, []):
                src = nm[x] if k != "removed" else om[x]
                add(f"{sign} {label} {x}: {str(src.get('text', ''))[:80]}")
    if len(L) > MAX_PREVIEW_ITEMS * 4:
        L = L[:MAX_PREVIEW_ITEMS * 4] + [f"  ... and {len(L) - MAX_PREVIEW_ITEMS * 4} more change(s)"]
    return L


def build_preview(current, cand, record):
    """Proposed-change summary + deterministic diff shown BEFORE anything is committed."""
    a = record["affected"]
    lines = [f"Request: {record['request']}", f"Proposed change: {record['summary']}", f"Why: {record['rationale']}",
             f"Change types: {', '.join(record['change_types'])}", "",
             "Differences between the current design and the proposal (computed by code, not by the LLM):"]
    lines += detail_lines(current, cand, a) or ["  (no structural differences)"]
    if record.get("unexpected_removals"):
        lines += ["", "!! CHECK CAREFULLY: this request only adds things, but the proposal also REMOVES: " +
                  ", ".join(record["unexpected_removals"]) + ". Reject it unless that is what you want."]
    if record.get("structural_removals"):
        lines += ["", "!! DELETES STRUCTURE: the proposal removes " + ", ".join(record["structural_removals"]) +
                  ". Deletions always need your explicit confirmation, whatever the declared change types say."]
    if record.get("weakened_protections"):
        lines += ["", "!! WEAKENS DATABASE PROTECTIONS: " + "; ".join(record["weakened_protections"]) +
                  ". This needs your explicit confirmation whatever the declared change types say; `--yes` never accepts it."]
    if record.get("unrelated_removals"):
        lines += ["", "!! POSSIBLY DESTRUCTIVE: the proposal removes notes/rules/dependencies that no structural removal in it "
                  "explains (the declared change types do not matter): " + ", ".join(record["unrelated_removals"]) +
                  ". Accept only if you want these removed."]
    if record.get("suspicious_changes"):
        lines += ["", "!! POSSIBLY OUT OF SCOPE: these changes go beyond what the declared change types "
                  f"({', '.join(record['change_types'])}) normally touch: " + "; ".join(record["suspicious_changes"]) +
                  ". This is a heuristic, not proof of what you intended - accept only if you want all of them."]
    for t in record["assumptions"]:
        lines.append(f"ASSUMPTION: {t}")
    for w in record["warnings"]:
        lines.append(f"WARNING: {w}")
    lines += ["", "Files that would be regenerated: " + ", ".join(record["files_regenerated"] + [CHANGELOG])]
    return "\n".join(lines)


def _make_record(number, request, meta, current, cand, files):
    diff = diff_designs(current, cand)
    old_warn = set(design_warnings(current))
    norm_warn = design_warnings(cand, analyze_design(cand))
    new_norm = [w for w in norm_warn if w not in old_warn]
    unexpected = unexpected_removals(meta["change_types"], diff)
    suspicious = suspicious_changes(meta["change_types"], diff, skip_removals=bool(unexpected))
    unrelated = [x for x in unrelated_removals(current, cand, diff) if x not in unexpected]
    structural = structural_removals(diff, set(unexpected) | set(suspicious))
    weakened = weakened_protections(current, cand)
    warns = list(meta["warnings"]) + new_norm
    if unexpected:
        warns.append("Possible unintended removal(s) for a request that only adds things: " + ", ".join(unexpected))
    if structural:
        warns.append("Structural deletion(s) (independent of the declared change types): " + ", ".join(structural))
    if unrelated:
        warns.append("Possibly destructive removal(s) not explained by any structural removal: " + ", ".join(unrelated))
    if weakened:
        warns.append("Weakened database protection(s): " + "; ".join(weakened))
    if suspicious:
        warns.append("Possibly out-of-scope change(s): " + "; ".join(suspicious))
    return {"number": number, "timestamp": now_iso(), "request": request, "change_types": meta["change_types"],
            "suspicious_changes": suspicious, "unrelated_removals": unrelated, "weakened_protections": weakened,
            "structural_removals": structural, "summary": meta["summary"], "rationale": meta["rationale"], "affected": diff,
            "files_regenerated": list(files), "unexpected_removals": unexpected,
            "assumptions": meta["assumptions"] + [f"{x['id']}: {x['text']}" for x in cand["assumptions"]
                                                  if x["id"] in diff["assumptions_added"]],
            "warnings": warns,
            "checks": {"design_valid": True, "documents_consistent": True, "normalization_warnings": norm_warn}}


# ------------------------------------------------------------------ changelog
def _items(label, d, extra=None):
    parts = []
    for k in ("added", "removed", "modified"):
        if d.get(k):
            parts.append(f"{k}: " + ", ".join(f"`{x}`" for x in d[k]))
    return f"- {label}: " + ("; ".join(parts) if parts else "none")


def render_changelog(revisions):
    out = ["# Changelog", "",
           "Accepted revisions of the design, newest last. Every entry lists what changed (computed by comparing "
           "`design.json` before and after), why, and which files were regenerated.", ""]
    if not revisions:
        out.append("_No revisions yet._")
    for r in revisions:
        a = r["affected"]
        out += [f"## Revision {r['number']} - {r['timestamp']}", "",
                f"**Request:** {r['request']}", "",
                f"**Change types:** {', '.join(r['change_types'])}", "",
                f"**What changed:** {r['summary']}", "", f"**Why:** {r['rationale']}", "",
                "**Affected elements**", ""]
        if a.get("dbms"):
            out.append(f"- DBMS: {a['dbms']['from']} -> {a['dbms']['to']} (all column types and dialect-specific constraints revisited)")
        out.append(_items("Entities", a["entities"]))
        for eid, ch in a["attributes"].items():
            out.append(f"  - attributes of `{eid}`: " + "; ".join(f"{k} " + ", ".join(f"`{x}`" for x in v) for k, v in ch.items() if v))
        out.append(_items("Relationships", a["relationships"]))
        for rid, ch in a["relationship_detail"].items():
            out.append(f"  - `{rid}`: {ch['from']} -> {ch['to']}")
        out.append(_items("Tables", a["tables"]))
        for tn, ch in a["columns"].items():
            out.append(f"  - columns of `{tn}`: " + "; ".join(f"{k} " + ", ".join(f"`{x}`" for x in v) for k, v in ch.items() if v))
        out.append(_items("Constraints and indexes", a["constraints"]))
        out.append(_items("Business rules", a["business_rules"]))
        for label, key in (("Assumptions", "assumptions"), ("Open questions", "open_questions")):
            if any((a.get(key) or {}).values()):                 # absent in revisions recorded before Batch 4B
                out.append(_items(label, a[key]))
        for label, key in (("Functional dependencies", "functional_dependencies"), ("Denormalizations", "denormalizations")):
            if any((a.get(key) or {}).values()):
                out.append(_items(label, a[key]))
        if a.get("table_fields"):
            out.append("- Table metadata changed: " + "; ".join(f"`{t}` ({', '.join(f)})" for t, f in sorted(a["table_fields"].items())))
        if a.get("entity_fields"):
            out.append("- Entity definitions changed: " + "; ".join(f"`{e}` ({', '.join(f)})" for e, f in sorted(a["entity_fields"].items())))
        if r.get("approval_invalidated"):
            out.append("- Earlier approval of the design was invalidated by this revision (approve again after review).")
        out += ["", "**Files regenerated:** " + ", ".join(f"`{f}`" for f in r["files_regenerated"]) + f", `{CHANGELOG}`", ""]
        out.append("**Assumptions introduced:** " + ("; ".join(r["assumptions"]) if r["assumptions"] else "none"))
        out.append("")
        out.append("**Warnings:** " + ("; ".join(r["warnings"]) if r["warnings"] else "none"))
        out.append("")
        chk = r["checks"]
        out.append(f"**Checks:** design validated; documents consistent with design.json: "
                   f"{'yes' if chk['documents_consistent'] else 'NO'}; normalization re-run "
                   f"({len(chk['normalization_warnings'])} warning(s) in total).")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ files
def _atomic_write(path, text):
    atomic_write_text(path, text)


def _snapshot(out_dir, names):
    return {n: ((out_dir / n).read_text(encoding="utf-8") if (out_dir / n).is_file() else None) for n in names}


def _restore(out_dir, snap):
    for n, text in snap.items():
        p = out_dir / n
        if text is None:
            if p.exists():
                p.unlink()
        else:
            _atomic_write(p, text)


def _public(design):
    return {k: v for k, v in design.items() if k != "_revision"}


def _history(session):
    return session.data.setdefault("revisions", [])


# ------------------------------------------------------------------ commit
def _read(path):
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _fp_of_file(path):
    """Fingerprint of a design.json on disk, or None if it is missing / not valid JSON."""
    try:
        return design_fingerprint(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def commit_status(session):
    """Read-only, deterministic classification of the last commit. -> {"state", "problems", "pending"}:
      clean              - no interrupted revision and the saved design matches output/design.json;
      pending_recoverable- an accepted revision was interrupted and can be completed with certainty (`recover`);
      recovery_required  - the evidence is contradictory (never guessed): see `problems`.
    Never writes anything."""
    d = session.data
    out_dir = session.output_dir
    pending = d.get("revision_pending")
    design = d.get("design")
    disk = _fp_of_file(out_dir / DESIGN_JSON)
    base = design_fingerprint(design) if design else None
    problems = []
    if not pending:
        if design and disk != base:
            problems.append("output/design.json does not match the saved design in state.json, and no interrupted "
                            "revision explains it (it was edited or damaged). Nothing was changed; restore it from "
                            "versions/ with `restore`, or review the file by hand.")
        history = d.get("revisions") or []
        if design and history and not versions.has_version(session, len(history)):
            problems.append(f"The snapshot of the current version {len(history)} is missing.")
        return {"state": "recovery_required" if problems else "clean", "problems": problems, "pending": False}
    cand, record = pending.get("candidate"), pending.get("record")
    if not isinstance(cand, dict) or not cand or not isinstance(record, dict) or "number" not in record:
        return {"state": "recovery_required", "pending": True,
                "problems": ["The saved interrupted revision is incomplete (no candidate design / record)."]}
    try:
        dm.link_relationships(dm.validate_design(copy.deepcopy(cand)))
    except DBAAgentError as e:
        problems.append(f"The saved candidate design of the interrupted revision is not valid: {e}")
    if len(_history(session)) + 1 != record["number"]:
        problems.append(f"The interrupted revision is number {record['number']} but {len(_history(session))} revision(s) "
                        "are committed; the numbers are not sequential.")
    if pending.get("base_fingerprint") not in (None, base):
        problems.append("The interrupted revision was made for a different base design than the one saved now.")
    cfp = design_fingerprint(cand)
    if pending.get("candidate_fingerprint") not in (None, cfp):
        problems.append("The saved candidate design does not match its recorded fingerprint.")
    if disk is not None and disk not in (base, cfp):
        problems.append("output/design.json matches neither the committed design nor the interrupted revision "
                        "(it was edited by hand). It is not overwritten.")
    return {"state": "recovery_required" if problems else "pending_recoverable", "problems": problems, "pending": True}


def _apply_commit(new, cand, record, history, new_choice, names):
    """The state changes of a committed revision, applied to a COPY of the session state (the commit point)."""
    new["design"] = cand
    if new_choice:
        new["dbms_choice"] = new_choice
    new["revisions"] = history
    new.pop("revision_pending", None)
    files = new.setdefault("output_files", [])
    for n in names:
        if n not in files:
            files.append(n)
    new["design_docs"] = {"validated_at": now_iso(), "files": list(REGENERATED), "warnings": design_warnings(cand)}
    new["revision_stage"] = {"status": "in_progress", "revisions_accepted": len(history),
                             "note": "Stage 5 stays incomplete until the user approves the design (`approve`)."}
    if new.get("approval"):                              # any accepted revision invalidates the approval
        old = new.pop("approval")
        new.setdefault("approval_history", []).append(dict(old, status="invalidated", invalidated_at=now_iso(),
                                                           invalidated_by_revision=record["number"]))
        for done in ("revision", "sql"):                 # SQL files of the old design must not stay "complete"
            if done in new.get("completed_stages", []):
                new["completed_stages"].remove(done)
        if new.get("sql_stage"):
            new["sql_stage"]["status"] = "stale"
            new["sql_stage"]["note"] = "Invalidated by an accepted revision: regenerate schema.sql and seed.sql after approval."
        new["current_stage"] = "revision"
    new["last_error"] = None


def commit(session, pending):
    """Write all files for the pending revision and commit it to session state. Idempotent: safe to call again
    after an interruption (files are deterministic; the history entry is added only once).

    Crash safety (no database, no framework): `revision_pending` in state.json (saved BEFORE any file is touched) is the
    journal. Every file is replaced atomically, the snapshot folder appears atomically, and the whole state change
    (design, history, approval invalidation, stage) becomes visible in ONE atomic write of state.json - that write is the
    commit point. Before it, a Python exception rolls the files back and removes the snapshot this call created (state.json
    and the in-memory state are untouched, the approval stays intact); a hard kill leaves `revision_pending` for `recover`
    / the next `revise`, which redoes the same deterministic writes. After it, everything is complete.

    Stage 5B additions: a DBMS switch also rewrites dbms-choice.md and state["dbms_choice"] (pending["dbms_choice"]);
    a snapshot of the new version is saved; an existing approval is invalidated."""
    d = session.data
    cand = pending["candidate"]
    record = pending["record"]
    new_choice = pending.get("dbms_choice")
    out_dir = session.output_dir
    committed = _history(session)
    if record["number"] != len(committed) + 1:
        last = committed[-1] if committed else None
        if last and last["number"] == record["number"] and last.get("timestamp") == record.get("timestamp"):
            d.pop("revision_pending", None)                 # this very revision is already committed: nothing to redo
            session.save()
            return last
        raise RecoveryRequired(f"Cannot commit revision {record['number']}: {len(committed)} revision(s) are committed, so "
                               f"the next number is {len(committed) + 1}. Run `recover` to inspect the session.")
    record["approval_invalidated"] = bool(d.get("approval"))
    history = list(committed) + [record]
    docs = render_all(cand)
    problems = check_documents(cand, docs)               # in-memory check before touching any file
    if problems:
        raise InvalidResponseError("Regenerated documents are not consistent with the revised design:\n" +
                                   "\n".join(f"- {p}" for p in problems[:MAX_PROBLEMS]), retryable=False)
    names = REGENERATED + ([DBMS_CHOICE_MD] if new_choice else []) + [CHANGELOG]
    snap = _snapshot(out_dir, names)
    created_snapshot = False
    try:
        texts = {DESIGN_JSON: json.dumps(cand, indent=2, ensure_ascii=False) + "\n", **docs,
                 CHANGELOG: render_changelog(history)}
        if new_choice:
            texts[DBMS_CHOICE_MD] = dbms_stage.render_markdown(
                new_choice, d.get("requirements"), (d.get("intake") or {}).get("project_name") or cand["project_name"])
        for n in names:
            _atomic_write(out_dir / n, texts[n])
        on_disk = _snapshot(out_dir, DOC_FILES)
        problems = check_documents(cand, on_disk)       # and again from disk
        if problems:
            raise InvalidResponseError("Documents written to disk are not consistent with the revised design:\n" +
                                       "\n".join(f"- {p}" for p in problems[:MAX_PROBLEMS]), retryable=False)
        if json.loads((out_dir / DESIGN_JSON).read_text(encoding="utf-8")) != cand:
            raise InvalidResponseError("design.json on disk differs from the revised design.", retryable=False)
        vtexts = dict(texts)
        vtexts.setdefault(DBMS_CHOICE_MD, _read(out_dir / DBMS_CHOICE_MD))
        created_snapshot = versions.write_version(session, record["number"], vtexts, {
            "timestamp": record["timestamp"], "request": record["request"], "summary": record["summary"],
            "dbms_choice": new_choice or d.get("dbms_choice"), "design_fingerprint": design_fingerprint(cand)},
            committed=len(committed))
        new_state = copy.deepcopy(d)
        _apply_commit(new_state, cand, record, history, new_choice, names)
        session.commit_data(new_state)                   # COMMIT POINT: one atomic write of state.json
    except (Exception, KeyboardInterrupt):
        _restore(out_dir, snap)
        if created_snapshot:
            versions.remove_version(session, record["number"])
        raise
    return record


def finish_pending(session):
    """Complete an interrupted revision (state has a validated candidate but the commit never finished). Refuses, without
    changing anything, when the evidence is contradictory (RecoveryRequired) - see commit_status()."""
    pending = session.data.get("revision_pending")
    if not pending:
        return None
    status = commit_status(session)
    if status["state"] == "recovery_required":
        raise RecoveryRequired("An interrupted revision cannot be completed automatically:\n" +
                               "\n".join(f"- {p}" for p in status["problems"]) +
                               "\nNothing was changed. Run `python -m dba_agent recover` for the options.")
    return commit(session, pending)


def discard_pending(session):
    """Abandon the interrupted revision on purpose: put the committed design's files back exactly as they were (from the
    snapshot of the current version; re-rendered if that snapshot is missing) and drop `revision_pending`. The discarded
    request is kept in state[\"revision_discarded\"]; the committed design, history and approval are untouched.
    -> the discarded record, or None if nothing was pending."""
    d = session.data
    pending = d.get("revision_pending")
    if not pending:
        return None
    out_dir = session.output_dir
    number = len(_history(session))
    names = [DESIGN_JSON, CHANGELOG, DBMS_CHOICE_MD] + DOC_FILES
    if versions.has_version(session, number) and not versions.verify_version(session, number) and \
            versions.load_version(session, number)[0].get("design_fingerprint") == design_fingerprint(d["design"]):
        folder = versions.version_dir(session, number)
        for n in names:
            if (folder / n).is_file():
                _atomic_write(out_dir / n, (folder / n).read_text(encoding="utf-8"))
    else:
        docs = render_all(d["design"])
        _atomic_write(out_dir / DESIGN_JSON, json.dumps(d["design"], indent=2, ensure_ascii=False) + "\n")
        for n, t in docs.items():
            _atomic_write(out_dir / n, t)
        _atomic_write(out_dir / CHANGELOG, render_changelog(_history(session)))
    rec = (pending.get("record") or {})
    new = copy.deepcopy(d)
    new.pop("revision_pending", None)
    new.setdefault("revision_discarded", []).append({"timestamp": now_iso(), "request": rec.get("request"),
                                                      "summary": rec.get("summary"), "number": rec.get("number")})
    session.commit_data(new)
    return rec


def ensure_current_version(session):
    """Make sure the CURRENT design has a snapshot (v000 for the original design; also covers sessions revised
    before snapshots existed). Called while state and files agree."""
    d = session.data
    number = len(_history(session))
    if not d.get("design"):
        return
    if versions.has_version(session, number):
        problems = versions.verify_version(session, number)
        meta = versions.load_version(session, number)[0] if not problems else {}
        if problems or (meta.get("design_fingerprint") not in (None, design_fingerprint(d["design"]))):
            raise RecoveryRequired(f"The snapshot of version {number} does not match the current design"
                                   + (f" ({problems[0]})" if problems else "") + ". It is not overwritten; run `recover`.")
        return
    out = session.output_dir
    texts = {n: _read(out / n) for n in [DESIGN_JSON, CHANGELOG, DBMS_CHOICE_MD] + DOC_FILES}
    versions.write_version(session, number, texts, {
        "timestamp": now_iso(), "request": "", "summary": "Initial design" if number == 0 else f"Revision {number}",
        "dbms_choice": d.get("dbms_choice"), "design_fingerprint": design_fingerprint(d["design"])},
        committed=number)


# ------------------------------------------------------------------ main entry
# Three separate steps (Stage 5B, library-level safety):
#   1. propose()          - ask the LLM, validate, diff, build the preview. WRITES NOTHING to design/documents/history.
#   2. accept()/reject()  - the review decision. A proposal flagged as suspicious needs acknowledge_suspicious=True.
#   3. commit_accepted()  - write files + commit. Refuses a proposal that was not accepted (or is stale).
# revise() = all three in one call, for the CLI and for tests; its `confirm` callback is the review step.
class Proposal:
    """A fully validated but NOT yet committed revision."""

    def __init__(self, request, candidate, record, preview, dbms_choice, base_fingerprint):
        self.request, self.candidate, self.record, self.preview = request, candidate, record, preview
        self.dbms_choice, self.base_fingerprint = dbms_choice, base_fingerprint
        self.status = "proposed"                      # proposed -> accepted -> committed   (or rejected)

    @property
    def suspicious(self):
        return (list(self.record.get("unexpected_removals") or []) + list(self.record.get("structural_removals") or [])
                + list(self.record.get("unrelated_removals") or []) + list(self.record.get("suspicious_changes") or [])
                + list(self.record.get("weakened_protections") or []))

    @property
    def needs_explicit_confirmation(self):
        return bool(self.suspicious)


def _note_failure(session, request, e):
    d = session.data
    pend = d.get("revision_pending")
    if isinstance(e, RevisionRejected) or not pend:
        d.pop("revision_pending", None)
    if isinstance(e, InvalidResponseError) and pend and not isinstance(e, RevisionRejected):
        d.pop("revision_pending", None)             # candidate failed consistency checks: discard, never retry blindly
    d.setdefault("revision_failures", []).append({"timestamp": now_iso(), "request": request, "error": str(e)})
    d["last_error"] = {"stage": "revision", "message": str(e)}
    session.save()


def propose(session, llm, prompts, request):
    """Step 1. Returns a Proposal; the design, documents and history are not touched (an interrupted earlier revision,
    if any, is finished first)."""
    d = session.data
    request = (request or "").strip()
    if not request:
        raise RevisionRejected("Empty change request.")
    if not d.get("design") or not d.get("design_docs"):
        raise InvalidResponseError("Stage 4 must be completed before revisions (no design yet).", retryable=False)
    try:
        if d.get("revision_pending"):                    # resume an interrupted revision first
            finish_pending(session)
        current = d["design"]
        dm.link_relationships(dm.validate_design(current))      # the base must be valid
        ensure_current_version(session)
        number = len(_history(session)) + 1
        shown = {k: v for k, v in current.items() if k not in ("meta", "requirement_gaps")}
        system = prompts.render("revision_system")
        user = prompts.render(
            "revision_user", dbms_ids=", ".join(f"{i} ({DBMS_NAMES[i]})" for i in DBMS_IDS),
            dialects="\n".join(f"### {DBMS_NAMES[i]} ({i})\n{_dialect_notes(prompts, i)}" for i in DBMS_IDS),
            dbms_name=current["dbms"]["name"], design=json.dumps(shown, indent=2, ensure_ascii=False), request=request)
        cand = llm.complete_json(f"revision:{number}", system, user, validator=lambda r: build_candidate(current, r))
        meta = cand.pop("_revision")
        switched = cand["dbms"]["id"] != current["dbms"]["id"]
        files = list(REGENERATED) + ([DBMS_CHOICE_MD] if switched else [])
        record = _make_record(number, request, meta, current, cand, files)
        cand["meta"] = dict(cand.get("meta") or {}, last_revision=number, revised_at=record["timestamp"])
        new_choice = None
        if switched and d.get("dbms_choice"):
            new_choice = dbms_stage.apply_override(d["dbms_choice"], cand["dbms"]["id"], request, meta["rationale"],
                                                   number, record["timestamp"])
        return Proposal(request, cand, record, build_preview(current, cand, record), new_choice, design_fingerprint(current))
    except (ProviderError, MalformedResponseError):
        raise                                            # API/transport failure: nothing about the design or session changes
    except Exception as e:
        _note_failure(session, request, e)
        raise


def accept(proposal, acknowledge_suspicious=False):
    """Step 2 (yes). A proposal with suspicious/out-of-scope changes can only be accepted with
    acknowledge_suspicious=True, i.e. after a person has seen the preview and explicitly confirmed it."""
    if proposal.status != "proposed":
        raise RevisionRejected(f"Proposal is already {proposal.status}.")
    if proposal.needs_explicit_confirmation and not acknowledge_suspicious:
        raise RevisionRejected("This proposal contains possibly unintended changes (" + "; ".join(proposal.suspicious) +
                               ") and needs explicit confirmation after reviewing the preview.")
    proposal.status = "accepted"
    return proposal


def reject(session, proposal, reason="declined by the user"):
    """Step 2 (no). Nothing was written; only a rejection note is saved."""
    proposal.status = "rejected"
    session.data.setdefault("revision_rejections", []).append(
        {"timestamp": now_iso(), "request": proposal.request, "summary": proposal.record["summary"], "reason": reason})
    session.data["last_error"] = None
    session.save()


def commit_accepted(session, proposal):
    """Step 3. Writes all files and commits. Refuses anything that was not accepted, or whose base design changed."""
    d = session.data
    if proposal.status != "accepted":
        raise RevisionRejected(f"Only an accepted proposal can be committed (this one is {proposal.status}).")
    if not d.get("design") or design_fingerprint(d["design"]) != proposal.base_fingerprint:
        raise RevisionRejected("The design changed after this proposal was made; propose the change again.")
    try:
        d["revision_pending"] = {"candidate": proposal.candidate, "record": proposal.record,
                                 "dbms_choice": proposal.dbms_choice, "accepted": True,
                                 "base_fingerprint": proposal.base_fingerprint,
                                 "candidate_fingerprint": design_fingerprint(proposal.candidate)}   # saved first: resumable
        session.save()
        record = commit(session, d["revision_pending"])
        proposal.status = "committed"
        return record
    except Exception as e:
        _note_failure(session, proposal.request, e)
        raise


def revise(session, llm, prompts, request, confirm=None, allow_suspicious=False):
    """propose -> review -> commit in one call. Returns the revision record, or None if the user declined it.

    confirm: callable(preview_text, record) -> bool; it IS the review step and runs after the proposal is fully
    validated and BEFORE anything is written. False -> proposal discarded, design/documents/history unchanged.
    confirm=None is the unattended/programmatic mode (tests, scripts): ordinary proposals are committed, but a
    proposal flagged as suspicious is refused (RevisionRejected) unless allow_suspicious=True. Interactive use must
    pass a confirm callback (the CLI always does)."""
    d = session.data
    if not (request or "").strip() and d.get("revision_pending"):
        return finish_pending(session)                     # `revise` with no request finishes an interrupted one
    proposal = propose(session, llm, prompts, request)
    if confirm is not None:
        ok = bool(confirm(proposal.preview, proposal.record))
        acknowledged = ok                                  # the callback saw the full preview incl. warnings
    else:
        ok, acknowledged = True, allow_suspicious
    if not ok:
        reject(session, proposal)
        return None                                        # nothing was written; last valid design preserved
    try:
        accept(proposal, acknowledge_suspicious=acknowledged)
    except RevisionRejected as e:
        _note_failure(session, proposal.request, e)
        raise
    return commit_accepted(session, proposal)


# ------------------------------------------------------------------ restore an earlier version
def restore_version(session, number, confirm=None):
    """Make an earlier snapshot the current design again. This is committed as a NEW revision (history, changelog
    and snapshots only grow; an existing approval is invalidated). Returns the record, or None if declined."""
    d = session.data
    if not d.get("design") or not d.get("design_docs"):
        raise InvalidResponseError("Stage 4 must be completed before versions can be restored.", retryable=False)
    if d.get("revision_pending"):
        finish_pending(session)
    ensure_current_version(session)
    current_number = len(_history(session))
    if number == current_number:
        raise RevisionRejected(f"Version {number} is already the current version.")
    vmeta, old_design, old_choice = versions.load_version(session, number)
    cand = dm.link_relationships(dm.validate_design(copy.deepcopy(old_design)))
    if design_fingerprint(cand) == design_fingerprint(d["design"]):
        raise RevisionRejected(f"Version {number} is identical to the current design; nothing to restore.")
    new_number = current_number + 1
    meta = {"change_types": ["other"], "summary": f"Restored version {number}",
            "rationale": f"The user restored version {number} ({vmeta.get('summary') or 'no description'}).",
            "assumptions": [], "warnings": []}
    record = _make_record(new_number, f"restore version {number}", meta, d["design"], cand,
                          list(REGENERATED) + ([DBMS_CHOICE_MD] if old_choice else []))
    record["restored_from"] = number
    cand["meta"] = dict(cand.get("meta") or {}, last_revision=new_number, revised_at=record["timestamp"])
    if confirm is not None and not confirm(build_preview(d["design"], cand, record), record):
        return None
    d["revision_pending"] = {"candidate": cand, "record": record, "dbms_choice": old_choice, "accepted": True,
                             "base_fingerprint": design_fingerprint(d["design"]),
                             "candidate_fingerprint": design_fingerprint(cand)}
    session.save()
    try:
        return commit(session, d["revision_pending"])
    except Exception:
        d.pop("revision_pending", None)     # a restore is re-issued by the user; the files were rolled back by commit()
        session.save()
        raise
