"""Functional-dependency analysis of the physical tables (no LLM, no database needed).

Method (textbook, per table):
  * Dependencies = the functional dependencies declared in design.json + one dependency
    "key -> all columns" for every TOTAL key (see below). Nothing else is ever assumed.
  * closure(X) = every column determined by X (attribute closure).
  * TOTAL key: PRIMARY KEY, or a UNIQUE constraint / unique index that has no condition (partial index) and
    no nullable column. SQL treats NULLs as distinct, so a UNIQUE over nullable columns, or a partial unique
    index, does NOT identify every row: it is reported as a *conditional key* and is never a candidate key
    or superkey.
  * superkey  : closure(X) contains all columns.
  * candidate key: a total key reduced until no column can be removed, plus any declared determinant that
    already determines every column (an "implied" key that nothing enforces - reported as a KEY finding), plus
    every alternate key implied by the dependencies (e.g. PK (a,b) with b -> c and c -> b also gives (a,c)).
    The enumeration is bounded (MAX_CANDIDATE_KEYS / MAX_KEY_SEARCH_STEPS); if it is cut off an UNCERTAIN
    finding is reported and 2NF/3NF findings are downgraded to UNCERTAIN.
  * prime attribute: belongs to some candidate key.
  * 2NF violated : a non-prime column depends on a PROPER subset of a candidate key (partial dependency).
  * 3NF (formal): for every non-trivial X -> A, X must be a superkey OR A must be prime. Violations are
    reported as "2NF" when X is a proper part of a candidate key (partial dependency) and as "3NF" otherwise
    (transitive / non-key dependency). Both break 3NF.
  * BCNF violated: X -> A (A not in X) with X not a superkey (A prime: this alone keeps 3NF).
  A nullable UNIQUE / partial unique index never makes a column set a key by itself, but a declared dependency
  X -> A is still accepted and judged by the formal rules above (X not a superkey, A not prime = violation).
  The determinants tested are the declared determinants; for each, every column in closure(X) is checked
  (so dependencies implied through other declared dependencies are found too).

UNCERTAIN findings: the analysis refuses to claim what it cannot know. It reports "UNCERTAIN" (status
"UNVERIFIED") when a declared "X determines every column" claim rests only on a conditional key (nullable
UNIQUE / partial unique index), when key enumeration was cut off, when a candidate key is too large to check, and when column types suggest several values per cell (1NF).

LIMITATIONS (also printed in normalization.md): the result is only as complete as the declared
dependencies. A dependency nobody declared cannot be found, so "no violation found" means
"no violation among the declared dependencies", not a proof. 1NF (atomic values) cannot be
verified from a schema; only suspicious column types / numbered column names are flagged. Keys that are
not declared in the schema are unknown to the analysis.
"""
import re
from itertools import combinations

from .design_model import _key_defs, where_text

MAX_KEY_SIZE_FOR_SUBSETS = 8
MAX_CANDIDATE_KEYS = 32        # enumeration bound: stop (and report uncertainty) beyond this many candidate keys
MAX_KEY_SEARCH_STEPS = 2000    # enumeration bound: maximum number of (key, dependency) combinations examined
NON_ATOMIC_HINTS = ("JSON", "ARRAY", "XML", "[]", "SET(", "HSTORE")
ORDER = ["1NF", "2NF", "3NF", "BCNF"]


def closure(attrs, fds):
    result = set(attrs)
    changed = True
    while changed:
        changed = False
        for left, right in fds:
            if left <= result and not right <= result:
                result |= right
                changed = True
    return result


def _cols(cols):
    return ", ".join(cols)


def _minimize(attrs, fds, allc):
    key = list(attrs)
    for a in list(key):
        trial = [x for x in key if x != a]
        if trial and closure(trial, fds) >= allc:
            key = trial
    return key


