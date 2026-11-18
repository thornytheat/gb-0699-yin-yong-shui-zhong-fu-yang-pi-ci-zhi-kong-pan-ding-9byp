"""M7 复测排程纯逻辑。

不触碰数据库与 HTTP，便于单测。状态机与容量约束集中在此，持久化层只负责落库。

名额约束：当天 SCHEDULED + RUNNING 之和不得超过当日容量。
状态机：WAITING -> SCHEDULED -> RUNNING -> DONE / FAILED；
        RUNNING 可回退 WAITING（释放仪器）；FAILED 仅可重试为新的 WAITING 任务。
"""
from datetime import date, datetime

# 空白类阻断原因：污染排查优先于普通临期报告
BLANK_REASONS = frozenset({"BLANK_FAIL", "BLANK_MISSING"})
# 阻断原因展示顺序，沿用 M6 放行优先级
REASON_ORDER = (
    "BLANK_FAIL", "BLANK_MISSING", "RECOVERY_FAIL", "SPIKE_ZERO",
    "BASE_MISSING", "PAIR_FAIL", "PAIR_MISSING", "CENSORED_REVIEW",
)
ACTIVE_STATES = frozenset({"WAITING", "SCHEDULED", "RUNNING"})
OCCUPYING_STATES = frozenset({"SCHEDULED", "RUNNING"})
TERMINAL_STATES = frozenset({"DONE", "FAILED"})


def parse_day(value):
    """接受 date 或 ISO(YYYY-MM-DD) 字符串，统一返回 date。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("日期须为 YYYY-MM-DD")


def urgency(due, today):
    """临期分级：当天到期 OVERDUE_TODAY，次日 DUE_SOON，其余 NORMAL。"""
    delta = (parse_day(due) - parse_day(today)).days
    if delta <= 0:
        return "OVERDUE_TODAY"
    if delta == 1:
        return "DUE_SOON"
    return "NORMAL"


def is_contamination(reasons):
    """受阻原因里带空白类，即视为污染排查任务。"""
    return bool(BLANK_REASONS.intersection(reasons or ()))


def priority_key(task, today):
    """排序键：先污染排查，再临期紧迫度，再创建时间，最后任务号兜底稳定。

    数值越小越靠前。污染批即使不临期也排在普通临期样之前——污染影响整批放行，
    查清后才能解除该批所有报告的阻断。
    """
    rank = {"OVERDUE_TODAY": 0, "DUE_SOON": 1, "NORMAL": 2}
    return (
        0 if is_contamination(task.get("reasons")) else 1,
        rank[urgency(task["due_date"], today)],
        str(task.get("created_at", "")),
        str(task.get("task_id", "")),
    )


def sort_tasks(tasks, today):
    return sorted(tasks, key=lambda t: priority_key(t, today))


def occupied(tasks, day):
    """当天已占位（已排定 + 已开测）数量。已开测样品不可挪走，故 RUNNING 计入。"""
    target = parse_day(day)
    return sum(
        1 for t in tasks
        if t.get("status") in OCCUPYING_STATES
        and t.get("scheduled_date") is not None
        and parse_day(t["scheduled_date"]) == target
    )


def available_capacity(tasks, day, capacity):
    """当天剩余名额。"""
    if not isinstance(capacity, int) or capacity < 0:
        raise ValueError("容量须为非负整数")
    return max(0, capacity - occupied(tasks, day))


def pick_next(tasks, today, capacity):
    """从候补里挑出当天应排定的任务，按优先级填充剩余名额。

    只考虑 WAITING 任务；SCHEDULED/RUNNING 已占位，DONE/FAILED 已结束。
    返回任务 ID 有序列表，调用方负责落库。
    """
    target = parse_day(today)
    waiting = [t for t in tasks if t.get("status") == "WAITING"]
    free = available_capacity(tasks, target, capacity)
    return [t["task_id"] for t in sort_tasks(waiting, target)[:free]]


def ensure_transition(current, target):
    """校验状态迁移是否合法，非法则 ValueError。"""
    allowed = {
        "WAITING": {"SCHEDULED"},
        "SCHEDULED": {"RUNNING", "WAITING"},
        "RUNNING": {"DONE", "FAILED", "WAITING"},
        "DONE": set(),
        "FAILED": set(),
    }
    if target not in allowed.get(current, ()):
        raise ValueError(f"状态迁移非法: {current} -> {target}")
    return target


def can_schedule(task, tasks, day, capacity):
    """排定前置条件：任务处于 WAITING 且当天仍有名额。"""
    if task.get("status") != "WAITING":
        return False
    return available_capacity(tasks, day, capacity) > 0


def retry_task(task, new_id, created_at):
    """失败重试：原任务须 FAILED；生成新的 WAITING 任务，沿用原排程理由。

    重试不重复占位——新任务回到候补队列，不自动占用名额。
    重试次数 +1，关联原任务号形成链路。created_at 由调用方注入，保持纯函数。
    """
    if task.get("status") != "FAILED":
        raise ValueError("仅失败任务可重试")
    clone = dict(task)
    clone["task_id"] = new_id
    clone["status"] = "WAITING"
    clone["scheduled_date"] = None
    clone["started_at"] = None
    clone["finished_at"] = None
    clone["retry_of"] = task.get("retry_of") or task["task_id"]
    clone["attempt"] = int(task.get("attempt", 1)) + 1
    clone["created_at"] = created_at
    return clone


def collect_reasons(ctx, batch, analyte):
    """汇总某检测批的阻断原因：放行结论 + 各质控检查的非 PASS 状态。"""
    found = set()
    for r in ctx.get("released", []):
        if r["batch"] == batch and r["analyte"] == analyte and r["status"] != "PASS":
            found.add(r["status"])
    for source in ("parallel", "recoveries", "blank_checks"):
        for r in ctx.get(source, []):
            if r["batch"] == batch and r["analyte"] == analyte and r["status"] != "PASS":
                found.add(r["status"])
    return [c for c in REASON_ORDER if c in found] or sorted(found)
