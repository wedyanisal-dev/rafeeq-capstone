"""Rafeeq's harness: everything around the model that is enforced in code, not in the prompt.

    context   AGENTS.md + skills (progressive disclosure) + a small memory + a compaction rule
    control   permission policy (writes need a human), step / cost / token budgets, an audit log
    security  egress guard (who may receive mail, what may leave), output redaction, PII-free memory
    wiring    the MCP server for read tools and the one SQLite file the MCP server and the agent share

`rafeeq.py` (CLI, evals, red-team) and `servers/rafeeq_api.py` (HTTP service) both build on this module.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mcp import StdioServerParameters

import agentkit as K
import madar

ROOT = Path(__file__).resolve().parent
OUT = K.lab_dir("rafeeq")

# ---------------------------------------------------------------- budgets (requirement 3)
MAX_STEPS = int(os.getenv("RAFEEQ_MAX_STEPS", "8"))  # model calls per user turn
MAX_COST = float(os.getenv("RAFEEQ_MAX_COST", "0.05"))  # USD per turn (free models cost 0: the token budget is the real brake)
MAX_RUN_TOKENS = int(os.getenv("RAFEEQ_MAX_RUN_TOKENS", "60000"))  # input + output tokens per user turn
MAX_RESULT_CHARS = 3000  # one tool result, before it enters the context
MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_TOKENS = 6000

WRITE_TOOLS = set(madar.Madar.WRITE_TOOLS) | {"memory_save"}

# ---------------------------------------------------------------- context: AGENTS.md, skills, memory
AGENTS_MD = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
SKILLS_DIR = K.DATA / "skills"


def _read_skill(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    head, body = raw[3:].split("---", 1)
    return dict(line.split(": ", 1) for line in head.strip().splitlines()), body.strip()


SKILLS = {_read_skill(p)[0]["name"]: p for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))}


def skill_index() -> str:
    return "\n".join(f"- {n}: {_read_skill(p)[0]['description']}" for n, p in SKILLS.items())


def load_skill(name: str) -> str:
    """Load the step-by-step procedure for one kind of task. Call it before booking, refunding, escalating or appealing.

    Args:
        name: a skill name from the list in your instructions, e.g. "refund-request".
    """
    if name not in SKILLS:
        return json.dumps({"error": f"No skill {name!r}. Available: {sorted(SKILLS)}"})
    return _read_skill(SKILLS[name])[1]


ID_RE = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")  # a full Saudi national / iqama number
MOBILE_RE = re.compile(r"(?<!\d)(?:\+966|00966|0)?5\d{8}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LINK_RE = re.compile(r"https?://([^/\s)>\]\"']+)")
TRUSTED_DOMAIN = "madar.example"


class Memory:
    """Durable preferences, one small file per topic. Data minimisation is enforced here, not requested in the prompt."""

    MAX_FILE, MAX_TOTAL = 400, 1500  # characters

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _file(self, topic: str) -> Path:
        slug = re.sub(r"[^a-z0-9_-]+", "-", topic.lower()).strip("-")[:40]
        if not slug:
            raise madar.ToolError("Give the memory a short topic such as 'language' or 'contact-time'.")
        return self.root / f"{slug}.md"  # a slug can't contain a slash: no path traversal

    def view(self) -> str:
        files = sorted(self.root.glob("*.md"))
        return "\n".join(f"- {f.stem}: {f.read_text(encoding='utf-8').strip()}" for f in files) or "(empty)"

    def save(self, topic: str, note: str) -> str:
        if ID_RE.search(note) or MOBILE_RE.search(note) or EMAIL_RE.search(note) or LINK_RE.search(note):
            raise madar.ToolError("Memory holds preferences only: no IDs, phone numbers, e-mail addresses or links (data minimisation).")
        if len(note) > self.MAX_FILE:
            raise madar.ToolError(f"Too long ({len(note)} > {self.MAX_FILE} characters). Keep only the preference.")
        path = self._file(topic)
        others = sum(len(p.read_text(encoding="utf-8")) for p in self.root.glob("*.md") if p != path)
        if others + len(note) > self.MAX_TOTAL:
            raise madar.ToolError("Memory is full. Ask the person which preference to forget.")
        path.write_text(note.strip(), encoding="utf-8")
        return f"Saved {path.stem}"


def build_spec(level: str, memory: Memory | None = None) -> str:
    """level "base": the course's system prompt only. level "context": plus AGENTS.md, the skill index and memory."""
    spec = madar.SYSTEM_PROMPT
    if level == "context":
        spec += "\n\n" + AGENTS_MD + "\n\n# Skills\nBefore a multi-step task, call load_skill with the matching name:\n" + skill_index()
        if memory is not None and memory.view() != "(empty)":
            spec += "\n\n# What you remember about this person (preferences only)\n" + memory.view()
    return spec


