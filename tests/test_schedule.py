# TRAP-27 复测排程：容量、优先级、状态机纯逻辑
from datetime import date

import pytest

from app.modules.m7_retest.schedule import (
    available_capacity,
    can_schedule,
    collect_reasons,
    ensure_transition,
    is_contamination,
    occupied,
    parse_day,
    pick_next,
    priority_key,
    retry_task,
    sort_tasks,
    urgency,
)

TODAY = "2026-11-18"


def t(task_id, due, reasons=None, status="WAITING", scheduled=None, created=None, attempt=1):
    return {
        "task_id": task_id, "sample_id": "S" + task_id, "batch": "B", "analyte": "Pb",
        "kind": "sample", "reasons": reasons or [], "due_date": due, "status": status,
        "scheduled_date": scheduled, "created_at": created or ("2026-11-18T0" + task_id[-1] + ":00:00+00:00"),
        "attempt": attempt,
    }


def test_parse_day_accepts_string_and_date():
    assert parse_day("2026-11-18") == date(2026, 11, 18)
    assert parse_day(date(2026, 11, 18)) == date(2026, 11, 18)


def test_parse_day_rejects_bad_value():
    with pytest.raises(ValueError):
        parse_day("18/11/2026")


def test_urgency_buckets():
    assert urgency("2026-11-18", TODAY) == "OVERDUE_TODAY"
    assert urgency("2026-11-17", TODAY) == "OVERDUE_TODAY"
    assert urgency("2026-11-19", TODAY) == "DUE_SOON"
    assert urgency("2026-11-25", TODAY) == "NORMAL"


def test_contamination_detects_blank_reasons():
    assert is_contamination(["BLANK_FAIL"]) is True
    assert is_contamination(["PAIR_FAIL", "BLANK_MISSING"]) is True
    assert is_contamination(["PAIR_FAIL"]) is False


def test_contamination_outranks_due_today():
    """只剩一个名额：污染批优先于当天到期的普通报告。"""
    due_sample = t("1", TODAY, ["PAIR_FAIL"])
    blank = t("2", "2026-11-25", ["BLANK_FAIL"])
    ordered = sort_tasks([due_sample, blank], TODAY)
    assert [x["task_id"] for x in ordered] == ["2", "1"]
    assert priority_key(blank, TODAY) < priority_key(due_sample, TODAY)


def test_due_today_outranks_due_soon_within_same_class():
    a = t("1", TODAY, ["PAIR_FAIL"])
    b = t("2", "2026-11-19", ["PAIR_FAIL"])
    assert [x["task_id"] for x in sort_tasks([b, a], TODAY)] == ["1", "2"]


def test_created_at_breaks_tie():
    a = t("1", TODAY, ["PAIR_FAIL"], created="2026-11-18T01:00:00+00:00")
    b = t("2", TODAY, ["PAIR_FAIL"], created="2026-11-18T00:00:00+00:00")
    assert [x["task_id"] for x in sort_tasks([a, b], TODAY)] == ["2", "1"]


def test_occupied_counts_scheduled_and_running_only():
    tasks = [
        t("1", TODAY, status="SCHEDULED", scheduled=TODAY),
        t("2", TODAY, status="RUNNING", scheduled=TODAY),
        t("3", TODAY, status="WAITING"),
        t("4", TODAY, status="DONE", scheduled=TODAY),
    ]
    assert occupied(tasks, TODAY) == 2


def test_occupied_isolated_by_day():
    tasks = [t("1", TODAY, status="SCHEDULED", scheduled="2026-11-19")]
    assert occupied(tasks, TODAY) == 0


def test_pick_next_respects_capacity():
    tasks = [t(str(i), TODAY, ["PAIR_FAIL"]) for i in range(1, 4)]
    chosen = pick_next(tasks, TODAY, 1)
    assert chosen == ["1"]
    assert pick_next(tasks, TODAY, 0) == []


