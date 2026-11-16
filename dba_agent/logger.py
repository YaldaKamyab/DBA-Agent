"""LLM call logging: one JSON object per line (JSONL)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .llm import redact


class LLMCallLogger:
    def __init__(self, log_path):
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, *, stage, attempt, provider, model, system, input, output,
            error=None, duration_s=None):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": stage,
            "attempt": attempt,
            "provider": provider,
            "model": model,
            "system": None if system is None else redact(system),
            "input": None if input is None else redact(input),
            "output": None if output is None else redact(output),
            "error": None if error is None else redact(error),
            "duration_s": None if duration_s is None else round(duration_s, 3),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read(self):
        if not self.path.is_file():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return entries
