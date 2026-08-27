const state = {
  taskId: null,
  pollTimer: null,
  qrSessionId: null,
  qrPollTimer: null,
  qrStartedAt: null,
  lastTabRequestAt: null,
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  uploadForm: document.querySelector("#uploadForm"),
  linkForm: document.querySelector("#linkForm"),
  mediaFile: document.querySelector("#mediaFile"),
  fileName: document.querySelector("#fileName"),
  uploadMode: document.querySelector("#uploadMode"),
  linkMode: document.querySelector("#linkMode"),
  uploadReal: document.querySelector("#uploadReal"),
  linkReal: document.querySelector("#linkReal"),
  videoUrl: document.querySelector("#videoUrl"),
  taskStatus: document.querySelector("#taskStatus"),
  taskId: document.querySelector("#taskId"),
  taskSource: document.querySelector("#taskSource"),
  taskMessage: document.querySelector("#taskMessage"),
  progressBar: document.querySelector("#progressBar"),
  summaryOutput: document.querySelector("#summaryOutput"),
  transcriptOutput: document.querySelector("#transcriptOutput"),
  rawOutput: document.querySelector("#rawOutput"),
  statusPanel: document.querySelector(".status-panel"),
  cookieForm: document.querySelector("#cookieForm"),
  cookieState: document.querySelector("#cookieState"),
  cookieText: document.querySelector("#cookieText"),
  saveCookieText: document.querySelector("#saveCookieText"),
  verifyCookies: document.querySelector("#verifyCookies"),
  cookieFile: document.querySelector("#cookieFile"),
  cookieMessage: document.querySelector("#cookieMessage"),
  startQrLogin: document.querySelector("#startQrLogin"),
  qrImage: document.querySelector("#qrImage"),
  qrMessage: document.querySelector("#qrMessage"),
  openCookieBrowser: document.querySelector("#openCookieBrowser"),
  checkCookieBrowser: document.querySelector("#checkCookieBrowser"),
  captureCookieBrowser: document.querySelector("#captureCookieBrowser"),
  historyList: document.querySelector("#historyList"),
  historyMessage: document.querySelector("#historyMessage"),
  refreshHistory: document.querySelector("#refreshHistory"),
};

