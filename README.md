# DBA Agent - all six stages implemented

CLI agent that uses an LLM as a database designer. **All six stages are implemented**: intake, requirements interview, DBMS
selection, design documentation (canonical `design.json` + four Markdown documents), the revision loop (natural-language
changes propagated to every document, `changelog.md`, version snapshots, restore), **explicit approval** of the design, and
**SQL generation** (`schema.sql` + validated `seed.sql`). Stages 1-4 run with `resume`; Stages 5 and 6 use dedicated
commands (`review`, `revise`, `approve`, `schema`, `seed`). The project foundation provides a configurable provider, JSON
session persistence, external prompts and an LLM call log.

**Verification status (please read):** the SQL has been *execution-tested on SQLite only*. PostgreSQL, MySQL/MariaDB and
SQL Server output is checked statically (and through fake connections in the tests) and **still requires validation on real
servers**. No live LLM run is claimed by the automated tests (they use mocked responses).

Pure standard library, Python 3.9+. No `pip install` needed.

## Install

```bash
unzip dba_agent.zip && cd dba_agent
python3 --version        # 3.9 or newer
```

## Configure

Copy an example and edit it (never commit real keys; `config.json` is git-ignored):

```bash
cp config.example.json config.json                    # Ollama (local)
# or
cp config.openai-compatible.example.json config.json  # OpenAI / GapGPT / LM Studio / vLLM ...
export OPENAI_API_KEY=sk-...                          # the variable named by "api_key_env"
```

| Key | Meaning |
|---|---|
| `provider` | `ollama` or `openai_compatible` |
| `model` | model name, e.g. `llama3.1`, `gpt-4o-mini` |
| `base_url` | Ollama: `http://localhost:11434`; OpenAI-compatible: URL ending in `/v1` (e.g. a GapGPT gateway URL) |
| `api_key_env` | name of the env var holding the API key (or `api_key` directly - not recommended) |
| `temperature`, `timeout` | sampling / HTTP timeout (s) |
| `max_attempts`, `retry_delay` | retries on connection errors and invalid model output |
| `json_mode` | ask the server for JSON output (Ollama `format:json`, OpenAI `response_format`); leave `false` if a gateway rejects it |
| `sessions_dir` | where sessions are stored (default `sessions`) |

Environment overrides (no file edit needed): `DBA_AGENT_PROVIDER`, `DBA_AGENT_MODEL`,
`DBA_AGENT_BASE_URL`, `DBA_AGENT_API_KEY`, `DBA_AGENT_CONFIG` (config path).

## Commands

```bash
python3 -m dba_agent new                 # start a session, type the description, end with a line "."
python3 -m dba_agent new --file brief.txt
python3 -m dba_agent resume [SESSION_ID] # default: latest session
python3 -m dba_agent list
python3 -m dba_agent logs [SESSION_ID] [--full]   # inspect LLM calls
python3 -m dba_agent design-draft [SESSION_ID] [--force]   # Stage 4A only (design.json + conceptual-model.md); optional
python3 -m unittest discover -s tests -v          # run tests (offline, mocked LLM)
```
Global options: `--config PATH`, `--sessions-dir DIR`.

## Stage 2 - requirements interview

After intake, the CLI asks `Continue to stage 'interview'? [Y/n]`.
1. The LLM writes round-1 questions covering all 7 required categories (entities/attributes,
   relationships/cardinalities, volume/growth, read-write/reporting, transactions/concurrency,
   security/roles/privacy, business rules). Answer one line each; Enter skips a question.
2. The LLM reads your real answers and writes a follow-up round (at least one follow-up round is
   enforced in code). After each later round it decides itself whether it has enough information
   (hard cap: 4 rounds, `MAX_ROUNDS` in `stages/interview.py`).
3. It writes `output/requirements.md` (summary, findings per category, open issues, decision to
   stop, full transcript) and stores it in `state.json` as `interview` / `requirements`.

Progress is saved after every answer. Quit any time (Ctrl-C / Ctrl-D) and run
`python3 -m dba_agent resume` - answered questions are kept and nothing is regenerated.
`resume` is a generic workflow controller: it runs whatever stage the session is at; it only asks
for confirmation before *starting* a new stage. Prompts: `prompts/interview_*.md`.
LLM log stage labels: `interview:questions`, `interview:review`, `interview:requirements`.

Interview completeness rules: "enough information" is never accepted while a category has no
substantive answer (all-skipped or "idk"-style answers) - the skipped questions are asked again.
At the round limit the model is still asked which gaps remain; skipped questions, uncovered
categories and remaining gaps are saved under `requirements.unresolved` in `state.json`, shown in
`requirements.md` (with a WARNING banner when incomplete) and passed to Stage 3.

## Stage 3 - DBMS selection

