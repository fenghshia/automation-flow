(() => {
  "use strict";
  const container = document.getElementById("service-snapshot");
  const localTimes = () => document.querySelectorAll("time[data-local-time]").forEach((node) => {
    const date = new Date(node.dateTime);
    if (node.dateTime && !Number.isNaN(date.getTime())) {
      node.textContent = date.toLocaleString("zh-CN", { hour12: false });
    }
  });
  localTimes();
  if (!container) return;
  const status = document.getElementById("refresh-status");
  const updated = document.getElementById("last-refresh");
  const error = document.getElementById("refresh-error");
  const now = document.getElementById("refresh-now");
  const toggle = document.getElementById("toggle-refresh");
  let paused = false, busy = false, timer, controller, failures = 0;
  const editing = () => container.contains(document.activeElement) &&
    document.activeElement.matches("input, select, textarea");
  function showTimestamp() {
    const value = container.querySelector("[data-updated-at]")?.dataset.updatedAt;
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) updated.textContent = date.toLocaleTimeString("zh-CN", { hour12: false });
    localTimes();
  }
  function schedule() {
    clearTimeout(timer);
    if (!paused && !document.hidden) timer = setTimeout(() => refresh(), Math.min(30000, 5000 * (failures + 1)));
  }
  async function refresh(force = false) {
    if (busy || document.hidden || (paused && !force)) return;
    if (editing()) {
      status.textContent = "编辑设置中，稍后刷新";
      schedule();
      return;
    }
    clearTimeout(timer);
    busy = true;
    now.disabled = true;
    status.textContent = "刷新中…";
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(container.dataset.refreshUrl, { cache: "no-store", signal: controller.signal });
      if (!response.ok || !response.headers.get("Content-Type")?.includes("text/html")) throw new Error("Unavailable snapshot");
      const html = await response.text();
      const parsed = new DOMParser().parseFromString(html, "text/html");
      if (!parsed.querySelector(".snapshot-body")) throw new Error("Invalid snapshot");
      if (editing()) return;
      container.replaceChildren(...document.importNode(parsed.body, true).childNodes);
      failures = 0;
      error.textContent = "";
      showTimestamp();
    } catch (_) {
      failures += 1;
      error.textContent = "刷新失败，仍显示上次数据；稍后重试。";
    } finally {
      clearTimeout(timeout);
      busy = false;
      now.disabled = false;
      status.textContent = paused ? "已暂停刷新" : (failures ? "等待重试" : "每 5 秒刷新");
      schedule();
    }
  }
  now.addEventListener("click", () => refresh(true));
  toggle.addEventListener("click", () => {
    paused = !paused;
    toggle.textContent = paused ? "恢复" : "暂停";
    toggle.setAttribute("aria-pressed", String(paused));
    status.textContent = paused ? "已暂停刷新" : "每 5 秒刷新";
    schedule();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(timer);
    else if (!paused) refresh();
  });
  showTimestamp();
  schedule();
})();
