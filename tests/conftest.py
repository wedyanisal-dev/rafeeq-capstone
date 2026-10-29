import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agentkit as K  # noqa: E402
import harness as H  # noqa: E402
import madar  # noqa: E402


class ScriptedShape:
    """A stand-in for the model: it emits the tool calls we script, then a final answer. No API key, no network.
    It lets the tests drive the REAL agent loop and hooks with a 'compromised' model."""

    name = "scripted"

    def __init__(self, calls: list[tuple[str, dict]], final: str = "done"):
        self.calls, self.final, self.i = calls, final, 0

    def start(self, system, task, history=None):
        return [{"role": "system", "content": system}, {"role": "user", "content": task}]

    def step(self, state, tools, model, use_cache=None, **kw):
        if self.i < len(self.calls):
            name, args = self.calls[self.i]
            self.i += 1
            return K.Turn("", [K.Call(f"c{self.i}", name, json.dumps(args))])
        return K.Turn(self.final, [])

    def add_results(self, state, results):
        state.extend({"role": "tool", "tool_call_id": c.id, "content": out} for c, out in results)


@pytest.fixture
def ctx(tmp_path):
    m = madar.Madar(tmp_path / "db")
    return H.Ctx(m=m, memory=H.Memory(tmp_path / "mem"), audit=tmp_path / "audit.jsonl")


@pytest.fixture
def run_scripted(ctx):
    def go(calls, final="done", approve=lambda n, a: True, tools=None):
        tools = tools or H.local_tools(ctx)
        ctx.begin_turn()
        return K.run_agent("hi", tools, system="test", shape=ScriptedShape(calls, final), max_steps=8,
                           permission=H.permission, approve=approve, before_tool=H.before_tool(ctx),
                           after_tool=H.after_tool(ctx), verbose=False)

    return go