async function fetchJson(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { detail: text || response.statusText };
    }
    if (!response.ok) {
      const message = data.detail || JSON.stringify(data);
      throw new Error(message);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("请求超时，请确认专用登录窗口仍然打开。");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function setStatus(status, message) {
  els.statusPanel.classList.remove("queued", "running", "succeeded", "failed");
  els.statusPanel.classList.add(status);
  els.taskStatus.textContent = status;
  els.taskMessage.textContent = message;

  const width = {
    queued: "18%",
    running: "58%",
    succeeded: "100%",
    failed: "100%",
  }[status] || "0%";
  els.progressBar.style.width = width;
}

function setTask(task) {
  els.taskId.textContent = task.task_id || "-";
  els.taskSource.textContent = task.request?.url || "-";
  els.rawOutput.textContent = JSON.stringify(task, null, 2);

  if (task.status === "queued") {
    setStatus("queued", "任务已提交，等待处理。");
    return;
  }

  if (task.status === "running") {
    setStatus("running", "正在转写和分析，较长视频可能需要一两分钟。");
    return;
  }

  if (task.status === "failed") {
    setStatus("failed", task.error || "任务失败。");
    els.summaryOutput.textContent = task.error || "任务失败。";
    return;
  }

  if (task.status === "succeeded") {
    setStatus("succeeded", "分析完成。");
    els.summaryOutput.textContent = task.result?.output || "无结果";
    els.transcriptOutput.textContent = task.result?.transcript || "无文字稿";
  }
}

function describeCookieSummary(summary) {
  if (!summary?.exists) {
    return "未保存 Cookies";
  }
  const loginState = summary.has_login_cookie ? "检测到登录态" : "未检测到关键登录态";
  const present = summary.present_important?.length ? `；已有：${summary.present_important.join(", ")}` : "";
  return `${summary.count || 0} 条，${loginState}${present}`;
}

async function refreshCookieStatus() {
  try {
    const data = await fetchJson("/cookies/status", {}, 8000);
    els.cookieState.textContent = describeCookieSummary(data);
    return data;
  } catch (error) {
    els.cookieState.textContent = error.message;
    return null;
  }
}

async function pollTask(taskId) {
  window.clearInterval(state.pollTimer);
  state.taskId = taskId;
  els.taskId.textContent = taskId;

  const load = async () => {
    const response = await fetch(`/tasks/${taskId}`);
    const task = await response.json();
    setTask(task);
    if (["succeeded", "failed"].includes(task.status)) {
      window.clearInterval(state.pollTimer);
      await loadHistory();
    }
  };

  await load();
  state.pollTimer = window.setInterval(load, 2500);
}

const STATUS_TEXT = { queued: "排队中", running: "处理中", succeeded: "已完成", failed: "失败" };

function statusBadge(status) {
  const span = document.createElement("span");
  span.className = `badge ${status}`;
  span.textContent = STATUS_TEXT[status] || status;
  return span;
}

async function loadHistory() {
  try {
    const tasks = await fetchJson("/tasks?limit=50", {}, 8000);
    els.historyList.textContent = "";
    if (!tasks.length) {
      els.historyMessage.textContent = "暂无历史任务。";
      return;
    }
    els.historyMessage.textContent = `共 ${tasks.length} 条（最近 50 条）`;
    for (const task of tasks) {
      const item = document.createElement("li");
      item.className = "history-item";

      const head = document.createElement("div");
      head.className = "history-item-head";
      const idEl = document.createElement("span");
      idEl.className = "history-id";
      idEl.textContent = task.task_id;
      head.append(idEl, statusBadge(task.status));

      const urlEl = document.createElement("div");
      urlEl.className = "history-url";
      urlEl.textContent = `${task.request?.mode || "?"} · ${task.request?.url || "-"}`;

      const actions = document.createElement("div");
      actions.className = "history-actions";
      const viewBtn = document.createElement("button");
      viewBtn.type = "button";
      viewBtn.textContent = "查看";
      viewBtn.addEventListener("click", () => viewHistoryTask(task.task_id));
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "delete-btn";
      deleteBtn.textContent = "删除";
      deleteBtn.addEventListener("click", () => deleteHistoryTask(task.task_id));
      actions.append(viewBtn, deleteBtn);

      item.append(head, urlEl, actions);
      els.historyList.append(item);
    }
  } catch (error) {
    els.historyMessage.textContent = `历史任务加载失败：${error.message}`;
  }
}

async function viewHistoryTask(taskId) {
  window.clearInterval(state.pollTimer);
  try {
    const task = await fetchJson(`/tasks/${taskId}`, {}, 10000);
    setTask(task);
    const summaryTab = document.querySelector('.tab[data-target="summaryView"]');
    if (summaryTab) {
      summaryTab.click();
    }
  } catch (error) {
    setStatus("failed", error.message);
  }
}

async function deleteHistoryTask(taskId) {
  if (!window.confirm(`确认删除任务 ${taskId}？关联的结果文件和临时文件也会一并删除。`)) {
    return;
  }
  try {
    const response = await fetch(`/tasks/${taskId}`, { method: "DELETE" });
    if (!response.ok) {
      throw new Error(`删除失败：${response.status}`);
    }
    await loadHistory();
  } catch (error) {
    els.historyMessage.textContent = error.message;
  }
}

els.mediaFile.addEventListener("change", () => {
  const file = els.mediaFile.files?.[0];
  els.fileName.textContent = file ? file.name : "选择音频或视频文件";
});

els.uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = els.mediaFile.files?.[0];
  if (!file) {
    setStatus("failed", "请先选择一个音频或视频文件。");
    return;
  }

  const form = new FormData();
  form.append("file", file);
  form.append("mode", els.uploadMode.value);
  form.append("use_mock", String(!els.uploadReal.checked));

  setStatus("queued", "正在上传文件。");
  const response = await fetch("/analyze-file", {
    method: "POST",
    body: form,
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("failed", JSON.stringify(data));
    return;
  }
  await pollTask(data.task_id);
});

