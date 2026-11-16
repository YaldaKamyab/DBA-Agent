"""Prompt loading from the external prompts/ directory ({{variable}} templates)."""
import re
from pathlib import Path

from .errors import PromptError

_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class PromptLoader:
    def __init__(self, prompts_dir):
        self.dir = Path(prompts_dir)

    def load(self, name):
        path = self.dir / f"{name}.md"
        if not path.is_file():
            raise PromptError(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8")

    def render(self, name, **variables):
        template = self.load(name)

        def sub(match):
            key = match.group(1)
            if key not in variables:
                raise PromptError(f"Prompt '{name}' needs variable '{key}'.")
            return str(variables[key])

        # single pass: user text containing {{...}} is never re-expanded
        return _VAR.sub(sub, template)
