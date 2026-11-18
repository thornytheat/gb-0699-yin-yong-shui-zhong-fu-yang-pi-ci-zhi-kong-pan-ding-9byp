from fastapi import APIRouter, Body

from app.errors import BusinessError, object_payload
from app.modules.m7_retest import schedule_store as store
from app.modules.m7_retest.service import execute
from app.modules.m7_retest.schedule import collect_reasons, parse_day

router = APIRouter()


@router.post("/api/7")
def calculate(payload: object = Body(default=None)):
    payload = object_payload(payload)
    from app.pipeline import sample_text
    text = payload.get("text", sample_text())
    if not isinstance(text, str):
        raise BusinessError("TEXT", "输入须为文本")
    return execute(text)


@router.get("/api/7")
def sample():
    from app.pipeline import sample_text
    return execute(sample_text())


# ---- 复测排程工作台 ----

def _seed_from_run(run_id, due_date):
    """依据一次判定运行的受阻批次生成复测任务，受阻原因随任务带入排程。"""
    from app import store as run_store
    item = run_store.get(run_id)
    result = item["result"]
    created = []
    for r in result.get("final", []):
        reasons = collect_reasons(result, r["batch"], r["analyte"])
        task = store.create_task({
            "sample_id": r["id"], "batch": r["batch"], "analyte": r["analyte"],
            "kind": r["kind"], "reasons": reasons, "due_date": due_date,
            "run_id": run_id,
        })
        created.append(task)
    return created


@router.get("/api/7/schedule")
def schedule_view(day: str | None = None):
    return store.workbench(day)


@router.post("/api/7/schedule/seed")
def seed_schedule(payload: object = Body(...)):
    payload = object_payload(payload)
    run_id = payload.get("run_id")
    if not isinstance(run_id, str):
        raise BusinessError("TASK", "run_id 须为文本")
    due_date = payload.get("due_date")
    parse_day(due_date)  # 校验
    return {"created": _seed_from_run(run_id, due_date)}


@router.post("/api/7/schedule/tasks")
def create_task(payload: object = Body(...)):
    return store.create_task(object_payload(payload))


@router.post("/api/7/schedule/plan")
def plan_day(payload: object = Body(default=None)):
    payload = object_payload(payload) if isinstance(payload, dict) else {}
    capacity = payload.get("capacity")
    return {"scheduled": store.schedule_for_day(payload.get("day"), capacity)}


@router.post("/api/7/schedule/capacity")
def set_capacity(payload: object = Body(...)):
    payload = object_payload(payload)
    capacity = payload.get("capacity")
    if type(capacity) is not int:
        raise BusinessError("CAPACITY", "每日名额须为非负整数")
    return {"capacity": store.set_capacity(capacity)}


@router.post("/api/7/schedule/tasks/{task_id}/transition")
def transition(task_id: str, payload: object = Body(...)):
    payload = object_payload(payload)
    target = payload.get("status")
    if not isinstance(target, str):
        raise BusinessError("STATUS", "目标状态须为文本")
    return store.transition(task_id, target, payload.get("day"))


@router.post("/api/7/schedule/tasks/{task_id}/retry")
def retry(task_id: str, payload: object = Body(default=None)):
    new_id = object_payload(payload).get("new_id") if isinstance(payload, dict) else None
    return {"task": store.retry_failed(task_id, new_id)}