Reads the saved requirements and asks the LLM to compare PostgreSQL, MySQL/MariaDB, SQLite and
SQL Server on 7 criteria (volume, concurrency, transactions, security, reporting, deployment,
maintainability). The answer is validated (all 4 DBMSs rated on every criterion, every rejected
option explained), then saved as `state.json -> dbms_choice` (`selected_id`: `postgresql`,
`mysql_mariadb`, `sqlite` or `sql_server`, plus justification, comparison, rejections,
design implications) and rendered to `output/dbms-choice.md`. Prompts: `prompts/dbms_*.md`;
log label `dbms:selection`.

## Session layout

```
sessions/<id>/
  state.json              # current_stage, completed_stages, description, intake data, last_error
  output/intake-summary.md, output/requirements.md, output/dbms-choice.md
  logs/llm_calls.jsonl    # one JSON line per LLM attempt: stage, input, output, attempt, error, duration
```
`current_stage` is the stage that runs next (`intake` -> `interview` after Stage 1).
If the LLM is unreachable, the session and description are kept; run `resume` to retry.

## Example run

```
$ python3 -m dba_agent new
Describe the system you need a database for.
Finish with a line containing only '.' (or Ctrl-D / Ctrl-Z):
I run a pet boarding business. Owners book stays for their pets, staff assign kennels...
.
Session: 20261108-101500-a1b2  (sessions/20261108-101500-a1b2)
Running stage 'intake' with ollama/llama3.1 ...
Stage 'intake' done.
--- Intake summary ... ---
# Intake Summary - Pet Boarding
## Overview ...
Resume later with: python3 -m dba_agent resume 20261108-101500-a1b2
```

## Layout

```
dba_agent/cli.py       CLI commands          dba_agent/state.py   session persistence
dba_agent/config.py    config + env          dba_agent/logger.py  JSONL LLM-call log
dba_agent/llm.py       Ollama/OpenAI client  dba_agent/prompts.py prompt loader ({{var}})
dba_agent/workflow.py  stage registry        dba_agent/stages/    one module per stage
dba_agent/design_model.py canonical design   dba_agent/design_docs.py  Stage 4 documents + checker
dba_agent/normalization_analysis.py  FD/key/normal-form analysis
prompts/               editable prompts      tests/               unittest suite
```

Adding a stage: create `stages/<name>.py` with `run(session, llm, prompts)`, add prompts to
`prompts/`, register it in `workflow.IMPLEMENTED`.
Sessions that stopped at Stage 4A (saved `design`, stage still `design`) continue with `resume`; no LLM call is repeated.

## Testing status

All automated tests (`python3 -m unittest discover -s tests`) pass offline with mocked LLM responses.
Stage 4 is covered by `tests/test_design_model.py`, `test_design_stage.py` (4A), `test_normalization.py`, `test_normalization_3nf.py` and
`test_stage4_complete.py` (4B, two different design scenarios). Live Ollama / OpenAI-compatible calls have **not**
been tested against a real model, and SQL generation is described in the Stage 6 sections below (SQLite execution-tested; other DBMSs still need real-server validation).

## Stage 4 - design documentation (complete)

Stage 4 runs after Stage 3 (`resume` asks "Continue to stage 'design'?"). Output in `sessions/<id>/output/`:

| File | Content |
|---|---|
| `design.json` | canonical design = single source of truth (machine-readable) |
| `conceptual-model.md` | entities, attributes, relationships, cardinalities, Mermaid ER diagram, business rules |
| `logical-schema.md` | per table: columns (type, nullability, default, key), PK, FKs with ON DELETE/UPDATE, UNIQUE, CHECK, indexes; relationship implementation; business-rule enforcement; Mermaid relational diagram |
| `normalization.md` | functional dependencies, candidate keys, 2NF/3NF/BCNF verdict per table, denormalizations with justification, warnings, limitations |
| `assumptions.md` | assumptions, assumed/unenforceable rules, open questions, unresolved interview requirements, design-review warnings |

### How it works
1. **Two LLM calls** (`design:conceptual`, `design:logical`, described under "Stage 4A" below) produce the canonical
   design. Nothing else in Stage 4 calls the LLM.
2. The four Markdown documents are **rendered by code** (`design_docs.py`) from the canonical design, so they cannot
   contradict it or each other. `normalization_analysis.py` computes attribute closures, candidate keys and normal forms.
3. The files are read back from disk and compared with the design (`design_docs.check_documents`): tables, columns,
   types, nullability, defaults, keys, FK actions, UNIQUE/CHECK/indexes, relationship implementation, rule enforcement,
   both Mermaid diagrams, normal-form verdicts, FDs, assumptions. Any difference (missing, altered or extra content)
   stops the stage with a list of problems.
