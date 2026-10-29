"""The HTTP service: auth, a chat turn (model faked at the Chat Completions boundary), queued writes, officer decision."""

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agentkit as K

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
import rafeeq_api  # noqa: E402


def fake_chat(script):
    """Replace agentkit.chat: each call returns the next scripted assistant message."""
    it = iter(script)

    def chat(messages, **kw):
        text, calls = next(it)
        K.LEDGER.add({"prompt_tokens": 100, "completion_tokens": 20})  # what the real chat() records
        tool_calls = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
                      for i, (n, a) in enumerate(calls)]
        msg = SimpleNamespace(content=text, tool_calls=tool_calls or None,
                              model_dump=lambda exclude_none=True: {"role": "assistant", "content": text,
                                                                    **({"tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in tool_calls]} if tool_calls else {})})
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 20}))

    return chat


@pytest.fixture
def client(tmp_path):
    app = rafeeq_api.create_app(tmp_path / "api", tmp_path / "trace.jsonl")
    with TestClient(app) as c:
        yield c, tmp_path / "api"


H1 = {"X-API-Key": "dev-key-1"}
OFFICER = {"X-Officer-Key": "officer-key-1"}


def test_auth_is_required(client):
    c, _ = client
    assert c.get("/healthz").status_code == 200
    assert c.post("/v1/chat", json={"session_id": "s", "message": "hi"}).status_code == 401
    assert c.get("/v1/pending").status_code == 401
    assert c.get("/v1/pending", headers=H1).status_code == 401  # a client key is not an officer key


def test_write_is_queued_then_executed_only_after_officer_approval(client, monkeypatch):
    c, _ = client
    book = dict(permit_id="FTR-30311", slot="2026-11-05 08:00", national_id_last4="6654", mobile="0532236654")
    monkeypatch.setattr(K, "chat", fake_chat([("", [("book_inspection", book)]), ("An officer will confirm your booking.", [])]))
    r = c.post("/v1/chat", headers=H1, json={"session_id": "s1", "message": "Book 5 Nov 08:00 for FTR-30311, I confirm."}).json()
    assert r["pending_actions"][0]["tool"] == "book_inspection" and "officer" in r["reply"]
    pending = c.get("/v1/pending", headers=OFFICER).json()
    assert len(pending) == 1
    rows = lambda: sqlite3.connect(rafeeq_api.H.SHARED_DB).execute("SELECT count(*) FROM inspections WHERE permit_id = 'FTR-30311'").fetchone()[0]
    assert rows() == 0  # the model's call alone changed nothing
    d = c.post(f"/v1/pending/{pending[0]['id']}/decision", headers=OFFICER, json={"approve": True}).json()
    assert d["approved"] and d["result"]["status"] == "booked" and rows() == 1
    assert c.post(f"/v1/pending/{pending[0]['id']}/decision", headers=OFFICER, json={"approve": True}).status_code == 409


def test_rejected_action_never_runs(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(K, "chat", fake_chat([("", [("create_refund", dict(permit_id="SGN-40388", reason="duplicate_payment", national_id_last4="2277", mobile="0505502277"))]), ("Queued.", [])]))
    c.post("/v1/chat", headers=H1, json={"session_id": "s2", "message": "refund the duplicate"})
    pid = c.get("/v1/pending", headers=OFFICER).json()[0]["id"]
    assert c.post(f"/v1/pending/{pid}/decision", headers=OFFICER, json={"approve": False}).json()["approved"] is False
    assert sqlite3.connect(rafeeq_api.H.SHARED_DB).execute("SELECT count(*) FROM refunds").fetchone()[0] == 0


def test_read_turn_and_session_token_budget(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(K, "chat", fake_chat([("", [("compute_fee", dict(permit_type="EVT", event_days=3))]), ("It costs 3,000 SAR (POL-02).", [])]))
    r = c.post("/v1/chat", headers=H1, json={"session_id": "s3", "message": "3-day event fee?"}).json()
    assert "3,000" in r["reply"] and r["tools"] == ["compute_fee"] and r["tokens"]["total"] > 0
    monkeypatch.setattr(rafeeq_api, "SESSION_TOKENS", 1)
    assert c.post("/v1/chat", headers=H1, json={"session_id": "s3", "message": "again"}).status_code == 429