els.linkForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = els.videoUrl.value.trim();
  if (!url) {
    setStatus("failed", "请输入视频链接。");
    return;
  }

  setStatus("queued", "正在提交链接。");
  const response = await fetch("/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      mode: els.linkMode.value,
      use_mock: !els.linkReal.checked,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("failed", JSON.stringify(data));
    return;
  }
  await pollTask(data.task_id);
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    document.querySelectorAll(".view").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`#${tab.dataset.target}`).classList.add("active");
  });
});

els.saveCookieText.addEventListener("click", async () => {
  const text = els.cookieText.value.trim();
  if (!text) {
    els.cookieMessage.textContent = "请先粘贴 Cookie 或 Cookie Editor JSON。";
    return;
  }

  const form = new FormData();
  form.append("file", new Blob([text], { type: "text/plain" }), "cookies.txt");
  els.cookieMessage.textContent = "正在保存粘贴内容...";
  try {
    const data = await fetchJson("/cookies", { method: "POST", body: form }, 12000);
    els.cookieText.value = "";
    els.cookieMessage.textContent = `已保存 ${data.count} 条 Cookies。`;
    await refreshCookieStatus();
    await boot();
  } catch (error) {
    els.cookieMessage.textContent = error.message;
  }
});

els.verifyCookies.addEventListener("click", async () => {
  els.cookieMessage.textContent = "正在检查本地 Cookies...";
  const data = await refreshCookieStatus();
  if (!data) {
    return;
  }
  const missing = data.missing_important?.length ? `；缺少：${data.missing_important.join(", ")}` : "";
  els.cookieMessage.textContent = `${describeCookieSummary(data)}${missing}。`;
});

els.cookieForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = els.cookieFile.files?.[0];
  if (!file) {
    els.cookieMessage.textContent = "请先选择 cookies.txt。";
    return;
  }

  const form = new FormData();
  form.append("file", file);
  els.cookieMessage.textContent = "正在保存 cookies...";
  try {
    const data = await fetchJson("/cookies", { method: "POST", body: form }, 12000);
    els.cookieMessage.textContent = `已保存 ${data.count} 条 Cookies，大小 ${data.size} bytes。`;
    await refreshCookieStatus();
    await boot();
  } catch (error) {
    els.cookieMessage.textContent = error.message;
  }
});

els.startQrLogin.addEventListener("click", async () => {
  window.clearInterval(state.qrPollTimer);
  state.qrSessionId = null;
  state.qrStartedAt = null;
  state.lastTabRequestAt = null;
  els.qrImage.hidden = true;
  els.qrImage.removeAttribute("src");
  els.qrMessage.textContent = "正在打开专用扫码登录窗口...";
  try {
    const data = await fetchJson("/cookies/browser/start", { method: "POST" }, 12000);
    state.qrSessionId = "browser";
    state.qrStartedAt = Date.now();
    els.qrMessage.textContent = data.message || "请在新打开的窗口里用抖音 App 扫码登录，本页会自动检测。";
    state.qrPollTimer = window.setInterval(pollQrLogin, 5000);
  } catch (error) {
    els.qrMessage.textContent = error.message;
  }
});

async function requestDouyinTab() {
  const now = Date.now();
  if (state.lastTabRequestAt && now - state.lastTabRequestAt < 15000) {
    return;
  }
  state.lastTabRequestAt = now;
  try {
    await fetchJson("/cookies/browser/start", { method: "POST" }, 12000);
  } catch {
    // 忽略重开失败，下一次轮询会重新提示状态。
  }
}