# ---------------------------------------------------------------- session state shared by the hooks
@dataclass
class Ctx:
    m: madar.Madar
    memory: Memory
    allowed_recipients: set[str] = field(default_factory=set)  # e-mails of applicants verified in THIS conversation
    start_tokens: int = 0
    audit: Path = OUT / "audit.jsonl"
    guards: bool = True  # the red-team run switches the code-level guards off to show what they stop

    def begin_turn(self) -> None:
        self.start_tokens = K.LEDGER.input_tokens + K.LEDGER.output_tokens

    def tokens_used(self) -> int:
        return K.LEDGER.input_tokens + K.LEDGER.output_tokens - self.start_tokens


def log(ctx: Ctx, event: str, **detail) -> None:
    with ctx.audit.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 1), "event": event, **detail}, ensure_ascii=False, default=str) + "\n")


def local_tools(ctx: Ctx, skills: bool = True) -> list:
    """Tools that live in this process: the write tools (they need approval), memory and, optionally, skills.
    The read tools (search_policies, get_permit, compute_fee, list_slots) come over MCP."""

    def memory_view() -> str:
        """Show the preferences you saved for this person (language, contact time, district)."""
        return ctx.memory.view()

    def memory_save(topic: str, note: str) -> str:
        """Save one durable preference for next time. Never save IDs, phone numbers, e-mail addresses or permit details.

        Args:
            topic: a short label such as "language" or "contact-time".
            note: the preference, one short sentence.
        """
        return ctx.memory.save(topic, note)

    m = ctx.m
    tools = [m.book_inspection, m.reschedule_inspection, m.create_refund, m.escalate, m.send_email, memory_view, memory_save]
    return tools + [load_skill] if skills else tools


# ---------------------------------------------------------------- harness hooks
def permission(name: str, args: dict) -> str:
    """Reads run; every write waits for a human (requirement 3)."""
    return "ask" if name in WRITE_TOOLS else "allow"


def approve_cli(name: str, args: dict) -> bool:
    return input(f"\n🛡️  Allow {name}({json.dumps(args, ensure_ascii=False)})? [y/N] ").strip().lower() == "y"


def logged_approver(ctx: Ctx, approve):
    """Wrap any approver so that every decision is in the audit log."""

    def wrapper(name: str, args: dict) -> bool:
        ok = bool(approve(name, args))
        log(ctx, "approval", tool=name, args=args, approved=ok)
        return ok

    return wrapper


def egress_error(reasons: list[str]) -> str:
    return json.dumps({"error": "Blocked by the egress policy: " + "; ".join(reasons) + ". Tell the person what you "
                                "would have sent and hand over to an officer."}, ensure_ascii=False)


def egress_check(ctx: Ctx, args: dict) -> list[str]:
    """Why a send_email call must not go out. Pure function of the arguments and the conversation's verified set."""
    body = f"{args.get('subject', '')}\n{args.get('body', '')}"
    reasons = []
    if str(args.get("to", "")).strip().lower() not in ctx.allowed_recipients:
        reasons.append("mail may only go to the registered address of an applicant verified in this conversation (POL-10)")
    if ID_RE.search(body) or MOBILE_RE.search(body):
        reasons.append("the message contains a full national ID or mobile number")
    if any(not host.lower().endswith(TRUSTED_DOMAIN) for host in LINK_RE.findall(body)):
        reasons.append(f"links may only point to {TRUSTED_DOMAIN}")
    return reasons


