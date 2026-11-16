"""Session state: JSON persistence + per-session directory layout.

sessions/<session_id>/
    state.json              <- full session state (resume from here)
    output/                 <- generated documents (intake-summary.md, ...)
    logs/llm_calls.jsonl    <- every LLM call
"""
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .errors import SessionError

STATE_VERSION = 1


def atomic_write_text(path, text):
    """Write `text` to `path` so that a crash leaves either the complete old file or the complete new one: write a temp
    file in the same directory, flush + fsync it, then os.replace. A leftover `*.tmp` after a crash is harmless."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:                                            # make the rename itself durable (best effort, POSIX only)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Session:
    def __init__(self, root, data):
        self.root = Path(root)
        self.data = data

    @property
    def state_path(self):
        return self.root / "state.json"

    @property
    def output_dir(self):
        return self.root / "output"

    @property
    def log_path(self):
        return self.root / "logs" / "llm_calls.jsonl"

    @property
    def session_id(self):
        return self.data["session_id"]

    @property
    def current_stage(self):
        return self.data["current_stage"]

    def complete_stage(self, stage, next_stage):
        done = self.data.setdefault("completed_stages", [])
        if stage not in done:
            done.append(stage)
        self.data["current_stage"] = next_stage

    def save(self):
        self.data["updated_at"] = now_iso()
        atomic_write_text(self.state_path, json.dumps(self.data, indent=2, ensure_ascii=False))

    def commit_data(self, new_data):
        """Make `new_data` the session state in ONE atomic step: it is written to state.json first and only adopted in
        memory if that succeeded. If the write fails, state.json and `self.data` both stay exactly as they were (so a
        later save() can never persist a half-applied change). `self.data` keeps its identity (updated in place)."""
        new_data["updated_at"] = now_iso()
        atomic_write_text(self.state_path, json.dumps(new_data, indent=2, ensure_ascii=False))
        self.data.clear()
        self.data.update(new_data)

    def write_output(self, filename, content):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / filename
        atomic_write_text(path, content)
        files = self.data.setdefault("output_files", [])
        if filename not in files:
            files.append(filename)
        return path


def new_session(sessions_dir, config_summary=None):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id = f"{stamp}-{secrets.token_hex(2)}"
    root = Path(sessions_dir) / session_id
    data = {
        "version": STATE_VERSION,
        "session_id": session_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "current_stage": "intake",      # the stage that runs next
        "completed_stages": [],
        "llm": config_summary or {},    # provider/model (never the API key)
        "description": None,            # raw user description (Stage 1 input)
        "intake": None,                 # structured Stage 1 output
        "output_files": [],
        "last_error": None,
    }
    session = Session(root, data)
    session.save()
    return session


def load_session(sessions_dir, session_id):
    root = Path(sessions_dir) / session_id
    path = root / "state.json"
    if not path.is_file():
        raise SessionError(f"Session '{session_id}' not found in {sessions_dir}.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SessionError(f"Cannot read {path}: {e}")
    if not isinstance(data, dict) or "current_stage" not in data:
        raise SessionError(f"{path} is not a valid session state file.")
    return Session(root, data)


def list_sessions(sessions_dir):
    base = Path(sessions_dir)
    if not base.is_dir():
        return []
    result = []
    for d in sorted(base.iterdir()):
        if (d / "state.json").is_file():
            try:
                result.append(json.loads((d / "state.json").read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return result


def latest_session_id(sessions_dir):
    sessions = list_sessions(sessions_dir)
    if not sessions:
        raise SessionError(f"No sessions found in {sessions_dir}.")
    return sessions[-1]["session_id"]