4. Only when that check passes is Stage 4 marked complete (`completed_stages` += `design`, `current_stage` = `revision`).
   Re-running Stage 4 reuses the saved design (no LLM calls) and re-renders the documents, so an interrupted run or a
   hand-edited/corrupted document is repaired by running `resume` again.

### Limitations (please read)
* The document check shows that the files **match `design.json`**; it does **not** prove the design is correct or that
  the future SQL will run. Only a real DBMS can show that (Stage 6 / optional bonus).
* Keys for normalization are only PRIMARY KEY and UNIQUE keys over NOT NULL columns without a condition. A UNIQUE over
  nullable columns or a partial unique index is a *conditional key*: it is listed in `normalization.md`, never used as a
  candidate key, and a dependency that rests on it is reported as UNCERTAIN (status `UNVERIFIED`), not as OK.
  Partial unique indexes are declared with an optional structured `"where"` list on an index, e.g.
  `{"name": "uq_active_copy", "columns": ["copy_id"], "unique": true, "where": [{"column": "status", "operator": "=", "value": "active"}]}`
  (operators `=`, `<>`, `IN`, `IS NULL`, `IS NOT NULL`; not available for MySQL/MariaDB). `schema.sql` renders it as `WHERE ...`.
  Formal 3NF is checked (every X -> A: X superkey or A prime); the design step is sent back to repair violations
  (bounded retries) and a remaining violation is reported as a warning, never hidden.
* Normalization analysis is only as good as the functional dependencies the LLM declares. An undeclared dependency
  cannot be found, so "no violation" means "none among the declared dependencies". 1NF cannot be verified from a schema
  (only JSON/ARRAY/XML-like column types are flagged). Declared keys are the only keys known.
  A table below 3NF without a justified denormalization produces a **warning** (CLI output, `normalization.md`,
  `assumptions.md`, `state.json["design_docs"]`); it does not block the stage.
* A UNIQUE key containing nullable columns is NOT treated as a key for the analysis (it is a conditional key, see above).
* The Mermaid diagrams are checked structurally (`check_mermaid_syntax`), not by the real Mermaid parser.
* Nothing in Stage 4 is hard-coded to a scenario; the tests use two unrelated demo domains.

## Stage 4A - canonical design + conceptual model (first half of Stage 4)

`resume` runs this as part of Stage 4. The optional `design-draft` command runs **only** this half
(`design.json` + `conceptual-model.md`) and leaves `current_stage`/`completed_stages` untouched; re-running reuses the
saved design, `--force` regenerates it:

```bash
python3 -m dba_agent design-draft [SESSION_ID] [--force]
```

### What it does
1. Reads `requirements` (Stage 2, only the *active* unresolved gaps) and `dbms_choice` (Stage 3).
2. LLM call `design:conceptual` -> entities, attributes, identifiers, relationships (min/max cardinalities),
   business rules, assumptions, open questions. Validated by `design_model.validate_conceptual`.
3. LLM call `design:logical` -> tables, columns, PK/FK (ON DELETE/UPDATE), UNIQUE, CHECK, indexes, functional
   dependencies, denormalizations, and how every business rule is enforced. Validated by
   `design_model.validate_physical` against the conceptual model.
4. The merged design is validated once more as a whole, relationships are linked to their physical
   implementation, and it is saved to `state.json["design"]` and `output/design.json`.
5. `output/conceptual-model.md` (entities, key attributes, relationships + cardinalities in words, Mermaid
   ER diagram, business rules, assumptions, remaining ambiguities) is rendered **by code** from the design,
   not by the LLM, so it cannot contradict `design.json`.

Each LLM call is retried up to `max_attempts` times; the validator's complete error list is fed back to the model
and every attempt is logged (`design:conceptual`, `design:logical`). If the second call fails, the first result
is kept in `state.json["design_progress"]` and not repeated on the next run.

### design.json layout (schema_version 1)
`conceptual` {entities, relationships} | `physical` {tables, functional_dependencies, denormalizations} |
`business_rules` (each with `enforcement`: schema / trigger / application + the constraint names) | `assumptions` |
`open_questions` | `requirement_gaps` | `dbms` | `meta`.
* **Entities vs tables are separate.** Many-to-many relationships stay many-to-many in `conceptual`; the junction
  table exists only in `physical` (`source: {type: relationship, id}`), and each relationship gets a derived
  `implemented_by` (FK or junction table). Each table has `source` (entity/relationship), each FK a `relationship` tag.
* **Cardinality** reads across (Mermaid style): `{min: 0|1, max: 1|"N"}` = how many of *this* participant relate to
  one instance of the *other*. `type` (one_to_one / one_to_many / many_to_many) and the one/many side are derived.
