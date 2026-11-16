"""Drives the REAL stage code (workflow.run_stage) for Stages 1-4 with scripted, deterministic LLM answers."""
import copy
import json
from pathlib import Path

from dba_agent import workflow
from dba_agent.prompts import PromptLoader
from dba_agent.state import new_session
from dba_agent.ui import UI
from tests import scenarios_6b as sc

PROMPTS = str(Path(__file__).resolve().parent.parent / "prompts")
ENOUGH = {"enough_information": True, "reasoning": "All categories are covered.", "follow_up_questions": []}


class ScriptedLLM:
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []                       # (stage, user prompt)

    def complete_json(self, stage, system, user, validator=None):
        self.calls.append((stage, user))
        item = copy.deepcopy(self.script[stage].pop(0))
        if isinstance(item, Exception):
            raise item
        return validator(item) if validator else item

    def users(self, stage):
        return [u for s, u in self.calls if s == stage]


def make_script(name, dbms=None, logical=None, conceptual=None, reviews=None):
    spec = sc.SCENARIOS[name]
    s12 = spec["stage12"]()
    return {"intake": [s12["intake"]], "interview:questions": [s12["questions"]],
            "interview:review": reviews or s12.get("reviews") or [s12["follow"], ENOUGH],
            "interview:requirements": [s12["requirements"]],
            "dbms:selection": [sc.choice(dbms or spec["dbms"])],
            "design:conceptual": [conceptual or spec["conceptual"]()], "design:logical": [logical or spec["logical"]()]}, s12


def scripted_ui(answers):
    it = iter(answers)

    def input_fn():
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return UI(input_fn, lambda *_: None)


def run_pipeline(tmp, name, until="design", dbms=None, answers=None, **kw):
    """-> (session, llm, s12). Runs intake, interview, dbms and design through workflow.run_stage."""
    script, s12 = make_script(name, dbms=dbms, **kw)
    llm = ScriptedLLM(script)
    prompts = PromptLoader(PROMPTS)
    s = new_session(tmp, {"provider": "fake", "model": "m"})
    s.data["description"] = sc.SCENARIOS[name]["description"]
    s.save()
    ui = scripted_ui(answers if answers is not None else s12["answers_r1"] + s12["answers_r2"] + ["security: admins only"])
    for stage in ("intake", "interview", "dbms", "design"):
        workflow.run_stage(stage, s, llm, prompts, ui)
        if stage == until:
            break
    return s, llm, s12
