# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi>=0.150", "uvicorn>=0.40", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1",
#                 "opentelemetry-sdk>=1.45", "mcp>=2.3", "pandas>=2.2"]
# ///
"""Rafeeq as an HTTP service (capstone track 1: "... and an API").

    RAFEEQ_API_KEYS=dev-key-1 RAFEEQ_OFFICER_KEYS=officer-key-1 uv run servers/rafeeq_api.py --port 8793

    curl -s localhost:8793/v1/chat -H "X-API-Key: dev-key-1" -H "Content-Type: application/json" \
         -d '{"session_id": "s1", "message": "What are your office hours?"}'

What it adds around the agent loop (the policy lives in harness.py, shared with the CLI):
- an API key per client (header X-API-Key; keys from RAFEEQ_API_KEYS, comma-separated);
- sessions: the conversation is stored in SQLite and continued on the next request with the same session_id;
- budgets: at most RAFEEQ_SESSION_TOKENS tokens per session (HTTP 429 after that) and RAFEEQ_MAX_STEPS steps per turn;
- fallbacks: OpenRouter's `models` list (agentkit.FALLBACKS), then one retry on a second model if the call fails;
- least privilege: read tools run; write tools are never executed by the model's call, they are queued for an officer
  (table pending_actions); an officer approves or rejects with POST /v1/pending/{id}/decision (X-Officer-Key), and only
  then the action runs, with the officer recorded in the audit log;
- the harness guards: egress check on e-mail, reply redaction, token / step budgets, compaction of long sessions;
- traces: OpenTelemetry spans with GenAI attributes (invoke_agent → chat / execute_tool), written as JSON lines to
  artifacts/traces/rafeeq_api.jsonl (swap in an OTLP exporter to send them to Jaeger, Phoenix, Langfuse, ...).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import agentkit as K  # noqa: E402
import harness as H  # noqa: E402
import madar  # noqa: E402
from rafeeq import CONFIGS  # noqa: E402

API_KEYS = {k.strip() for k in os.getenv("RAFEEQ_API_KEYS", "dev-key-1").split(",") if k.strip()}
SESSION_TOKENS = int(os.getenv("RAFEEQ_SESSION_TOKENS", "40000"))
OFFICER_KEYS = {k.strip() for k in os.getenv("RAFEEQ_OFFICER_KEYS", "officer-key-1").split(",") if k.strip()}
SECOND_MODEL = os.getenv("RAFEEQ_SECOND_MODEL", K.FALLBACKS[0] if K.FALLBACKS else K.MODEL)


# ---------------------------------------------------------------- traces
class JsonlExporter(SpanExporter):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans) -> SpanExportResult:
        with self.path.open("a", encoding="utf-8") as f:
            for s in spans:
                f.write(json.dumps({"name": s.name, "trace_id": f"{s.context.trace_id:032x}",
                                    "span_id": f"{s.context.span_id:016x}",
                                    "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                                    "ms": round((s.end_time - s.start_time) / 1e6, 1),
                                    "attributes": dict(s.attributes or {})}, ensure_ascii=False) + "\n")
        return SpanExportResult.SUCCESS


def tracer_for(path: Path):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(path)))
    return provider.get_tracer("rafeeq")


class TracedShape(K.ChatShape):
    """Chat Completions with one `chat {model}` span per model call."""

    def __init__(self, tracer, **kw):
        super().__init__(**kw)
        self.tracer = tracer

    def step(self, state, tools, model, use_cache=None, **kw):
        with self.tracer.start_as_current_span(f"chat {model}") as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.request.model", model)
            turn = super().step(state, tools, model, use_cache=use_cache, **kw)
            u = turn.usage.model_dump() if hasattr(turn.usage, "model_dump") else (turn.usage or {})
            span.set_attribute("gen_ai.usage.input_tokens", u.get("prompt_tokens") or 0)
            span.set_attribute("gen_ai.usage.output_tokens", u.get("completion_tokens") or 0)
            span.set_attribute("gen_ai.response.finish_reasons", ["tool_calls" if turn.calls else "stop"])
            return turn


def traced(tracer, fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with tracer.start_as_current_span(f"execute_tool {fn.__name__}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", fn.__name__)
            return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- the service
class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w.-]+$")
    message: str = Field(min_length=1, max_length=4000)


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tools: list[str]
    pending_actions: list[dict]
    tokens: dict
    model_steps: int


class Decision(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=300)


def create_app(workdir: Path | None = None, trace_file: Path | None = None) -> FastAPI:
    workdir = workdir or K.lab_dir("rafeeq_api")
    m = madar.Madar(workdir, actor="rafeeq-api")
    m.db_path = H.SHARED_DB  # the MCP server (reads) and this process (writes) share one file
    shutil.copy(madar.DB_TEMPLATE, H.SHARED_DB)
    db = workdir / "sessions.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, history TEXT, tokens INTEGER, updated REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS pending_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
                    "tool TEXT, args TEXT, created REAL, status TEXT DEFAULT 'pending', decided_by TEXT, result TEXT)")
    tracer = tracer_for(trace_file or K.ARTIFACTS / "traces" / "rafeeq_api.jsonl")
    bridge: dict = {}  # the MCP client lives as long as the app

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        bridge["mcp"] = K.MCPBridge(H.mcp_server()).open()
        try:
            yield
        finally:
            bridge["mcp"].close()

    lock = threading.Lock()  # one agent run at a time: K.LEDGER is process-wide (fine for a lab, not for scale)
    app = FastAPI(title="Rafeeq API", version="2.0.0", lifespan=lifespan)
    config = CONFIGS["B"]

    def api_key(x_api_key: str = Header(default="")) -> str:
        if x_api_key not in API_KEYS:
            raise HTTPException(401, "Missing or invalid X-API-Key")
        return x_api_key

    def officer_key(x_officer_key: str = Header(default="")) -> str:
        if x_officer_key not in OFFICER_KEYS:
            raise HTTPException(401, "Missing or invalid X-Officer-Key")
        return x_officer_key

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "model": K.MODEL}

    @app.post("/v1/chat", response_model=ChatOut)
    def chat(req: ChatIn, key: str = Depends(api_key)) -> ChatOut:
        sid = f"{key}:{req.session_id}"
        with sqlite3.connect(db) as con:
            row = con.execute("SELECT history, tokens FROM sessions WHERE id = ?", (sid,)).fetchone()
        history, spent = (json.loads(row[0]), row[1]) if row else ([], 0)
        if spent >= SESSION_TOKENS:
            raise HTTPException(429, f"Session budget of {SESSION_TOKENS} tokens used up. Start a new session.")
        pending: list[dict] = []
        ctx = H.Ctx(m=m, memory=H.Memory(workdir / "memory" / re.sub(r"\W+", "_", sid)), audit=workdir / "audit.jsonl")

        def permission(name: str, args: dict) -> str:
            if name in H.WRITE_TOOLS:  # never executed on the model's say-so: queued for an officer
                pending.append({"tool": name, "args": args})
                return "deny"
            return "allow"

        with lock, tracer.start_as_current_span("invoke_agent Rafeeq") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "Rafeeq")
            span.set_attribute("gen_ai.conversation.id", req.session_id)
            ctx.begin_turn()
            tools = [traced(tracer, t) for t in H.local_tools(ctx) + bridge["mcp"].agent_tools()]
            kw = dict(system=H.build_spec(config["level"], ctx.memory), shape=TracedShape(tracer),
                      history=H.compact(history), max_steps=H.MAX_STEPS, max_cost=H.MAX_COST, permission=permission,
                      before_tool=H.before_tool(ctx), after_tool=H.after_tool(ctx), verbose=False)
            try:
                run = K.run_agent(req.message, tools, **kw)
            except Exception as e:  # noqa: BLE001 - every model in the routing list failed: try a second model once
                span.set_attribute("error.type", type(e).__name__)
                run = K.run_agent(req.message, tools, model=SECOND_MODEL, **kw)
            used = ctx.tokens_used()
        reply = H.redact_reply(run.final) or "I couldn't finish this request. An officer will follow up."
        if pending:
            reply += "\n\n(An officer will review and confirm: " + ", ".join(p["tool"] for p in pending) + ".)"
        with sqlite3.connect(db) as con:
            con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                        (sid, json.dumps(run.state[1:], ensure_ascii=False, default=str), spent + used, time.time()))
            con.executemany("INSERT INTO pending_actions (session_id, tool, args, created) VALUES (?,?,?,?)",
                            [(req.session_id, p["tool"], json.dumps(p["args"], ensure_ascii=False), time.time()) for p in pending])
        return ChatOut(session_id=req.session_id, reply=reply, tools=[n for n, _ in run.tool_calls],
                       pending_actions=pending, tokens={"total": used}, model_steps=run.steps)

    @app.get("/v1/pending")
    def list_pending(_: str = Depends(officer_key)) -> list[dict]:
        with sqlite3.connect(db) as con:
            rows = con.execute("SELECT id, session_id, tool, args, status FROM pending_actions WHERE status = 'pending'").fetchall()
        return [{"id": r[0], "session_id": r[1], "tool": r[2], "args": json.loads(r[3]), "status": r[4]} for r in rows]

    @app.post("/v1/pending/{action_id}/decision")
    def decide(action_id: int, d: Decision, officer: str = Depends(officer_key)) -> dict:
        with sqlite3.connect(db) as con:
            row = con.execute("SELECT tool, args, status FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
            if not row:
                raise HTTPException(404, "No such action")
            if row[2] != "pending":
                raise HTTPException(409, f"Already {row[2]}")
            tool, args = row[0], json.loads(row[1])
            result = "rejected by officer"
            if d.approve:
                with lock:
                    result = _run_write(m, tool, args)
            con.execute("UPDATE pending_actions SET status = ?, decided_by = ?, result = ? WHERE id = ?",
                        ("approved" if d.approve else "rejected", officer[:6] + "…", result[:500], action_id))
        m._audit("officer_decision", {"action_id": action_id, "tool": tool, "approved": d.approve, "note": d.note})
        return {"id": action_id, "tool": tool, "approved": d.approve, "result": json.loads(result) if result.startswith("{") else result}

    return app


def _run_write(m: madar.Madar, tool: str, args: dict) -> str:
    if tool not in madar.Madar.WRITE_TOOLS:
        return json.dumps({"error": f"{tool} is not an executable write tool"})
    return K.call_tool({tool: getattr(m, tool)}, tool, args)


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8793)
    args = ap.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
