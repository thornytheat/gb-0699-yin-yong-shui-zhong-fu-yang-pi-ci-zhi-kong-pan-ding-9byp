# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

## M7 复测排程

排程独立存于 `schedule.sqlite`，与运行记录库隔离。当日名额约束 `SCHEDULED + RUNNING <= capacity`；并发写入在 `BEGIN IMMEDIATE` 事务内复核占位，保证不超额。

- `GET /api/7/schedule?day=YYYY-MM-DD`：工作台视图，返回当天已排定/已开测、候补（含污染标记与临期分级）、名额占用。
- `POST /api/7/schedule/seed`：`{run_id, due_date}` 从一次判定运行的受阻批次生成 WAITING 复测任务，阻断原因（空白、平行、加标等）随任务带入。
- `POST /api/7/schedule/tasks`：手工建单，同样品/批/检测项存在未结束任务时 409。
- `POST /api/7/schedule/plan`：`{day?, capacity?}` 按优先级把候补填入当天剩余名额；重复触发不重复占位。
- `POST /api/7/schedule/capacity`：`{capacity}` 设置每日名额（非负整数）。
- `POST /api/7/schedule/tasks/{id}/transition`：`{status, day?}` 状态迁移 `WAITING→SCHEDULED→RUNNING→DONE/FAILED`，RUNNING/SCHEDULED 可回退 WAITING；非法迁移 409，名额已满 409。
- `POST /api/7/schedule/tasks/{id}/retry`：仅 FAILED 可重试，生成新 WAITING 任务（attempt+1、retry_of 链路），不自动占名额。

优先级：空白类原因（BLANK_FAIL/BLANK_MISSING）的污染排查最先，其次临期分级（当天到期 > 次日 > 常规），再按创建时间、任务号兜底。已开测（RUNNING）样品不可被排程挪走并持续占位。


`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`GET /api/export/{id}`：唯一证据CSV。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
