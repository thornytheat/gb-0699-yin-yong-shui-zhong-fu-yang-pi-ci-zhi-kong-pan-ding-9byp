# TRAP-27 排程持久化：并发不超额、重试不重复占位、已开测不可挪
import threading

import pytest

from app.errors import BusinessError
from app.modules.m7_retest import schedule_store as st

TODAY = "2026-11-18"


@pytest.fixture(autouse=True)
def clean_db():
    st.reset()
    st.set_capacity(1)
    yield
    st.reset()


def make(task_id, due=TODAY, reasons=None, **kw):
    return st.create_task({
        "task_id": task_id, "sample_id": "S" + task_id, "batch": "B02",
        "analyte": "Pb", "kind": "sample", "reasons": reasons or ["PAIR_FAIL"],
        "due_date": due, **kw,
    })


def test_create_and_list_task():
    make("T1")
    tasks = st.list_tasks()
    assert len(tasks) == 1 and tasks[0]["task_id"] == "T1"
    assert tasks[0]["status"] == "WAITING" and tasks[0]["reasons"] == ["PAIR_FAIL"]


def test_duplicate_active_task_rejected():
    make("T1")
    with pytest.raises(BusinessError, match="已存在"):
        make("T2", sample_id="ST1")  # 同样品同批同检测项
    # 不同样品可建单
    make("T2")
    assert len(st.list_tasks()) == 2


def test_capacity_blocks_overbook():
    make("T1")
    make("T2")
    planned = st.schedule_for_day(TODAY, capacity=1)
    assert [t["task_id"] for t in planned] == ["T1"]
    # 名额已满，再次排程不会塞入候补
    assert st.schedule_for_day(TODAY, capacity=1) == []


def test_running_task_holds_slot_and_cannot_be_moved():
    make("T1")
    make("T2")
    st.schedule_for_day(TODAY, 1)
    st.transition("T1", "RUNNING")
    # T1 已开测，持续占位；T2 当天无法排进来
    assert st.schedule_for_day(TODAY, 1) == []
    view = st.workbench(TODAY, 1)
    assert view["occupied"] == 1 and view["remaining"] == 0
    assert {t["task_id"] for t in view["running"]} == {"T1"}


def test_full_lifecycle_and_capacity_release():
    make("T1")
    st.schedule_for_day(TODAY, 1)
    st.transition("T1", "RUNNING")
    st.transition("T1", "DONE")
    # 完成后释放名额，候补可排入
    make("T2")
    planned = st.schedule_for_day(TODAY, 1)
    assert [t["task_id"] for t in planned] == ["T2"]


def test_illegal_transition_rejected():
    make("T1")
    with pytest.raises(BusinessError):
        st.transition("T1", "DONE")  # WAITING 不能直接 DONE


def test_manual_schedule_rejects_when_full():
    make("T1")
    make("T2")
    st.schedule_for_day(TODAY, 1)  # T1 占满
    with pytest.raises(BusinessError, match="名额已满"):
        st.transition("T2", "SCHEDULED", day=TODAY)


def test_retry_failed_waits_without_taking_slot():
    make("T1", reasons=["BLANK_FAIL"])
    st.schedule_for_day(TODAY, 1)
    st.transition("T1", "RUNNING")
    st.transition("T1", "FAILED")
    before = st.workbench(TODAY, 1)
    assert before["occupied"] == 0  # 失败释放名额
    retry = st.retry_failed("T1", new_id="T1R")
    assert retry["status"] == "WAITING" and retry["attempt"] == 2
    assert retry["retry_of"] == "T1"
    after = st.workbench(TODAY, 1)
    # 重试任务在候补，不自动占位
    assert after["occupied"] == 0
    assert [t["task_id"] for t in after["waiting"]] == ["T1R"]


def test_retry_only_allowed_after_failure():
    make("T1")
    with pytest.raises(BusinessError):
        st.retry_failed("T1")


def test_concurrent_planning_never_exceeds_capacity():
    """并发排程：容量 1，多线程同时触发，最终占位不得超过 1。"""
    for i in range(8):
        make(f"T{i}")
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        st.schedule_for_day(TODAY, 1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    view = st.workbench(TODAY, 1)
    assert view["occupied"] == 1
    assert len(view["scheduled"]) == 1


def test_workbench_prioritizes_contamination_and_shows_reasons():
    # 普通临期样 vs 不临期的污染批
    make("DUE", due=TODAY, reasons=["PAIR_FAIL"])
    make("BLANK", due="2026-11-25", reasons=["BLANK_FAIL"])
    view = st.workbench(TODAY, 1)
    assert view["waiting"][0]["task_id"] == "BLANK"
    assert view["waiting"][0]["contamination"] is True
    assert view["waiting"][0]["urgency"] == "NORMAL"
    assert view["waiting"][1]["urgency"] == "OVERDUE_TODAY"
    # 唯一名额给污染排查
    planned = st.schedule_for_day(TODAY, 1)
    assert [t["task_id"] for t in planned] == ["BLANK"]


def test_capacity_validation():
    with pytest.raises(BusinessError):
        st.set_capacity(-1)
    with pytest.raises(BusinessError):
        st.set_capacity("1")  # type: ignore[arg-type]


def test_task_validation():
    with pytest.raises(BusinessError):
        st.create_task({"sample_id": "X", "batch": "B", "analyte": "Pb",
                        "kind": "sample", "reasons": [], "due_date": TODAY})
    with pytest.raises(BusinessError):
        st.create_task({"sample_id": "X", "batch": "B", "analyte": "Pb",
                        "kind": "sample", "reasons": ["PAIR_FAIL"], "due_date": "bad-day"})


def test_unschedule_releases_capacity():
    make("T1")
    make("T2")
    st.schedule_for_day(TODAY, 1)
    st.transition("T1", "WAITING")  # 撤换已排定任务
    view = st.workbench(TODAY, 1)
    assert view["occupied"] == 0 and view["remaining"] == 1
    replanned = st.schedule_for_day(TODAY, 1)
    assert [t["task_id"] for t in replanned] == ["T1"]


def test_capacity_is_per_day():
    make("T1")
    make("T2")
    st.schedule_for_day(TODAY, 1)  # 今天名额被 T1 占满，T2 仍候补
    # 明天是独立名额，候补 T2 可排到明天，不影响今天占用
    tomorrow = st.schedule_for_day("2026-11-19", 1)
    assert [t["task_id"] for t in tomorrow] == ["T2"]
    assert st.get_task("T2")["scheduled_date"] == "2026-11-19"
    assert st.workbench(TODAY, 1)["occupied"] == 1
    assert st.workbench("2026-11-19", 1)["occupied"] == 1
