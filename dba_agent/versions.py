"""Recoverable design versions (Stage 5B).

sessions/<id>/versions/v000/, v001/, ...   one folder per accepted revision (v000 = the design before the first
revision). Each folder holds copies of design.json, the four documents, dbms-choice.md, changelog.md and meta.json
(which also keeps the saved DBMS choice). Version N = the state after accepted revision N, so restoring a version
is just "commit that snapshot as a new revision" (see revision.restore_version) - history is never rewritten.
"""
import json
import os
import re
import shutil
from pathlib import Path

from .errors import DBAAgentError
from .state import atomic_write_text

META = "meta.json"
_VDIR = re.compile(r"^v\d{3,}$")
QUARANTINE = "_orphaned"          # snapshots of revisions that were never committed are moved here, never deleted/overwritten


class VersionConflict(DBAAgentError):
    """A different, valid snapshot already exists for that version number; it is never overwritten."""


def _fingerprint(design):
    from .approval import design_fingerprint        # local import: approval does not depend on versions
    return design_fingerprint(design)


def version_dir(session, number):
    return session.root / "versions" / f"v{number:03d}"


def has_version(session, number):
    return (version_dir(session, number) / META).is_file()


def _meta_of(folder):
    try:
        return json.loads((folder / META).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def quarantine_version(session, number):
    """Move the snapshot folder of `number` out of the version list (kept for inspection). -> new path or None."""
    folder = version_dir(session, number)
    if not folder.exists():
        return None
    base = session.root / "versions" / QUARANTINE
    base.mkdir(parents=True, exist_ok=True)
    k = 1
    while (base / f"v{number:03d}-{k}").exists():
        k += 1
    target = base / f"v{number:03d}-{k}"
    os.rename(folder, target)
    return target


def write_version(session, number, texts, meta, committed=None):
    """texts: {filename: text or None}; meta: JSON-able dict with `design_fingerprint`. -> True if a snapshot was created,
    False if an identical one (same design fingerprint) already existed and was kept.

    The snapshot is built in a temporary folder and renamed into place, so it is complete or absent (meta.json is
    written last). A valid snapshot is NEVER overwritten: another fingerprint at that number raises VersionConflict,
    except when `committed` (the number of committed revisions) shows that number was never committed - then the old
    folder is an orphan of an interrupted commit and is quarantined (moved to versions/_orphaned/), not deleted."""
    folder = version_dir(session, number)
    existing = _meta_of(folder) if folder.exists() else None
    if existing is not None:
        if existing.get("design_fingerprint") == meta.get("design_fingerprint"):
            return False
        if committed is None or number <= committed:
            raise VersionConflict(f"Version {number} already has a different snapshot (fingerprint "
                                  f"{str(existing.get('design_fingerprint'))[:12]}); it is not overwritten. Run `recover` to inspect.")
    if folder.exists():
        quarantine_version(session, number)               # partial or orphaned folder of an interrupted commit
    tmp = folder.with_name(folder.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    try:
        for name, text in texts.items():
            if text is not None:
                atomic_write_text(tmp / name, text)
        atomic_write_text(tmp / META, json.dumps(dict(meta, number=number), indent=2, ensure_ascii=False))
        os.rename(tmp, folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return True


def remove_version(session, number):
    """Delete a snapshot THIS process just created (rollback of a failed commit)."""
    shutil.rmtree(version_dir(session, number), ignore_errors=True)


def list_versions(session):
    base = session.root / "versions"
    out = []
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not _VDIR.match(d.name):
                continue
            m = _meta_of(d)
            if isinstance(m, dict) and "number" in m:
                out.append(m)
    return sorted(out, key=lambda m: m["number"])


def verify_version(session, number):
    """-> list of problems (empty = the snapshot is complete and its design.json matches the recorded fingerprint)."""
    folder = version_dir(session, number)
    meta = _meta_of(folder)
    if meta is None:
        return [f"Version {number} has no readable {META}."]
    try:
        design = json.loads((folder / "design.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"Version {number}: design.json is missing or unreadable."]
    want = meta.get("design_fingerprint")
    if want and _fingerprint(design) != want:
        return [f"Version {number}: design.json does not match the fingerprint recorded for it (the snapshot was modified)."]
    return []


def load_version(session, number):
    """-> (meta, design dict, dbms_choice or None). Raises DBAAgentError if missing or unreadable."""
    folder = version_dir(session, number)
    try:
        meta = json.loads((folder / META).read_text(encoding="utf-8"))
        design = json.loads((folder / "design.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        known = ", ".join(str(m["number"]) for m in list_versions(session)) or "none"
        raise DBAAgentError(f"Version {number} not found or unreadable (available versions: {known}).")
    problems = verify_version(session, number)
    if problems:
        raise DBAAgentError(problems[0])
    return meta, design, meta.get("dbms_choice")
