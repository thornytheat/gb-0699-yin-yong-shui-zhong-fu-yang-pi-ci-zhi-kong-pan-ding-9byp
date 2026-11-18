# TRAP-27 排程接口：工作台、阻断原因带入、容量与重试端到端
import pytest

from app.errors import BusinessError
from app.modules.m7_retest import schedule_store as st


@pytest.fixture(autouse=True)
def clean_schedule():
    st.reset()
    st.set_capacity(1)
    yield
    st.reset()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_workbench_empty(client):
    r = client.get("/api/7/schedule")
    assert r.status_code == 200
    body = r.json()
    assert body["capacity"] == 1 and body["waiting"] == []


def test_seed_carries_blocking_reasons(client):
    run = client.post("/api/run").json()  # 默认样例，B02 阻断
    r = client.post("/api/7/schedule/seed", json={"run_id": run["id"], "due_date": "2026-11-18"})
    assert r.status_code == 200
    created = r.json()["created"]
    assert created, "受阻批次应生成复测任务"
    # 每个任务都带阻断原因，且来自 B02
    assert all(t["batch"] == "B02" for t in created)
    assert all(t["reasons"] for t in created)


def test_plan_endpoint_enforces_single_slot(client):
    st.create_task({"task_id": "A", "sample_id": "SA", "batch": "B", "analyte": "Pb",
                    "kind": "sample", "reasons": ["PAIR_FAIL"], "due_date": "2026-11-18"})
    st.create_task({"task_id": "B", "sample_id": "SB", "batch": "B", "analyte": "Pb",
                    "kind": "sample", "reasons": ["PAIR_FAIL"], "due_date": "2026-11-18"})
    r = client.post("/api/7/schedule/plan", json={"day": "2026-11-18", "capacity": 1})
    assert r.status_code == 200
    assert [t["task_id"] for t in r.json()["scheduled"]] == ["A"]
    # 重复触发不重复占位
    r2 = client.post("/api/7/schedule/plan", json={"day": "2026-11-18", "capacity": 1})
    assert r2.json()["scheduled"] == []


def test_lifecycle_and_retry_over_http(client):
    st.create_task({"task_id": "A", "sample_id": "SA", "batch": "B", "analyte": "Pb",
                    "kind": "sample", "reasons": ["BLANK_FAIL"], "due_date": "2026-11-18"})
    client.post("/api/7/schedule/plan", json={"day": "2026-11-18", "capacity": 1})
    assert client.post("/api/7/schedule/tasks/A/transition", json={"status": "RUNNING"}).json()["status"] == "RUNNING"
    assert client.post("/api/7/schedule/tasks/A/transition", json={"status": "FAILED"}).json()["status"] == "FAILED"
    retry = client.post("/api/7/schedule/tasks/A/retry", json={"new_id": "A2"}).json()["task"]
    assert retry["status"] == "WAITING" and retry["attempt"] == 2
    view = client.get("/api/7/schedule").json()
    assert view["occupied"] == 0
    assert "A2" in {t["task_id"] for t in view["waiting"]}


def test_overbook_returns_409(client):
    st.create_task({"task_id": "A", "sample_id": "SA", "batch": "B", "analyte": "Pb",
                    "kind": "sample", "reasons": ["PAIR_FAIL"], "due_date": "2026-11-18"})
    st.create_task({"task_id": "B", "sample_id": "SB", "batch": "B", "analyte": "Pb",
                    "kind": "sample", "reasons": ["PAIR_FAIL"], "due_date": "2026-11-18"})
    client.post("/api/7/schedule/plan", json={"day": "2026-11-18", "capacity": 1})
    r = client.post("/api/7/schedule/tasks/B/transition", json={"status": "SCHEDULED", "day": "2026-11-18"})
    assert r.status_code == 409 and r.json()["error"] == "CAPACITY"


def test_capacity_update_and_validation(client):
    assert client.post("/api/7/schedule/capacity", json={"capacity": 3}).json()["capacity"] == 3
    bad = client.post("/api/7/schedule/capacity", json={"capacity": -2})
    assert bad.status_code == 422


def test_duplicate_seed_task_conflict(client):
    payload = {"sample_id": "SA", "batch": "B", "analyte": "Pb",
               "kind": "sample", "reasons": ["PAIR_FAIL"], "due_date": "2026-11-18"}
    client.post("/api/7/schedule/tasks", json=payload)
    dup = client.post("/api/7/schedule/tasks", json=payload)
    assert dup.status_code == 409 and dup.json()["error"] == "DUPLICATE"


def test_seed_unknown_run_404(client):
    r = client.post("/api/7/schedule/seed", json={"run_id": "nope", "due_date": "2026-11-18"})
    assert r.status_code == 404