* Code-level consistency checks (violations are sent back to the LLM): one table per entity, a column per
  attribute (nullable iff optional), every identifier enforced (PK or UNIQUE), FK on the many side with
  nullability = optionality, UNIQUE FK for 1:1, junction table with a key containing both FKs, weak-entity FK inside
  the PK, FK targets are keys, SET NULL only on nullable columns, unique constraint/index names, no reserved SQL
  words as identifiers, no RESTRICT on SQL Server, an FD for every table, every business rule mapped to an enforcement.
* Prompts: `prompts/design_*.md` (dialect notes per DBMS in `design_dialect_<dbms_id>.md`).

## Stage 2 changes in this delivery
* Skipped questions are no longer "unresolved" forever. A later question settles earlier ones through
  `resolves` links (set by the coverage guard when it re-asks, or by the reviewing model via the optional `resolves`
  field), but only if the settling question itself received a usable answer. `requirements.unresolved` now has
  `skipped_questions` (full history, each marked `resolved` / `resolved_by`), `open_questions` (the active list used
  by `requirements.md`, Stage 3 and Stage 4) and `not_applicable_questions`. Older saved sessions still work.
* An explicit "not applicable" (`n/a`, `not applicable`, `does not apply`, `n/a - no online payments` ...) is a
  deliberate answer, not missing information: it is not listed as unresolved, it can cover a category (except
  *entities and attributes*), and the requirements prompt records it as a finding. `skip`, `idk`, `?`, empty
  stay "missing".

## Tests
`python3 -m unittest discover -s tests` (129 tests, offline). If Node and the `mermaid` package are installed
(set `MERMAID_CORE` to `.../mermaid/dist/mermaid.core.mjs`), one test additionally runs the real Mermaid parser on
the generated ER diagram; otherwise it is skipped.


## Stage 5A - revision engine (natural-language design changes)

```bash
python3 -m dba_agent revise [SESSION_ID] --request "Make owner-pet many-to-many"   # one change
python3 -m dba_agent revise [SESSION_ID]                                            # prompt loop, empty line stops
```

How a revision works (`dba_agent/revision.py`, prompts in `prompts/revision_system.md` / `revision_user.md`):

1. The configured LLM receives the current `design.json` and the request and returns the complete revised design
   plus a summary, rationale, assumptions and warnings. Handles add entity/attribute, remove attribute,
   cardinality change, business rule, constraint change and DBMS switch (all types/dialect elements rewritten).
2. The whole candidate is validated with `design_model.validate_design` (+ DBMS-switch checks, no-op check). Invalid
   answers are fed back to the LLM (bounded retries); if they still fail, nothing changes.
3. What changed is computed by code (diff of old/new design), not taken from the LLM.
4. All four documents are re-rendered from the candidate, checked (`check_documents`, in memory and again from disk),
   normalization is re-run, `changelog.md` is rewritten from the revision history, and only then are
   `design`, `revisions` and the file set committed to `state.json`. A failure restores the previous files.
5. An interrupted revision is stored as `revision_pending`; the next `revise` (with or without a request) finishes it.

`changelog.md` entries: number, timestamp, original request, what/why, affected entities/relationships/tables/
constraints/rules, regenerated files, assumptions, warnings, check results. Failed requests are kept in
`state.json` under `revision_failures` (not in the changelog).

Stage 5 ends only with the explicit `approve` command (Stage 5B below). The session stays at stage `revision` until then;
`resume` points to `revise` / `approve`.

## Stage 5B - safe revisions and explicit approval

```
python -m dba_agent revise [SESSION_ID] [--request "..."] [--yes]   # propose -> preview + diff -> accept (y) / reject (N)
python -m dba_agent versions [SESSION_ID]                           # v000 = original design, vN = after revision N
python -m dba_agent restore [SESSION_ID] --version N [--yes]        # bring back an earlier version (as a new revision)
python -m dba_agent review [SESSION_ID]                             # overview + can it be approved? (changes nothing)
python -m dba_agent approve [SESSION_ID] [--yes]                    # explicit final approval -> stage becomes `sql`
```

* Each proposal costs one LLM call. Before anything is written you see the summary and a diff computed by code.
  The default answer is **no**. `--yes` accepts ordinary proposals without asking, but it **never** accepts a
  *suspicious* proposal: with `--yes`, a flagged proposal is still shown and you are asked explicitly (no answer = no).
* "Suspicious" means the deterministic diff contains changes outside the likely scope of the change types the LLM
  declared for the request: removals in an add-only request (e.g. "add pet weight" that drops a phone number), or
  added/removed/modified entities, attributes, relationships, constraints, business rules or a DBMS change that the
  declared types do not normally touch (`revision.SCOPE`). This is a **heuristic on declared change types, not proof
  of what you intended**: a flagged change may be legitimate and an unflagged one may still be unwanted - always read
  the diff. Requests declared as `other` are not scope-checked.
