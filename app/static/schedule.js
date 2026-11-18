// M7 复测排程工作台：名额约束、污染优先候补、状态迁移与失败重试
(function () {
  const panel = document.querySelector(".schedule-panel");
  if (!panel) return;

  const runId = panel.dataset.runId;
  const meta = document.getElementById("sched-meta");
  const notice = document.getElementById("sched-notice");
  const capInput = document.getElementById("sched-capacity");
  const dayInput = document.getElementById("sched-day");
  const scheduledEl = document.getElementById("sched-scheduled");
  const waitingEl = document.getElementById("sched-waiting");

  const STATUS_CN = {
    WAITING: "候补", SCHEDULED: "已排定", RUNNING: "已开测", DONE: "完成", FAILED: "失败",
  };
  const URGENCY_CN = {
    OVERDUE_TODAY: "当天到期", DUE_SOON: "次日到期", NORMAL: "常规",
  };

  const today = () => new Date().toISOString().slice(0, 10);
  dayInput.value = today();

  async function call(url, options) {
    const res = await fetch(url, Object.assign(
      { headers: { "Content-Type": "application/json" } }, options));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || "请求失败");
    return data;
  }
  const post = (url, body) => call(url, { method: "POST", body: JSON.stringify(body || {}) });

  function taskCard(t, isWaiting) {
    const el = document.createElement("article");
    el.className = "task " + (t.contamination ? "contam" : "normal");
    const tags = t.reasons.map((r) => `<code>${r}</code>`).join("");
    const urgent = isWaiting ? `<span class="urgency u-${t.urgency}">${URGENCY_CN[t.urgency] || t.urgency}</span>` : "";
    const contam = t.contamination ? '<span class="badge-contam">污染排查</span>' : "";
    const actions = [];
    if (t.status === "SCHEDULED") {
      actions.push(`<button data-act="RUNNING" data-id="${t.task_id}">开测</button>`);
      actions.push(`<button data-act="WAITING" data-id="${t.task_id}">撤回候补</button>`);
    } else if (t.status === "RUNNING") {
      actions.push(`<button data-act="DONE" data-id="${t.task_id}">完成</button>`);
      actions.push(`<button data-act="FAILED" data-id="${t.task_id}">失败</button>`);
    } else if (t.status === "FAILED") {
      actions.push(`<button data-act="retry" data-id="${t.task_id}">重试</button>`);
    }
    el.innerHTML = `
      <header><b>${t.sample_id}</b><span class="st st-${t.status}">${STATUS_CN[t.status] || t.status}</span>${contam}${urgent}</header>
      <small>${t.batch} · ${t.analyte} · ${t.kind} · 到期 ${t.due_date}${t.attempt > 1 ? " · 第" + t.attempt + "次" : ""}</small>
      <div class="task-reasons">${tags}</div>
      <div class="task-actions">${actions.join("")}</div>`;
    return el;
  }

  async function load() {
    const day = dayInput.value || today();
    try {
      const data = await call(`/api/7/schedule?day=${encodeURIComponent(day)}`);
      capInput.value = data.capacity;
      meta.textContent = `${data.date} · 名额 ${data.occupied}/${data.capacity}（剩余 ${data.remaining}）`;
      scheduledEl.innerHTML = "";
      waitingEl.innerHTML = "";
      data.scheduled.forEach((t) => scheduledEl.appendChild(taskCard(t, false)));
      data.waiting.forEach((t) => waitingEl.appendChild(taskCard(t, true)));
      if (!data.scheduled.length) scheduledEl.innerHTML = '<p class="empty">当天暂无排定任务</p>';
      if (!data.waiting.length) waitingEl.innerHTML = '<p class="empty">候补队列已空</p>';
      notice.textContent = `候补 ${data.waiting.length} 个，已排定/开测 ${data.scheduled.length} 个`;
    } catch (err) {
      notice.textContent = err.message;
    }
  }

  panel.addEventListener("click", async (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    const { act, id } = btn.dataset;
    btn.disabled = true;
    try {
      if (act === "retry") {
        await post(`/api/7/schedule/tasks/${encodeURIComponent(id)}/retry`, {});
      } else {
        await post(`/api/7/schedule/tasks/${encodeURIComponent(id)}/transition`,
          { status: act, day: dayInput.value || today() });
      }
      await load();
    } catch (err) {
      notice.textContent = err.message;
      btn.disabled = false;
    }
  });

  document.getElementById("sched-refresh").onclick = load;
  dayInput.onchange = load;

  capInput.onchange = async () => {
    try {
      await post("/api/7/schedule/capacity", { capacity: Number(capInput.value) });
      await load();
    } catch (err) {
      notice.textContent = err.message;
    }
  };

  document.getElementById("sched-seed").onclick = async (event) => {
    event.target.disabled = true;
    try {
      const data = await post("/api/7/schedule/seed", { run_id: runId, due_date: dayInput.value || today() });
      notice.textContent = `已生成 ${data.created.length} 个复测任务，阻断原因已带入`;
      await load();
    } catch (err) {
      notice.textContent = err.message;
      event.target.disabled = false;
    }
  };

  document.getElementById("sched-plan").onclick = async (event) => {
    event.target.disabled = true;
    try {
      const data = await post("/api/7/schedule/plan",
        { day: dayInput.value || today(), capacity: Number(capInput.value) });
      notice.textContent = data.scheduled.length
        ? `已排定 ${data.scheduled.length} 个：${data.scheduled.map((t) => t.sample_id).join("、")}`
        : "没有可占用名额或候补已空";
      await load();
    } catch (err) {
      notice.textContent = err.message;
    } finally {
      event.target.disabled = false;
    }
  };

  load();
})();
