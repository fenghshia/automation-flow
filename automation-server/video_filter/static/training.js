(() => {
  "use strict";
  const snapshot = document.getElementById("service-snapshot");
  const feedback = document.getElementById("training-feedback");
  if (!snapshot || !feedback) return;
  const submitting = new Set();
  const accepted = new Map();
  const drafts = new Map();
  const keyFor = (form) => `${form.action}|${form.dataset.modelType}`;
  const updateButtons = () => snapshot.querySelectorAll(".training-form").forEach((form) => {
    const button = form.querySelector("button");
    const key = keyFor(form);
    const draft = drafts.get(key);
    if (draft) form.querySelectorAll("input[name]").forEach((input) => {
      if (Object.hasOwn(draft, input.name)) input.value = draft[input.name];
    });
    const taskId = accepted.get(key);
    if (taskId) {
      const task = [...snapshot.querySelectorAll("[data-training-task]")]
        .find((node) => node.dataset.trainingTask === taskId);
      if (task && ["succeeded", "failed", "cancelled"].includes(task.dataset.status)) accepted.delete(key);
    }
    const pending = submitting.has(key);
    button.disabled = pending || accepted.has(key) || form.dataset.ready !== "true";
    if (pending && button.textContent !== "正在提交…") button.textContent = "正在提交…";
    else if (accepted.has(key) && !pending && button.textContent !== "训练已排队或进行中") button.textContent = "训练已排队或进行中";
  });
  const messages = {
    insufficient_confirmed_samples_minimum_10_per_class: "可训练样本不足，请补充喜欢和不喜欢的已标注视频及特征摘要。",
    lineage_reconciliation_pending: "文件记录正在对账，完成后可重新提交训练。",
    complete_scan_required: "请等待完整目录扫描结束后再训练。",
    video_filter_disabled: "视频筛选服务尚未启用。",
    group_not_found: "当前分组未启用或已不存在。",
    group_not_initialized: "当前分组尚未初始化，请等待首次扫描。",
    invalid_configuration: "当前配置不可用，请检查本地配置。",
    schema_unavailable: "数据库暂不可用，请稍后再试。",
    operation_unavailable: "训练请求未被接受，请检查配置和服务状态。",
    invalid_training_acceptance: "请输入 0 到 100 之间的喜欢和不喜欢精确率门槛。",
    training_pending_with_different_acceptance: "该模型已有其他验收设置的训练任务，请等任务结束后再提交。"
  };
  const states = { queued: "已排队", running: "正在训练", succeeded: "已有完成的训练任务",
    failed: "已有失败的训练任务，请查看下方错误代码", cancelled: "已有取消的训练任务" };
  updateButtons();
  new MutationObserver(updateButtons).observe(snapshot, { childList: true, subtree: true });
  document.addEventListener("input", (event) => {
    const form = event.target.closest(".training-form");
    if (form) drafts.set(keyFor(form), Object.fromEntries([...form.querySelectorAll("input[name]")]
      .map((input) => [input.name, input.value])));
  });
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches(".training-form")) return;
    event.preventDefault();
    const key = keyFor(form);
    if (form.dataset.ready !== "true" || submitting.has(key) || accepted.has(key)) return;
    if (!form.reportValidity()) return;
    const acceptance = Object.fromEntries([...form.querySelectorAll("input[name]")]
      .map((input) => [input.name, Number(input.value) / 100]));
    if (Object.values(acceptance).some((value) => !Number.isFinite(value) || value < 0 || value > 1)) return;
    submitting.add(key);
    updateButtons();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    feedback.hidden = false;
    feedback.className = "notice";
    feedback.textContent = "正在提交训练任务…";
    try {
      const response = await fetch(form.action, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_type: form.dataset.modelType, acceptance }), signal: controller.signal });
      const data = await response.json();
      if (!response.ok) {
        feedback.className = "notice danger";
        feedback.textContent = messages[data.error_code] || "训练请求未被接受，请检查样本、配置及任务状态。";
      } else if (typeof data.task_id === "string" && states[data.status]) {
        feedback.textContent = `${states[data.status]} · 任务 ${data.task_id.slice(0, 8)}。训练进度与验收结果将在下方更新。`;
        if (data.status === "queued" || data.status === "running") {
          accepted.set(key, data.task_id);
          form.dataset.ready = "false";
          form.querySelector("button").textContent = "训练已排队或进行中";
        }
      } else {
        throw new Error("Invalid training response");
      }
    } catch (_) {
      feedback.className = "notice warning";
      feedback.textContent = "未能确认提交结果，请刷新查看训练任务后再决定是否重试。";
    } finally {
      clearTimeout(timeout);
      submitting.delete(key);
      const current = [...snapshot.querySelectorAll(".training-form")].find((item) => keyFor(item) === key);
      if (current && current.dataset.ready === "true") {
        current.querySelector("button").textContent = form.dataset.modelType === "mil" ? "手动训练 注意力 MIL" : "手动训练 逻辑回归";
      }
      updateButtons();
      document.getElementById("refresh-now")?.click();
    }
  });
})();