* **Library level:** the revision is three separate steps - `revision.propose()` (LLM + validation + diff, writes
  nothing), `revision.accept()` / `revision.reject()` (the review; suspicious proposals need
  `acknowledge_suspicious=True`) and `revision.commit_accepted()` (writes files; refuses anything not accepted or
  stale). `revision.revise(..., confirm=callback)` runs all three, with the callback as the review. Without a callback
  (`confirm=None`, for scripts/tests) ordinary proposals commit as before but suspicious ones raise `RevisionRejected`
  unless `allow_suspicious=True` is passed explicitly. The CLI always passes a callback.
* A rejected or failed revision leaves `design.json`, all documents, `state.json` and the changelog untouched.
* A DBMS switch also updates `state.json["dbms_choice"]` and `dbms-choice.md`: the user override is recorded
  (who/when/why) and the original analysis is kept and marked *superseded*.
* Snapshots are stored in `sessions/<id>/versions/vNNN/` (design, documents, changelog, DBMS choice).
* `approve` refuses when the design is invalid, the documents/`design.json`/DBMS choice disagree, a revision is
  pending, or a table is below 3NF without justification. The normalization checks only know the *declared*
  functional dependencies - this limitation is printed every time. Approval stores time, version, a SHA-256
  fingerprint of the canonical design and SHA-256 hashes of `design.json`, the four Stage 4 documents and
  `dbms-choice.md`; any later accepted revision (or restore) invalidates it.
* **Approval integrity:** Stage 6 calls `approval.verify_approved_design(session)`, which refuses when the design is not
  approved or was revised since, the approved fingerprint differs from the session design or from `design.json` on disk,
  a revision is pending, the saved DBMS choice differs from the design's DBMS, or `design.json` / any Stage 4 document /
  `dbms-choice.md` is missing, corrupted, modified after approval (hash mismatch) or inconsistent with the design.
  Verification is read-only: tampered files are **reported, never silently repaired** (use `restore` or `revise`).
  Approvals made before hashes existed must be re-confirmed with `approve` (it only re-verifies and records hashes).

## Stage 6A - schema.sql generation (seed.sql: see Stage 6B below)

```
python -m dba_agent schema [SESSION_ID] [--force] [--status]
```

Deterministic (no LLM): `dba_agent/schema_sql.py` translates the **physical** part of the verified, approved `design.json`
into `output/schema.sql` for PostgreSQL, MySQL/MariaDB, SQLite or SQL Server. The same design always gives byte-identical SQL.

* **Approval first.** `schema` calls `approval.verify_approved_design()` (see Stage 5B): not approved / revised since /
  fingerprint mismatch / pending revision / DBMS mismatch / any of `design.json`, the four documents or `dbms-choice.md`
  missing or modified -> nothing is generated and the problems are listed. SQL is built from the verified design in
  session state, never from Markdown.
* **State.** `state.json["sql_stage"]["schema"]` stores the approved-design fingerprint, the SHA-256 of `schema.sql`,
  table order, notes and `executed_against_dbms: false`. `schema --status` recomputes validity: `current`, `stale` (design
  revised/approval gone), `modified` (file edited), `missing_file`, `incomplete` (interrupted run). Re-running `schema`
  repairs `incomplete`/`missing_file`/`stale` (once re-approved); `--force` also rewrites a `modified` file. Stage 6 is **not**
  marked complete (`completed_stages` is unchanged) until seed.sql exists.
