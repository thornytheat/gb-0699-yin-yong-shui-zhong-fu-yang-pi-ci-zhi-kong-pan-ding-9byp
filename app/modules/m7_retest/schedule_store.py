"""M7 复测排程持久化。

独立 SQLite 文件（schedule.sqlite），与运行记录库隔离。
容量约束在事务内复核，配合 BEGIN IMMEDIATE 串行化并发写入，保证并发排程不超额。
"""
import json
import sqlite3
from datetime import date, datetime, timezone

from app.config import DATA_DIR
from app.errors import BusinessError
from app.modules.m7_retest import schedule as sched

DB_NAME = "schedule.sqlite"

# task_id 全局唯一，作为状态迁移的乐观锁；retry_of 串起重试链路。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS retest_tasks (
    task_id        TEXT PRIMARY KEY,
    sample_id      TEXT NOT NULL,
    batch          TEXT NOT NULL,
    analyte        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    reasons        TEXT NOT NULL,
    due_date       TEXT NOT NULL,
    status         TEXT NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 1,
    retry_of       TEXT,
    run_id         TEXT,
    scheduled_date TEXT,
    created_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT,
    note           TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS schedule_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

VALID_STATUSES = {"WAITING", "SCHEDULED", "RUNNING", "DONE", "FAILED"}


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / DB_NAME, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _row_to_task(row):
    task = dict(row)
    task["reasons"] = json.loads(task["reasons"])
    return task


def reset():
    """测试辅助：清空排程表与容量设置。"""
    with connect() as db:
        db.execute("DELETE FROM retest_tasks")
        db.execute("DELETE FROM schedule_meta")


def get_capacity():
    with connect() as db:
        row = db.execute("SELECT value FROM schedule_meta WHERE key='capacity'").fetchone()
    return int(row["value"]) if row else 1


def set_capacity(capacity):
    if not isinstance(capacity, int) or capacity < 0:
        raise BusinessError("CAPACITY", "每日名额须为非负整数")
    with connect() as db:
        db.execute(
            "INSERT INTO schedule_meta(key,value) VALUES('capacity',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(capacity),),
        )
    return capacity


