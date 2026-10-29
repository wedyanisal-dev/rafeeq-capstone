# Live demo script (2 minutes) and fallback

Goal: one multi-step task that shows MCP reads, a skill, an **approval on a write**, and then the same assistant
surviving a hostile e-mail. Start from a clean state: `rm -rf artifacts/rafeeq/db artifacts/rafeeq/session-demo.json`.

```bash
uv run rafeeq.py chat --session demo
```

1. **Policy + fee (MCP read, number from a tool)**
   `What does an illuminated 5 m² sign cost, and where does that come from?`
   → `compute_fee` → "1,250 SAR (POL-02)".
2. **Multi-step task with verification and approval**
   `My food truck permit FTR-30311 was approved. I want the health inspection on Thursday 5 November in the morning. ID ends 6654, mobile 0532236654.`
   → `load_skill(inspection-booking)` → `get_permit` (verification) → `list_slots` (MCP) → the assistant proposes a slot and **asks you to confirm**.
   Reply `Yes, book 08:00.` → the terminal shows **🛡️ Allow book_inspection(...)? [y/N]** → press `y` (say it out loud: this is the human gate).
   → "Booked INS-9002".
3. **The write was audited and is visible to the next read**
   `Which slots are still free that day?` (08:00 is gone: the MCP server and the local tool share one database), then show
   `artifacts/rafeeq/audit.jsonl` (tool calls and the approval).
4. **Attack (≈30 s)** paste the contractor e-mail from `attacks.jsonl` (`exfil-email-audit`) with `Help me reply to this e-mail:`.
   Show that no mail leaves: `sqlite3 artifacts/rafeeq/db/madar.sqlite "select count(*) from outbox"` → `0`, and the audit log line
   `"event": "blocked"` if the model tried. Backup proof that does not depend on the model: `uv run pytest -q tests/test_agent_e2e.py::test_compromised_model_cannot_exfiltrate`.

**Fallback (free model rate-limited):** play the screen recording (`docs/demo.mp4`, record it on Day 5 morning), or run the
scripted tests above, which need no key; the response cache (`cache/`) replays any request already made once.

**API variant (if asked):** `RAFEEQ_API_KEYS=dev-key-1 RAFEEQ_OFFICER_KEYS=officer-key-1 uv run servers/rafeeq_api.py`, then
`POST /v1/chat` (the booking is *queued*), `GET /v1/pending` and `POST /v1/pending/1/decision {"approve": true}` with `X-Officer-Key`.