def candidate_keys_bounded(table, fds):
    """Returns (keys, complete). Keys: total declared keys reduced until minimal, plus declared determinants that
    already determine every column, plus every alternate key implied by the dependencies (Lucchesi-Osborn: for a
    known key K and a dependency X -> Y, (K - Y) + X contains another key). The search is bounded by
    MAX_CANDIDATE_KEYS / MAX_KEY_SEARCH_STEPS; complete=False means it was cut off and more keys may exist."""
    allc = {c["name"] for c in table["columns"]}
    seeds = [tuple(d["columns"]) for d in _key_defs(table) if d["total"]]
    seeds += [tuple(sorted(left)) for left, _right in fds if closure(left, fds) >= allc]
    found, complete, steps = [], True, 0
    for seed in seeds:
        key = _minimize(seed, fds, allc)
        if set(key) <= allc and not any(set(key) == set(f) for f in found):
            found.append(tuple(key))
    queue = list(found)
    while queue:
        known = queue.pop(0)
        for left, right in fds:
            steps += 1
            if steps > MAX_KEY_SEARCH_STEPS:
                return _only_minimal(found), False
            cand = (set(known) - set(right)) | set(left)
            if not cand <= allc or any(set(f) <= cand for f in found):
                continue
            if len(found) >= MAX_CANDIDATE_KEYS:
                return _only_minimal(found), False
            key = tuple(c for c in (c["name"] for c in table["columns"]) if c in _minimize(sorted(cand), fds, allc))
            found.append(key)
            queue.append(key)
    return _only_minimal(found), complete


def _only_minimal(found):
    return [k for k in found if not any(set(o) < set(k) for o in found)]


def candidate_keys(table, fds):
    """Minimal keys (declaration order of the seeds first, then implied alternate keys). See candidate_keys_bounded."""
    return candidate_keys_bounded(table, fds)[0]


def _finding(form, det, dep, message):
    return {"form": form, "determinant": list(det), "dependent": list(dep), "message": message}


# Verdict about the 3NF claim of one table (kept separate from "no violation found"):
NF3_PROVEN, NF3_UNVERIFIED, NF3_VIOLATED = "proven", "unverified", "violated"


def nf3_verdict(findings, keys_complete):
    """-> (verdict, reasons). `violated`: a known 2NF/3NF violation among the declared dependencies. `unverified`:
    no violation was found but the claim cannot be proven (key enumeration cut off, key too large to check, a
    dependency resting only on a conditional key). `proven`: complete key set, no violation, nothing uncertain
    about keys - always relative to the DECLARED dependencies. A 1NF atomicity hint does not change the 3NF
    verdict (it is a 1NF question, reported on its own)."""
    if any(f["form"] in ("2NF", "3NF") for f in findings):
        return NF3_VIOLATED, []
    reasons = [f["message"] for f in findings if f["form"] == "UNCERTAIN" and not f.get("atomicity")]
    if not keys_complete and not reasons:
        reasons = ["Candidate-key enumeration was cut off."]
    return (NF3_UNVERIFIED, reasons) if reasons else (NF3_PROVEN, [])


