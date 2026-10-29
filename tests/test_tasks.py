"""The eval suite itself is code: check that it is well formed and that its db graders can pass AND fail."""

import json
from pathlib import Path

import pytest

import evals
import madar

ROOT = Path(__file__).resolve().parents[1]
TASKS = evals.load_tasks(ROOT / "tasks.jsonl")
VALID = {"contains_any", "not_contains", "tool_called", "tool_not_called", "db", "judge"}


def test_at_least_15_unique_tasks_with_graders():
    assert len(TASKS) >= 15
    assert len({t["id"] for t in TASKS}) == len(TASKS)
    for t in TASKS:
        assert t["lang"] in ("en", "ar") and t["prompt"].strip() and t["graders"]
        assert {g["type"] for g in t["graders"]} <= VALID, t["id"]
    assert len({t["category"] for t in TASKS}) >= 5


def test_every_grader_type_that_needs_no_model_is_automatic():
    automatic = sum(1 for t in TASKS if any(g["type"] != "judge" for g in t["graders"]))
    assert automatic == len(TASKS), "every task needs at least one non-LLM grader"


# the correct action for each write task, so we know the db grader passes after it and fails before it
GOLDEN = {
    "book-ftr-inspection": ("book_inspection", dict(permit_id="FTR-30311", slot="2026-11-05 08:00", national_id_last4="6654", mobile="0532236654")),
    "refund-duplicate-payment": ("create_refund", dict(permit_id="SGN-40388", reason="duplicate_payment", national_id_last4="2277", mobile="0505502277")),
    "escalate-overdue-bld": ("escalate", dict(permit_id="BLD-20417", note="SLA breached", national_id_last4="7730", mobile="0555017730")),
}


@pytest.mark.parametrize("task", [t for t in TASKS if t["id"] in GOLDEN], ids=lambda t: t["id"])
def test_db_grader_fails_before_and_passes_after_the_right_action(task, tmp_path):
    m = madar.Madar(tmp_path / "db")
    db_graders = [g for g in task["graders"] if g["type"] == "db"]
    assert db_graders
    before = evals.grade({**task, "graders": db_graders}, "", [], m)
    assert not all(g["pass"] for g in before)
    name, args = GOLDEN[task["id"]]
    getattr(m, name)(**args)
    after = evals.grade({**task, "graders": db_graders}, "", [(name, args)], m)
    assert all(g["pass"] for g in after), after


@pytest.mark.parametrize("tid", ["escalate-not-overdue", "no-confirmation-no-write", "injection-contractor-email"])
def test_negative_db_graders_pass_on_an_untouched_database(tid, tmp_path):
    task = next(t for t in TASKS if t["id"] == tid)
    m = madar.Madar(tmp_path / "db")
    graders = [g for g in task["graders"] if g["type"] == "db"]
    assert all(g["pass"] for g in evals.grade({**task, "graders": graders}, "", [], m))


def test_attacks_file_is_well_formed():
    attacks = [json.loads(l) for l in (ROOT / "attacks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(attacks) >= 3 and all({"id", "prompt", "note"} <= a.keys() for a in attacks)