def before_tool(ctx: Ctx):
    def hook(name: str, args: dict) -> str | None:
        if ctx.tokens_used() > MAX_RUN_TOKENS:  # token budget: a free model has no dollar cost to stop on
            log(ctx, "budget", kind="tokens", used=ctx.tokens_used())
            return json.dumps({"error": f"Token budget of {MAX_RUN_TOKENS} reached. Answer with what you have."})
        if ctx.guards and name == "send_email":
            reasons = egress_check(ctx, args)
            if reasons:
                log(ctx, "blocked", tool=name, args=args, reasons=reasons)
                return egress_error(reasons)
        return None

    return hook


def after_tool(ctx: Ctx):
    def hook(name: str, args: dict, result: str) -> str | None:
        """Audit every call, remember who was verified, and keep the result inside the context budget."""
        log(ctx, "tool", tool=name, args=args, chars=len(result), error=result.startswith('{"error"'))
        if name == "get_permit" and not result.startswith('{"error"'):
            rows = ctx.m.sql("SELECT a.email FROM permits p JOIN applicants a USING (applicant_id) WHERE p.permit_id = ?",
                             (str(args.get("permit_id", "")).strip().upper(),))
            if rows:
                ctx.allowed_recipients.add(rows[0]["email"].lower())
        if len(result) > MAX_RESULT_CHARS:
            return result[:MAX_RESULT_CHARS] + f"\n[… {len(result) - MAX_RESULT_CHARS} characters cut; ask for less]"
        return None

    return hook


def redact_reply(text: str) -> str:
    """Last line of defence on what the person reads: no outside links or images, no full IDs or mobile numbers."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[image removed]", text)
    text = re.sub(r"https?://([^/\s)>\]\"']+)[^\s)>\]\"']*",
                  lambda mt: mt.group(0) if mt.group(1).lower().endswith(TRUSTED_DOMAIN) else "[link removed]", text)
    text = ID_RE.sub(lambda mt: "******" + mt.group(0)[-4:], text)
    return MOBILE_RE.sub(lambda mt: "*****" + mt.group(0)[-4:], text)


def spotlight(text: str, source: str = "document") -> str:
    """Mark pasted third-party text as data. The sender can't close our tag. (A prompt-level layer: weak alone.)"""
    body = text.replace("<", "‹").replace(">", "›")
    return f"<untrusted_{source}>\n{body}\n</untrusted_{source}>"


def compact(history: list) -> list:
    """Context rule, two steps, no extra model call.
    1. Clear the content of tool results older than the last 6 messages (they were used; the answer kept what mattered).
    2. If the history is still over the budget, keep only the last MAX_HISTORY_MESSAGES, starting on a user message
       (a tool result without its call is rejected by the API)."""
    out = []
    for i, msg in enumerate(history):
        if msg.get("role") == "tool" and i < len(history) - 6 and len(str(msg.get("content", ""))) > 80:
            msg = {**msg, "content": "[old tool result cleared to save context]"}
        out.append(msg)
    if len(out) > MAX_HISTORY_MESSAGES or K.count_tokens(out) > MAX_HISTORY_TOKENS:
        tail = out[-MAX_HISTORY_MESSAGES:]
        while tail and tail[0].get("role") != "user":
            tail = tail[1:]
        out = tail
    return out


# ---------------------------------------------------------------- wiring: MCP server + one shared database
SHARED_DB = K.lab_dir("rafeeq_shared") / "madar.sqlite"


def mcp_server() -> StdioServerParameters:
    """Read tools are served by servers/madar_mcp.py over stdio (requirement 1: one tool surface exposed via MCP)."""
    shutil.copy(madar.DB_TEMPLATE if madar.DB_TEMPLATE.exists() else madar.Madar(OUT / "tmp").db_path, SHARED_DB)
    return StdioServerParameters(command="uv", args=["run", "--quiet", str(ROOT / "servers" / "madar_mcp.py")],
                                 env={**os.environ, "MADAR_DB": str(SHARED_DB)})


@contextmanager
def shared_db(m: madar.Madar):
    """The MCP server (reads) and this process (writes) must see ONE database: point `m` at the shared file for the run
    and copy the end state back, so graders and callers still read `m`."""
    original = m.db_path
    shutil.copy(original, SHARED_DB)
    m.db_path = SHARED_DB
    try:
        yield m
    finally:
        m.db_path = original
        shutil.copy(SHARED_DB, original)