def test_pick_next_skips_non_waiting_and_counts_existing_occupancy():
    tasks = [
        t("1", TODAY, status="RUNNING", scheduled=TODAY),
        t("2", TODAY, ["PAIR_FAIL"]),
        t("3", TODAY, ["PAIR_FAIL"]),
    ]
    # 容量 1 已被 RUNNING 占满，候补无法再排入
    assert pick_next(tasks, TODAY, 1) == []
    # 容量 2 时只剩一个名额
    assert pick_next(tasks, TODAY, 2) == ["2"]


def test_available_capacity_never_negative():
    tasks = [t("1", TODAY, status="RUNNING", scheduled=TODAY)]
    assert available_capacity(tasks, TODAY, 0) == 0


def test_can_schedule_requires_waiting_and_capacity():
    tasks = [t("1", TODAY, status="RUNNING", scheduled=TODAY)]
    waiting = t("2", TODAY)
    assert can_schedule(waiting, tasks, TODAY, 2) is True
    assert can_schedule(waiting, tasks, TODAY, 1) is False
    assert can_schedule(t("3", TODAY, status="DONE"), [], TODAY, 5) is False


def test_state_machine_valid_paths():
    assert ensure_transition("WAITING", "SCHEDULED") == "SCHEDULED"
    assert ensure_transition("SCHEDULED", "RUNNING") == "RUNNING"
    assert ensure_transition("RUNNING", "DONE") == "DONE"
    assert ensure_transition("RUNNING", "FAILED") == "FAILED"
    # 已开测可回退释放仪器；已排定可撤换
    assert ensure_transition("RUNNING", "WAITING") == "WAITING"
    assert ensure_transition("SCHEDULED", "WAITING") == "WAITING"


def test_state_machine_rejects_illegal_and_terminal():
    with pytest.raises(ValueError):
        ensure_transition("DONE", "RUNNING")
    with pytest.raises(ValueError):
        ensure_transition("FAILED", "WAITING")
    with pytest.raises(ValueError):
        ensure_transition("WAITING", "DONE")


def test_running_cannot_be_moved_by_scheduler():
    """已开测样品不能挪走：pick_next 不触碰 RUNNING，且其名额持续占用。"""
    running = t("1", TODAY, status="RUNNING", scheduled=TODAY)
    waiting = t("2", TODAY)
    assert pick_next([running, waiting], TODAY, 1) == []


def test_retry_creates_new_waiting_without_occupying():
    failed = t("1", TODAY, ["BLANK_FAIL"], status="FAILED", scheduled=TODAY, attempt=1)
    clone = retry_task(failed, "2", "2026-11-19T00:00:00+00:00")
    assert clone["task_id"] == "2"
    assert clone["status"] == "WAITING"
    assert clone["scheduled_date"] is None
    assert clone["attempt"] == 2
    assert clone["retry_of"] == "1"
    # 沿用阻断原因，污染排查链路不丢失
    assert clone["reasons"] == ["BLANK_FAIL"]


def test_retry_only_from_failed():
    with pytest.raises(ValueError):
        retry_task(t("1", TODAY, status="WAITING"), "2", "x")


def test_retry_does_not_double_book():
    failed = t("1", TODAY, status="FAILED", scheduled=TODAY, attempt=1)
    clone = retry_task(failed, "2", "x")
    # 新任务回到候补，当天占位只剩原任务（FAILED 已释放，实际为 0）
    assert occupied([clone], TODAY) == 0


def test_collect_reasons_merges_release_and_checks():
    ctx = {
        "released": [{"batch": "B02", "analyte": "Pb", "status": "BLANK_FAIL"}],
        "blank_checks": [{"batch": "B02", "analyte": "Pb", "status": "BLANK_FAIL"}],
        "parallel": [{"batch": "B02", "analyte": "Pb", "status": "PAIR_FAIL"}],
        "recoveries": [],
    }
    reasons = collect_reasons(ctx, "B02", "Pb")
    assert "BLANK_FAIL" in reasons and "PAIR_FAIL" in reasons
    # 空白原因排在前面，与 M6 优先级一致
    assert reasons[0] == "BLANK_FAIL"


def test_collect_reasons_empty_for_passing_batch():
    ctx = {"released": [{"batch": "B01", "analyte": "Pb", "status": "PASS"}],
           "blank_checks": [], "parallel": [], "recoveries": []}
    assert collect_reasons(ctx, "B01", "Pb") == []