def get_task(task_id):
    with connect() as db:
        row = db.execute("SELECT * FROM retest_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise BusinessError("NOT_FOUND", "复测任务不存在", 404)
    return _row_to_task(row)


def list_tasks(status=None):
    sql = "SELECT * FROM retest_tasks"
    args = ()
    if status:
        if status not in VALID_STATUSES:
            raise BusinessError("STATUS", "状态码无效")
        sql += " WHERE status=?"
        args = (status,)
    sql += " ORDER BY created_at, task_id"
    with connect() as db:
        return [_row_to_task(r) for r in db.execute(sql, args)]


def _active_duplicate(db, sample_id, batch, analyte, exclude=None):
    """同一未结束的样品/批/检测项不允许重复建单（重试除外，重试由链路产生）。"""
    sql = ("SELECT task_id FROM retest_tasks WHERE sample_id=? AND batch=? AND analyte=? "
           "AND status IN ('WAITING','SCHEDULED','RUNNING')")
    args = [sample_id, batch, analyte]
    if exclude:
        sql += " AND task_id<>?"
        args.append(exclude)
    return db.execute(sql, args).fetchone()


def create_task(payload):
    required = ("sample_id", "batch", "analyte", "kind", "reasons", "due_date")
    for key in required:
        if not payload.get(key):
            raise BusinessError("TASK", f"缺少字段 {key}")
    reasons = payload["reasons"]
    if not isinstance(reasons, list) or not all(isinstance(x, str) for x in reasons):
        raise BusinessError("TASK", "阻断原因须为字符串数组")
    due = sched.parse_day(payload["due_date"])  # 校验日期格式
    task_id = str(payload["task_id"]) if payload.get("task_id") else _generate_id()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        if _active_duplicate(db, payload["sample_id"], payload["batch"], payload["analyte"]):
            raise BusinessError("DUPLICATE", "该样品已存在未结束的复测任务", 409)
        db.execute(
            "INSERT INTO retest_tasks(task_id,sample_id,batch,analyte,kind,reasons,due_date,"
            "status,attempt,run_id,scheduled_date,created_at,note) VALUES(?,?,?,?,?,?,?,'WAITING',1,?,?,?,?)",
            (task_id, payload["sample_id"], payload["batch"], payload["analyte"], payload["kind"],
             json.dumps(reasons, ensure_ascii=False), due.isoformat(),
             payload.get("run_id"), None, now, str(payload.get("note", ""))),
        )
    return get_task(task_id)


def _generate_id():
    return "RT" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def schedule_for_day(day=None, capacity=None):
    """按优先级把候补任务排进当天，受剩余名额约束。返回本次新排定的任务列表。

    BEGIN IMMEDIATE 取写锁，事务内重新统计占位，杜绝并发超额。
    """
    target = sched.parse_day(day or date.today())
    cap = capacity if capacity is not None else get_capacity()
    if not isinstance(cap, int) or cap < 0:
        raise BusinessError("CAPACITY", "每日名额须为非负整数")
    chosen = []
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        tasks = [_row_to_task(r) for r in db.execute("SELECT * FROM retest_tasks")]
        for task_id in sched.pick_next(tasks, target, cap):
            db.execute(
                "UPDATE retest_tasks SET status='SCHEDULED',scheduled_date=? WHERE task_id=? AND status='WAITING'",
                (target.isoformat(), task_id),
            )
            chosen.append(task_id)
    return [get_task(i) for i in chosen]


def transition(task_id, target, day=None):
    """通用状态迁移，带乐观锁与容量/不可挪动校验。"""
    if target not in VALID_STATUSES:
        raise BusinessError("STATUS", "状态码无效")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM retest_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise BusinessError("NOT_FOUND", "复测任务不存在", 404)
        current = _row_to_task(row)
        try:
            sched.ensure_transition(current["status"], target)
        except ValueError as exc:
            raise BusinessError("TRANSITION", str(exc), 409)
        now = datetime.now(timezone.utc).isoformat()
        if target == "SCHEDULED":
            target_day = sched.parse_day(day or date.today())
            tasks = [_row_to_task(r) for r in db.execute("SELECT * FROM retest_tasks WHERE task_id<>?", (task_id,))]
            if sched.available_capacity(tasks, target_day, get_capacity()) <= 0:
                raise BusinessError("CAPACITY", "当天复测名额已满", 409)
            db.execute("UPDATE retest_tasks SET status='SCHEDULED',scheduled_date=? WHERE task_id=?",
                       (target_day.isoformat(), task_id))
        elif target == "RUNNING":
            # 已开测后不可再被排程挪动；RUNNING 持续占位
            db.execute("UPDATE retest_tasks SET status='RUNNING',started_at=COALESCE(started_at,?) WHERE task_id=?",
                       (now, task_id))
        elif target in ("DONE", "FAILED"):
            db.execute("UPDATE retest_tasks SET status=?,finished_at=? WHERE task_id=?", (target, now, task_id))
        else:  # 回退 WAITING，仅 SCHEDULED/RUNNING 可回退，释放当天名额
            db.execute("UPDATE retest_tasks SET status='WAITING',scheduled_date=NULL WHERE task_id=?", (task_id,))
    return get_task(task_id)


def retry_failed(task_id, new_id=None):
    """失败重试：原 FAILED 任务保留为证据，另建一条 WAITING 任务，不占名额。"""
    parent = get_task(task_id)
    if parent["status"] != "FAILED":
        raise BusinessError("TRANSITION", "仅失败任务可重试", 409)
    now = datetime.now(timezone.utc).isoformat()
    new_id = new_id or _generate_id()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM retest_tasks WHERE task_id=?", (new_id,)).fetchone():
            raise BusinessError("DUPLICATE", "新任务号已存在", 409)
        clone = sched.retry_task(parent, new_id, now)
        db.execute(
            "INSERT INTO retest_tasks(task_id,sample_id,batch,analyte,kind,reasons,due_date,status,"
            "attempt,retry_of,run_id,scheduled_date,created_at,started_at,finished_at,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (clone["task_id"], clone["sample_id"], clone["batch"], clone["analyte"], clone["kind"],
             json.dumps(clone["reasons"], ensure_ascii=False), clone["due_date"], clone["status"],
             clone["attempt"], clone["retry_of"], clone.get("run_id"), clone["scheduled_date"],
             clone["created_at"], clone["started_at"], clone["finished_at"], clone.get("note", "")),
        )
    return get_task(new_id)


def workbench(day=None, capacity=None):
    """工作台视图：当天排定/候补/已开测及理由，含名额与候补优先级。"""
    target = sched.parse_day(day or date.today())
    cap = capacity if capacity is not None else get_capacity()
    tasks = list_tasks()
    scheduled = [t for t in tasks if t["status"] in sched.OCCUPYING_STATES
                 and t.get("scheduled_date") and sched.parse_day(t["scheduled_date"]) == target]
    running = [t for t in tasks if t["status"] == "RUNNING"]
    waiting = sched.sort_tasks([t for t in tasks if t["status"] == "WAITING"], target)
    for t in waiting:
        t["urgency"] = sched.urgency(t["due_date"], target)
        t["contamination"] = sched.is_contamination(t["reasons"])
    return {
        "date": target.isoformat(),
        "capacity": cap,
        "occupied": sched.occupied(tasks, target),
        "remaining": sched.available_capacity(tasks, target, cap),
        "scheduled": scheduled,
        "running": running,
        "waiting": waiting,
    }