async function pollQrLogin() {
  if (!state.qrSessionId) {
    return;
  }
  if (state.qrStartedAt && Date.now() - state.qrStartedAt > 5 * 60 * 1000) {
    window.clearInterval(state.qrPollTimer);
    els.qrMessage.textContent = "扫码检测已暂停，仍未成功时可以重新打开扫码登录窗口。";
    return;
  }
  try {
    const status = await fetchJson("/cookies/browser/status", {}, 8000);
    if (!status.running) {
      els.qrMessage.textContent = "专用登录窗口未连接，请重新打开扫码登录窗口。";
      return;
    }
    if (!status.has_douyin_tab) {
      els.qrMessage.textContent = "专用浏览器已连接，正在打开抖音登录页…";
      await requestDouyinTab();
      return;
    }

    const data = await fetchJson("/cookies/browser/capture", { method: "POST" }, 20000);
    if (data.has_login_cookie) {
      window.clearInterval(state.qrPollTimer);
      els.qrMessage.textContent = `扫码登录成功，已保存 ${data.count} 条 Cookies。`;
      els.cookieMessage.textContent = `扫码登录成功，已保存 ${data.count} 条 Cookies。`;
      await refreshCookieStatus();
      await boot();
      return;
    }
    els.qrMessage.textContent = `窗口已连接，等待扫码确认；当前抓到 ${data.count} 条非登录 Cookies，未覆盖本地文件。`;
  } catch (error) {
    els.qrMessage.textContent = error.message;
  }
}

els.openCookieBrowser.addEventListener("click", async () => {
  els.cookieMessage.textContent = "正在打开专用登录窗口...";
  try {
    const data = await fetchJson("/cookies/browser/start", { method: "POST" });
    els.cookieMessage.textContent =
      data.message || "请在新打开的浏览器窗口登录抖音，登录后点击“检查窗口”或“抓取登录态”。";
  } catch (error) {
    els.cookieMessage.textContent = error.message;
  }
});

els.checkCookieBrowser.addEventListener("click", async () => {
  els.cookieMessage.textContent = "正在检查专用登录窗口...";
  try {
    const data = await fetchJson("/cookies/browser/status", {}, 8000);
    if (!data.running) {
      els.cookieMessage.textContent = "专用登录窗口未连接，请先点击“打开登录窗口”。";
      return;
    }
    const tabText = data.has_douyin_tab ? "已找到抖音页面" : "未找到抖音页面，可再次点击“打开登录窗口”打开抖音页";
    els.cookieMessage.textContent = `DevTools 已连接，${tabText}，标签页 ${data.tabs.length} 个。`;
  } catch (error) {
    els.cookieMessage.textContent = error.message;
  }
});

els.captureCookieBrowser.addEventListener("click", async () => {
  els.cookieMessage.textContent = "正在抓取专用窗口里的 douyin.com cookies...";
  try {
    const data = await fetchJson("/cookies/browser/capture", { method: "POST" }, 20000);
    const loginState = data.has_login_cookie ? "检测到登录态" : "未检测到关键登录态";
    const missing = data.missing_important?.length ? `；缺少：${data.missing_important.join(", ")}` : "";
    const savedText = data.saved ? `已保存 ${data.count} 条 cookies` : `抓到 ${data.count} 条 cookies，未覆盖本地文件`;
    els.cookieMessage.textContent = `${savedText}，${loginState}${missing}。`;
    await refreshCookieStatus();
    await boot();
  } catch (error) {
    els.cookieMessage.textContent = error.message;
  }
});

els.refreshHistory.addEventListener("click", loadHistory);

async function boot() {
  try {
    const response = await fetch("/health");
    const health = await response.json();
    await refreshCookieStatus();
    const cookieState = health.cookies_file
      ? "cookies 文件已配置"
      : health.cookies_from_browser
        ? `浏览器 cookies: ${health.cookies_from_browser}`
        : "未配置 cookies";
    els.serviceState.textContent = `服务正常 · 默认 ${health.mock_mode ? "Mock" : "真实"} 模式 · ${cookieState}`;
    await loadHistory();
  } catch {
    els.serviceState.textContent = "本地服务未连接";
  }
}

boot();