def analyze_table(table, declared_fds, denormalizations):
    """declared_fds: [(frozenset determinant, frozenset dependent)]. Returns a plain dict."""
    name = table["name"]
    colorder = [c["name"] for c in table["columns"]]
    allc = set(colorder)
    defs = _key_defs(table)
    total = [d for d in defs if d["total"]]
    conditional = [d for d in defs if not d["total"]]
    declared = [(frozenset(l), frozenset(r)) for l, r in declared_fds]
    fds = declared + [(frozenset(d["columns"]), frozenset(allc)) for d in total]
    keys, keys_complete = candidate_keys_bounded(table, fds)
    prime = {a for k in keys for a in k}
    nonprime = [c for c in colorder if c not in prime]
    notes, findings = [], []

    def superkey(x):
        return closure(x, fds) >= allc

    # ---- notes about keys
    for d in total:
        reduced = next((k for k in keys if set(k) <= set(d["columns"])), None)
        if reduced and set(reduced) != set(d["columns"]):
            notes.append(f"{d['kind']} ({_cols(d['columns'])}) is a superkey but not minimal; the candidate key is "
                         f"({_cols(reduced)}).")
    cond_info = []
    for d in conditional:
        reasons = []
        if d["nullable_columns"]:
            reasons.append(f"nullable column(s) {_cols(d['nullable_columns'])}: SQL treats NULLs as distinct")
        if d["where"]:
            reasons.append(f"it is a partial unique index that only applies where {where_text(d['where'])}")
        cond_info.append({"kind": d["kind"], "name": d["name"], "columns": d["columns"], "reasons": reasons})
        notes.append(f"{d['kind']} ({_cols(d['columns'])}) is only a conditional key ({'; '.join(reasons)}); "
                     "it does not identify every row and is NOT used as a candidate key or superkey.")
    tk_sets = [set(d["columns"]) for d in total]
    ck_sets = [set(d["columns"]) for d in conditional]
    for left, _right in declared_fds:
        left = set(left)
        if closure(left, fds) < allc or any(k <= left for k in tk_sets):
            continue
        if any(k <= left for k in ck_sets):
            findings.append(_finding("UNCERTAIN", sorted(left), [],
                                     f"The declared dependency says ({_cols(sorted(left))}) determines every column, but the only "
                                     "constraint over it is a conditional key (nullable UNIQUE or partial unique index); rows with "
                                     "NULLs / outside the index condition are not covered, so it is not treated as a key. Make the "
                                     "columns NOT NULL with a full UNIQUE constraint, or confirm that the dependency really holds."))
        else:
            findings.append(_finding("KEY", sorted(left), [],
                                     f"The declared dependency says ({_cols(sorted(left))}) determines every column, "
                                     "but no PRIMARY KEY/UNIQUE constraint enforces it as a key."))
    # ---- 2NF: non-prime column depends on a proper subset of a candidate key
    seen = set()
    for k in keys:
        if len(k) < 2:
            continue
        if len(k) > MAX_KEY_SIZE_FOR_SUBSETS:
            findings.append(_finding("UNCERTAIN", k, [], f"Candidate key ({_cols(k)}) is too large for the partial-dependency "
                                                         "check; 2NF is not verified for it."))
            continue
        for size in range(1, len(k)):
            for sub in combinations(k, size):
                for a in nonprime:
                    if a in closure(sub, fds) and not any(set(s2) < set(sub) and a == a2 for s2, a2 in seen):
                        seen.add((sub, a))
                        findings.append(_finding("2NF", sub, [a], f"Partial dependency: non-prime column {a} depends on "
                                                                  f"({_cols(sub)}), only part of the candidate key ({_cols(k)})."))
    # ---- 3NF (formal criterion) / BCNF over the declared determinants
    reported = {f["dependent"][0] for f in findings if f["form"] in ("2NF", "3NF", "BCNF")}
    determinants = []
    for left, _right in declared:
        if left not in determinants:
            determinants.append(left)
    direct_of = {x: set().union(*[r for l, r in declared if l == x]) - x for x in determinants if not superkey(x)}
    stated = set().union(*direct_of.values()) if direct_of else set()      # columns some declared dependency already covers
    for x in determinants:
        if superkey(x):
            continue
        direct = direct_of[x]
        implied = closure(x, fds) - x - direct
        for a in sorted(direct | implied):
            if a not in allc or (a in implied and (a in reported or a in stated)):
                continue
            if a in nonprime and any(x < set(k) for k in keys):
                continue                                        # partial dependency: already reported as 2NF
            via = "" if a in direct else " (through other declared dependencies)"
            if a in nonprime:
                findings.append(_finding("3NF", sorted(x), [a],
                                         f"Non-key dependency: ({_cols(sorted(x))}) -> {a}{via}, but ({_cols(sorted(x))}) is not a "
                                         f"superkey and {a} is not part of any candidate key."))
            else:
                findings.append(_finding("BCNF", sorted(x), [a],
                                         f"({_cols(sorted(x))}) -> {a}{via} where {a} is a prime attribute but "
                                         f"({_cols(sorted(x))}) is not a superkey (violates BCNF only)."))
    # ---- candidate-key enumeration was cut off: prime / non-prime is not certain, so 2NF/3NF claims are not either
    if not keys_complete:
        for f in findings:
            if f["form"] in ("2NF", "3NF"):
                f["message"] = f"({f['form']} candidate, not certain) " + f["message"]
                f["form"] = "UNCERTAIN"
        findings.append(_finding("UNCERTAIN", [], [],
                                 f"Candidate-key enumeration was stopped at its bound ({len(keys)} keys found); more candidate keys "
                                 "may exist, so which columns are prime (and therefore 2NF/3NF) is not verified."))
    # ---- 1NF hints (atomicity cannot be proven from a schema)
    flagged = [c["name"] for c in table["columns"] if any(h in c["data_type"].upper() for h in NON_ATOMIC_HINTS)]
    if flagged:
        findings.append(dict(_finding("UNCERTAIN", [], flagged,
                                      f"Column(s) {_cols(flagged)} may hold several values per cell; 1NF (atomic values) cannot be "
                                      "confirmed from the schema."), atomicity=True))
    stems = {}
    for c in colorder:
        m = re.match(r"^(.+?)_?\d+$", c)
        if m:
            stems.setdefault(m.group(1), []).append(c)
    repeating = [cols for cols in stems.values() if len(cols) >= 2]
    for cols in repeating:
        notes.append(f"Columns {_cols(cols)} look like a repeating group; if they hold the same kind of fact, 1NF suggests a "
                     "separate table (ignore if they are genuinely different attributes).")
    # ---- conclusion
    nf3, nf3_reasons = nf3_verdict(findings, keys_complete)
    forms = {f["form"] for f in findings}
    if "2NF" in forms:
        highest = "1NF"
    elif "3NF" in forms:
        highest = "2NF"
    elif nf3 == NF3_UNVERIFIED:
        highest = "UNVERIFIED"          # 3NF/BCNF cannot be claimed: more keys (or an unchecked key) could change the verdict
    elif "BCNF" in forms:
        highest = "3NF"
    else:
        highest = "BCNF"
    denorm = [d for d in denormalizations if d["table"] == name]
    below_3nf = nf3 == NF3_VIOLATED
    uncertain = [f["message"] for f in findings if f["form"] == "UNCERTAIN"]
    if below_3nf and denorm:
        status = "violation, justified denormalization"
    elif below_3nf:
        status = "VIOLATION, not justified"
    elif denorm and nf3 == NF3_UNVERIFIED:
        status = "declared denormalization, 3NF unverified"
    elif denorm:
        status = "declared denormalization"
    elif uncertain:
        status = "UNVERIFIED"
    else:
        status = "OK"
    return {"table": name, "columns": colorder,
            "fds": [(sorted(l), sorted(r)) for l, r in declared_fds],
            "candidate_keys": [list(k) for k in keys], "keys_complete": keys_complete, "prime": [c for c in colorder if c in prime],
            "nonprime": nonprime, "findings": findings, "notes": notes, "non_atomic_columns": flagged,
            "conditional_keys": cond_info, "uncertain": uncertain,
            # formal_3nf is True ONLY when 3NF is proven (see nf3_verdict); use nf3_status to tell "violated" from "unverified"
            "formal_3nf": nf3 == NF3_PROVEN, "nf3_status": nf3, "nf3_unresolved": nf3_reasons,
            "highest": highest, "status": status, "denormalized": bool(denorm), "unjustified": below_3nf and not denorm}