* **What is generated:** CREATE TABLE in foreign-key order (columns, dialect types, NOT NULL, DEFAULT, identity, PK incl.
  composite, UNIQUE, CHECK, FK incl. composite with ON DELETE/UPDATE), then CREATE INDEX. Every identifier is quoted.
  Circular table dependencies: the closing FK becomes `ALTER TABLE ... ADD CONSTRAINT` (SQLite accepts forward references).
  Identity: PostgreSQL `GENERATED BY DEFAULT AS IDENTITY` (so seed data may give explicit ids), MySQL `AUTO_INCREMENT`,
  SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`, SQL Server `IDENTITY(1,1)`.
* **Refused with a clear error (no silent fallback):** types of another dialect, UNSIGNED outside MySQL, unknown defaults
  (supported: numbers, `'text'`, TRUE/FALSE, NULL, CURRENT_TIMESTAMP/NOW(), CURRENT_DATE, CURRENT_TIME, UUID generation),
  CHECK expressions with subqueries/`;`/comments/foreign-dialect syntax, `SET NULL` on NOT NULL columns, `SET DEFAULT` without a
  default or on MySQL, identity on non-integer or (MySQL/SQLite) non-PK columns, TEXT/BLOB/(MAX) key columns on MySQL/SQL Server,
  over-long MySQL keys, SQL Server cascade cycles / multiple cascade paths / cascading self-references, name clashes the DBMS
  would reject. SQL Server specifics: RESTRICT is emitted as NO ACTION (noted), UNIQUE over nullable columns becomes a filtered
  unique index so several NULLs stay possible.
* **Conditional keys, NULL-strict checks, structured triggers.**
  - A *partial* unique index needs the structured `where` list on the index (e.g. one active loan per copy:
    `{"columns": ["copy_id"], "unique": true, "where": [{"column": "status", "operator": "=", "value": "active"}]}`);
    SQLite/PostgreSQL get `CREATE UNIQUE INDEX ... WHERE ...`, SQL Server a filtered index, MySQL/MariaDB is refused. An optional index flag
    `"conditional": true|false` says explicitly whether the uniqueness is conditional: `true` without `where` is refused,
    `false` is always an ordinary unique index. Without the flag, only declarative wording in `purpose` ("partial unique index",
    "conditional key", "unique only when ...") is refused as a missing `where`; a bare "WHERE" in prose (e.g. "used in queries
    WHERE status is active") no longer counts. Older approved sessions keep their `design.json` untouched.
  - SQL passes a CHECK whose result is UNKNOWN (NULL). A CHECK that tests NULLs explicitly (`IS [NOT] NULL` combined with AND/OR)
    is therefore generated NULL-strict, `CASE WHEN (...) THEN 1 ELSE 0 END = 1`, so e.g. a voided record with a missing actor or
    reason is rejected. Plain checks such as `price > 0` keep standard SQL behaviour (NULL passes; use NOT NULL to forbid it).
  - Optional per-table `triggers` in `design.json`: `append_only` (`operations`: UPDATE and/or DELETE) and `immutable_columns`
    (`columns`). They are generated for **SQLite only** (executed in the tests); for every other DBMS the design validator and the
    generator refuse them with a message, and the rule must be declared `application`. For SQLite the protection no longer
    depends on a connection setting: BEFORE INSERT/UPDATE guard triggers reject `INSERT OR REPLACE` / `REPLACE INTO` /
    `UPDATE OR REPLACE` (and upserts / `INSERT OR IGNORE`, which raise) that would delete an existing row through its rowid,
    primary key, UNIQUE constraint or unique index. `PRAGMA recursive_triggers = ON` is still emitted as extra protection only.
    Limits: anyone who can `DROP TRIGGER`/`DROP TABLE`, use `writable_schema` or replace the file defeats it; `immutable_columns`
    still allows `DELETE` followed by `INSERT`; `append_only` with `operations` that omit `DELETE` intentionally allows
    delete-and-reinsert. PostgreSQL / MySQL-MariaDB / SQL Server triggers are not generated (unverifiable here): there the
    rule must be enforced in the application.
  - **What is enforced?** `implemented_by` names are checked against the constraints, indexes, columns and triggers the
    generator really emitted (names found in the generated SQL, right kind and table, trigger operation coverage). The
    `schema.sql` header lists rules ENFORCED (mapping verified; the script cannot prove an object expresses the whole rule),
    rules whose mapping could NOT be fully verified (missing/unknown name, non-unique index, nullable column, append-only
    trigger blocking only one operation - not counted as enforced), and rules that need application logic. Each enforced entry
    in `state.json` (`rule_enforcement`) lists its objects and conditional caveats (e.g. SQLite needs `foreign_keys = ON` per
    connection). `logical-schema.md` still shows the *declared* mechanism; `schema.sql` is the source of truth.
  - `sql_validate.split_statements` keeps a `CREATE TRIGGER ... BEGIN ... END` body (with its inner `;`) in one statement.
* **Verification level (please read).** Only **SQLite** output is actually *executed* in the tests (`sqlite3`). PostgreSQL,
  MySQL/MariaDB and SQL Server output is checked **statically** (quoting, dialect tokens, ordering, rule checks) and has
  **not** been run on a real server. CHECK expressions are copied from the design after a lexical check, not parsed.

## Stage 6B - seed.sql, SQL validation and Stage 6 completion

```
python -m dba_agent seed [SESSION_ID] [--rows N] [--force] [--regenerate] [--status]
```

Order of work: `approve` -> `schema` -> `seed`. `seed` needs a valid approval **and** a *current* schema.sql
(missing / modified / stale schema.sql is reported with the command that fixes it; nothing is written, no LLM call is made).

**Architecture (the LLM never writes SQL)**
1. `design.json` (approved) -> `seed_data.build_model` (types, keys, FKs, CHECKs, identity; unseedable schemas raise a
   specific `SeedError` *before* the LLM is called, e.g. a cycle of NOT NULL foreign keys or a NOT NULL BLOB column).
2. The configured LLM proposes `{"tables": {table: [row, ...]}}` (prompts: `prompts/seed_system.md`, `prompts/seed_user.md`;
   logged as stage `seed`, every attempt). Reuses the normal provider config, retries and call log.
3. `seed_data.validate_records` (Python): every table populated, columns/types/lengths/ranges, NOT NULL, PRIMARY KEY, UNIQUE
   (case-insensitive on MySQL/SQL Server), FOREIGN KEYs (composite, optional, self-referencing, no row loops), CHECK
   constraints (evaluated in an in-memory SQLite; expressions SQLite cannot evaluate are reported as *not evaluated*),
   placeholder values (`test1`, `item2`, ...). Errors are fed back to the LLM (retry). *Coverage* findings (optional
   relationships with NULL and non-NULL rows, every allowed status value used, many-to-many partners) also trigger a retry
   (bounded by `max_attempts`); if feasible coverage is still missing afterwards, Stage 6 stays open (see *Sample-data coverage*).
4. `seed_data.render_seed_sql` writes `seed.sql` deterministically: parents before children, self-references ordered,
   circular foreign keys loaded as NULL and fixed with `UPDATE`, identity values explicit (`SET IDENTITY_INSERT` on SQL
   Server, `setval` on PostgreSQL), dialect-specific literals, wrapped in one transaction.
5. Validated records are saved first in `output/seed-data.json` (recovery point): after an interrupted run `seed` rebuilds
   `seed.sql` from them **without calling the LLM** (`--regenerate` / `--force` ask the LLM again).

**Rows**: `--rows N` or config `"seed_rows"` (default 8; `--rows 0` or a negative number is an error). **Every** table must have at least N rows. Only where the
schema makes that impossible is the target lowered and reported as a warning: a UNIQUE/PK made only of foreign keys
(1:1 and link tables) allows at most the product of the parents' row counts, and a unique column limited by a CHECK `IN (...)`/ENUM
list allows at most that many rows.

**Validation levels (never mixed up)**
| DBMS | What happens | Reported level |
|---|---|---|
| SQLite | schema.sql then seed.sql are *executed* in a fresh in-memory DB with `foreign_keys=ON`; `foreign_key_check`, `integrity_check`, row counts, NULL counts (optional relationships) and status values are compared with the validated data | `executed` / `failed` |
| PostgreSQL, MySQL/MariaDB, SQL Server | executed only if config has `validation.<dbms id>` settings, the driver is installed and the target database is **empty** (use a throw-away database/container; it is left populated) | `executed` / `failed`, otherwise `unverified` with the reason |

```json
"validation": {
  "postgresql":    {"dsn": "postgresql://user:pass@localhost:5432/scratch"},
  "mysql_mariadb": {"host": "localhost", "port": 3306, "user": "root", "password": "...", "database": "scratch"},
  "sql_server":    {"connection_string": "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=scratch;UID=sa;PWD=...;TrustServerCertificate=yes"}
}
```
Drivers: `psycopg` (or `psycopg2`), `pymysql`, `pyodbc`. SQLite is **never** used as a stand-in for another DBMS.
`unverified` means only static checks ran (statement count, every table inserted, parent-before-child order).

**Completion and freshness.** Stage 6 is marked complete (`current_stage = done`) only when schema.sql is current and
seed.sql was generated and validated at the available level (`failed` never completes; `unverified` completes but is
labelled in the CLI output and in `state.json`). `seed --status` recomputes: `missing`, `incomplete` (interrupted),
`missing_file`, `modified`, `stale` (design revised / approval invalid / schema.sql changed), `failed_validation`, `current`.
An accepted revision cancels the approval, reopens Stage 6 and makes both SQL files stale; regenerate with
`approve`, `schema`, `seed`.

**Sample-data coverage.** Feasible coverage is required: optional columns/relationships need both NULL and non-NULL rows, every
CHECK `IN (...)`/ENUM status value (up to 12) must appear, and many-to-many link tables must link a parent to several partners.
The LLM gets actionable feedback and retries (bounded by `max_attempts`, no endless loop). Requirements that are impossible or
not applicable (more allowed values than rows, a CHECK that forbids NULL, a link table whose other side has <2 rows) are listed
as `coverage not applicable` warnings and never block. If feasible coverage is still missing after the last attempt, `seed.sql`
is written but reported as `coverage missing`, Stage 6 stays open and `seed --regenerate` (optionally `--rows N`) asks again.
`seed --status` also refreshes Stage 6 completion: a missing, modified or stale file reopens it.

**Honest limitations**: PostgreSQL / MySQL / SQL Server paths were *not* run against real servers while developing
(only through fake connections in the tests); CHECK evaluation in Python uses SQLite semantics; the whole dataset is
requested in one LLM call (very large schemas may need a larger model or smaller `--rows`); binary columns are only
seeded with NULL / defaults; no automatic LLM repair loop (that is the optional bonus, not implemented).

## Stage 6B seed validation (Batch 3)

- `seed` reports six separate facts: seed generation, SQL execution, referential integrity, business rules, sample-data
  coverage and Stage 6 overall. A coverage-only gap is the state `coverage_incomplete` (SQL passed), never `failed_validation`.
- Partial UNIQUE indexes (structured `where`) are checked only among rows that satisfy the condition. Composite foreign keys
  with a NULL component need no parent row. NULL-example coverage is required only when feasible.
- A missing NULL example is repaired offline from the saved data (no LLM call) when one cell can be changed and the whole
  dataset still validates; every repair is listed in `seed-data.json`, `state.json` and the CLI output - review it.
  `--force` / `--regenerate` still ask the LLM again; `--rows` and `--status` are unchanged.
- Only SQLite is executed here. PostgreSQL, MySQL/MariaDB and SQL Server results stay "unverified" unless a scratch server is configured.

## API provider errors (Batch 5A)

`revise` and the other stages now distinguish provider failures from design results:

| Message | Meaning |
|---|---|
| `API ERROR ... (HTTP 401/403/429/503, timeout, network)` | The request failed at the provider; nothing was evaluated and the session is untouched. The HTTP status and provider request id are shown when available; keys are never printed. |
| `INVALID MODEL RESPONSE` | The provider answered, but the reply was empty, not JSON or the wrong shape. |
| `REVISION REJECTED` | A usable proposal was rejected by local validation. |

Quota is reported only when the provider's own error text says so. Providers are chosen purely by config
(`config.gemini.example.json` for Google's OpenAI-compatible endpoint, `config.gapgpt-backup.json` for GapGPT);
a missing/empty `api_key_env` variable is reported by name. Changing provider never alters existing sessions.

## Final release (Batch 6C) - verification summary and Windows 11 quick start

**What the automated tests prove.** Stages 1-5 run through the real stage code with scripted (mocked) LLM answers; Stage 6A is
deterministic Python; the SQL is *executed on disposable SQLite databases* with positive and negative tests
(`tests/test_batch6c_final.py`). **Mocked LLM answers prove the workflow, not that a real model understands an unseen
scenario** - use a real scenario for the final demonstration. PostgreSQL, MySQL/MariaDB and SQL Server scripts are generated and
checked statically only; they were **not** executed on real servers.

**Known limitations (not fixed on purpose):**
- A conditional CHECK over nullable columns *without* an explicit `IS [NOT] NULL` test (e.g. `status <> 'x' OR length(trim(note)) > 0`)
  accepts NULL, as SQL always does. The generator keeps the SQL unchanged and now states this in `schema.sql` (NOTE line) instead of
  implying full enforcement. Make the column NOT NULL or write explicit NULL tests (then the CHECK is generated NULL-strict).
- Overlapping time ranges and "employee provides this service" are application-level rules unless the design models them (e.g. a
  composite foreign key); they are reported as *application logic*, never as enforced.
- Seed data is checked against declared constraints only; free-text business rules are not interpreted.

### Windows 11 PowerShell

```powershell
# 1. Install dependencies: only Python 3.9+ is needed (standard library only)
python --version                       # if missing: winget install Python.Python.3.12
cd C:\path\to\dba_agent_final          # the unzipped folder (Expand-Archive .\dba_agent_final.zip .)