def analyze_design(design):
    """Analysis of every table of a canonical design. Returns {table name: result}."""
    phys = design["physical"]
    by_table = {}
    for f in phys["functional_dependencies"]:
        by_table.setdefault(f["table"], []).append((frozenset(f["determinant"]), frozenset(f["dependent"])))
    return {t["name"]: analyze_table(t, by_table.get(t["name"], []), phys["denormalizations"])
            for t in phys["tables"]}


def form_check(result, form):
    """'satisfied' / 'VIOLATED' / 'UNVERIFIED' for one normal form (cumulative: 3NF also needs 2NF ...).
    2NF/3NF are never 'satisfied' while the 3NF verdict is unverified (e.g. candidate-key enumeration cut off)."""
    order = {"2NF": {"2NF"}, "3NF": {"2NF", "3NF"}, "BCNF": {"2NF", "3NF", "BCNF"}}[form]
    if any(f["form"] in order for f in result["findings"]):
        return "VIOLATED"
    if result.get("nf3_status") == NF3_UNVERIFIED:
        return "UNVERIFIED"
    return "satisfied"


def normalization_problems(design):
    """Hard 2NF/3NF violations that are not justified by a declared denormalization, as repair instructions
    (used to send a generated design back for a targeted fix). Uncertain findings are NOT problems here:
    they are reported as warnings, never guessed either way. No schema is decomposed automatically."""
    out = []
    for name, a in analyze_design(design).items():
        if not a["unjustified"]:
            continue
        for f in a["findings"]:
            if f["form"] not in ("2NF", "3NF"):
                continue
            det, dep = _cols(f["determinant"]), _cols(f["dependent"])
            out.append(f"table '{name}': {f['message']} Fix: move column(s) {dep} into a table whose key is ({det}) and keep "
                       f"({det}) in '{name}' as a foreign key; or, if the dependency does not really hold, remove it from "
                       "'functional_dependencies'; or, only if deliberate, add a 'denormalizations' entry with a concrete justification.")
    return out