# 2. Configure the AI provider (config.json already points to Gemini's OpenAI-compatible endpoint)
$env:GEMINI_API_KEY = "paste-your-key-here"        # current window only; the key is never written to a file
Get-Content .\config.json                          # check provider / model / base_url / api_key_env
# other providers: Copy-Item .\config.openai-compatible.example.json .\config.json  (then set $env:OPENAI_API_KEY)

# 3. Start a NEW scenario (type the description, finish with a line containing only a dot)
python -m dba_agent new
python -m dba_agent new --file .\brief.txt         # or read the description from a file

# 4. Resume a session (default: the latest) and list sessions
python -m dba_agent list
python -m dba_agent resume                         # or: python -m dba_agent resume 20261116-101500-ab12

# 5. After Stage 4: review, revise (optional), approve, then generate SQL
python -m dba_agent review
python -m dba_agent revise --request "add a phone number to customers"
python -m dba_agent approve
python -m dba_agent schema                         # schema.sql (no LLM call)
python -m dba_agent seed --rows 5                  # seed.sql + real SQLite execution check (when SQLite was chosen)
python -m dba_agent seed --status                  # final status: current files, validation level, Stage 6 complete or not

# 6. Run the tests (offline, mocked LLM, no API key needed)
python -m unittest discover -s tests -q
```
Generated files are in `sessions\<SESSION_ID>\output\` (`schema.sql`, `seed.sql`, `seed-data.json`, design documents).
Re-running `seed` after an interrupted run reuses saved sample data; a revision after approval makes the SQL stale and reopens Stage 6.
