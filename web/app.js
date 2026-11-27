/**
 * VideoInsight Agent 前端交互逻辑。
 *
 * 所有数据均来自后端真实接口，界面中不保留任何假数据：
 *   /uploads/*          分片上传、续传与提交分析
 *   POST /analyze        链接分析
 *   GET  /tasks/{id}     任务轮询
 *   GET  /tasks          历史任务
 *   DELETE /tasks/{id}   删除任务
 *   GET  /tasks/{id}/media|video  媒体预览与下载
 *   POST /tasks/{id}/chat        AI Agent 对话
 *   /cookies/*、/health          Cookies 授权与服务状态
 */
(function () {
  "use strict";

  /* ======================================================================
   * 1. 常量
   * ==================================================================== */

  const POLL_INTERVAL_MS = 2000;
  const QR_POLL_INTERVAL_MS = 3000;
  const QR_POLL_MAX_ATTEMPTS = 100; // 约 5 分钟
  const QR_POLL_MAX_ERRORS = 5;
  const DEFAULT_TIMEOUT_MS = 20000;
  const CAPTURE_TIMEOUT_MS = 30000;

  const STATUS_TEXT = {
    queued: "排队中",
    running: "处理中",
    succeeded: "已完成",
    failed: "失败",
  };
  const STATUS_BADGE = {
    queued: "badge-accent",
    running: "badge-accent",
    succeeded: "badge-success",
    failed: "badge-danger",
  };
  /* 各状态的目标进度（动画最终落点） */
  const STATUS_PROGRESS = {
    queued: 12,
    running: 88,
    succeeded: 100,
    failed: 100,
  };
  /* 每次轮询在目标进度内平滑爬升的幅度上限 */
  const PROGRESS_STEP_MAX = 8;
  const PROGRESS_STEP_MIN = 4;
  /* 后端实时进度阶段 → 中文标签（真实模式下由 /tasks/{id} 下发 progress/stage） */
  const STAGE_TEXT = {
    downloading: "下载中",
    transcribing: "转写中",
    analyzing: "AI 分析中",
    done: "处理完成",
  };
  const MODE_TEXT = {
    summary: "AI 总结",
    transcript: "逐字稿",
    interview: "面经提取",
  };

  const VIDEO_EXT_RE = /\.(mp4|webm|m4v|mov|ogv|mkv|avi|flv|ts)$/i;
  const AUDIO_EXT_RE = /\.(mp3|m4a|wav|aac|ogg|flac|opus|oga|weba)$/i;

  /* ======================================================================
   * 2. DOM 引用
   * ==================================================================== */

  const $ = (selector) => document.querySelector(selector);

  const els = {
    // 服务状态
    statusDot: $("#statusDot"),
    serviceState: $("#serviceState"),

    // 上传分析
    uploadForm: $("#uploadForm"),
    uploadSubmit: $("#uploadSubmit"),
    dropZone: $("#dropZone"),
    mediaFile: $("#mediaFile"),
    fileName: $("#fileName"),
    uploadMode: $("#uploadMode"),
    uploadReal: $("#uploadReal"),
    uploadRealRow: $("#uploadRealRow"),
    uploadProgress: $("#uploadProgress"),
    uploadProgressBar: $("#uploadProgressBar"),
    uploadProgressText: $("#uploadProgressText"),
    uploadPause: $("#uploadPause"),
    uploadCancel: $("#uploadCancel"),
    uploadResumeHint: $("#uploadResumeHint"),

    // 链接分析
    linkForm: $("#linkForm"),
    linkSubmit: $("#linkSubmit"),
    videoUrl: $("#videoUrl"),
    linkMode: $("#linkMode"),
    linkReal: $("#linkReal"),
    linkRealRow: $("#linkRealRow"),
    linkPreview: $("#linkPreview"),
    linkPreviewBadge: $("#linkPreviewBadge"),
    linkPreviewUrl: $("#linkPreviewUrl"),
    linkLabel: $("#linkLabel"),
    linkPreviewNote: $("#linkPreviewNote"),

    // 任务状态
    taskStatusBadge: $("#taskStatusBadge"),
    taskId: $("#taskId"),
    taskSource: $("#taskSource"),
    taskMessage: $("#taskMessage"),
    progressBar: $("#progressBar"),
    progressPercent: $("#progressPercent"),

    // 分析计时统计
    timingStats: $("#timingStats"),
    timingDuration: $("#timingDuration"),
    timingTranscribe: $("#timingTranscribe"),
    timingTotal: $("#timingTotal"),
    timingSpeed: $("#timingSpeed"),

    // 结果区
    summaryOutput: $("#summaryOutput"),
    transcriptToolbar: $("#transcriptToolbar"),
    transcriptOutput: $("#transcriptOutput"),
    copyTranscriptBtn: $("#copyTranscriptBtn"),
    transcriptCopyStatus: $("#transcriptCopyStatus"),
    commentsOutput: $("#commentsOutput"),
    rawOutput: $("#rawOutput"),
    copyPanelBtn: $("#copyPanelBtn"),
    taskUsedBadge: $("#taskUsedBadge"),
    copyActionMessage: $("#copyActionMessage"),
    expandPanelBtn: $("#expandPanelBtn"),

    // 媒体
    mediaPanel: $("#mediaPanel"),
    mediaVideo: $("#mediaVideo"),
    mediaAudio: $("#mediaAudio"),
    mediaDownloadMain: $("#mediaDownloadMain"),
    mediaDownloadAudio: $("#mediaDownloadAudio"),
    mediaHint: $("#mediaHint"),

    // 历史任务
    historyList: $("#historyList"),
    historyMessage: $("#historyMessage"),
    refreshHistory: $("#refreshHistory"),

    // Cookies
    cookieState: $("#cookieState"),
    clearCookieLogin: $("#clearCookieLogin"),
    sidebarCookieStatus: $("#sidebarCookieStatus"),
    cookieText: $("#cookieText"),
    cookieFile: $("#cookieFile"),
    cookieMessage: $("#cookieMessage"),
    cookieAuthHint: $("#cookieAuthHint"),
    cookieAdminControls: $("#cookieAdminControls"),
    cookieReadOnly: $("#cookieReadOnly"),
    refreshSharedCookies: $("#refreshSharedCookies"),
    cookieImport: $("#cookieImport"),
    cookieImportSummary: $("#cookieImportSummary"),
    cookieTextActions: $("#cookieTextActions"),
    desktopCookieActions: $("#desktopCookieActions"),
    qrImage: $("#qrImage"),
    qrMessage: $("#qrMessage"),
    qrView: $("#qrView"),
    qrStage: $("#qrStage"),
    qrPlaceholder: $("#qrPlaceholder"),
    qrExpiry: $("#qrExpiry"),
    saveQrImage: $("#saveQrImage"),
    qrMobileHint: $("#qrMobileHint"),
    cancelQrLogin: $("#cancelQrLogin"),
    saveCookieText: $("#saveCookieText"),
    verifyCookies: $("#verifyCookies"),
    uploadCookieFile: $("#uploadCookieFile"),
    startQrLogin: $("#startQrLogin"),
    openCookieBrowser: $("#openCookieBrowser"),
    checkCookieBrowser: $("#checkCookieBrowser"),
    captureCookieBrowser: $("#captureCookieBrowser"),

    // AI 对话
    agentContext: $("#agentContext"),
    chatMessages: $("#chatMessages"),
    promptChips: $("#promptChips"),
    chatInput: $("#chatInput"),
    sendChatBtn: $("#sendChatBtn"),
    newChatBtn: $("#newChatBtn"),

    // 模型设置
    modelState: $("#modelState"),
    llmProvider: $("#llmProvider"),
    llmBaseUrl: $("#llmBaseUrl"),
    llmProtocol: $("#llmProtocol"),
    testModelConfig: $("#testModelConfig"),
    llmModel: $("#llmModel"),
    llmApiKey: $("#llmApiKey"),
    saveModelConfig: $("#saveModelConfig"),
    clearModelConfig: $("#clearModelConfig"),
    modelMessage: $("#modelMessage"),

    // 访问令牌（仅在服务端配置了 VIDEOINSIGHT_AUTH_TOKEN 时需要）
    authSection: $("#auth"),
    authState: $("#authState"),
    authToken: $("#authToken"),
    saveAuthToken: $("#saveAuthToken"),
    clearAuthToken: $("#clearAuthToken"),
    authMessage: $("#authMessage"),

    // 主题切换
    themeToggle: $("#themeToggle"),
    themeToggleIcon: $("#themeToggleIcon"),
    themeToggleText: $("#themeToggleText"),
  };

  // 初始 AI 对话欢迎语（用于「新建干净对话」时回退）
  const WELCOME_HTML = els.chatMessages.innerHTML;

  /* ======================================================================
   * 3. 运行时状态
   * ==================================================================== */

  const state = {
    currentTaskId: null, // 当前展示/对话所依据的任务
    currentTask: null,
    pollTimer: null,
    watchVersion: 0,
    streamController: null,
    chatController: null,
    renderedTaskId: null,
    mediaKey: null,
    qrSessionId: null,
    qrTimer: null,
    qrAttempts: 0,
    qrFailures: 0,
    qrPolling: false,
    qrStartedAt: 0,
    pendingCookieRetry: null,
    cookieAuthorization: null,
    clearingCookies: false,
    accountIsAdmin: false,
    accountId: null, // 当前账号身份（account:<id> / admin:<name>），未登录为 null
    parsedLink: null, // 最新的 /parse-link 解析结果（含干净 url 与建议标签）
    transcriptText: "",
    summaryText: "", // AI 总结原始 Markdown，供「复制」按钮使用
    chatSessions: new Map(),
    usedTasks: new Map(),
    historySignature: null,
  };

  /* ======================================================================
   * 4. 通用工具
   * ==================================================================== */

  const ACTIVE_TASK_KEY = "videoinsight_active_task";
  const CHAT_CACHE_KEY = "videoinsight_chats_v1";
  let accountLoginEnabled = false;
  let serverModelConfig = null;
  let queueView = null;
  let uploadClient = null;
  let identityResolved = false;
  let accountStateEpoch = 0;
  let legacyConfigCleared = false;
  const AUTH_TOKEN_KEY = "videoinsight_auth_token";

  /** 读取本地保存的访问令牌；服务端未启用鉴权时它一直是空串。 */
  function getAuthToken() {
    try {
      return localStorage.getItem(AUTH_TOKEN_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function handleAccountError(response, data) {
    if (response.status === 409 && data?.code === "account_changed") {
      clearStaleAccountState();
      window.location.reload();
    } else if (response.status === 401 && accountLoginEnabled) {
      clearStaleAccountState();
      window.location.replace("/login");
    }
  }

  /**
   * 把访问令牌换成 HttpOnly Cookie。
   *
   * 必须做这一步：媒体是通过 `<video src>` / `<a href>` 由浏览器原生发起的，
   * 这些请求带不了 Authorization 头，只有 Cookie 能通过鉴权。
   */
  async function syncAuthSession(token) {
    const headers = new Headers({ "Content-Type": "application/json" });
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    const response = await fetch("/auth/session", {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ token: token }),
    });
    return response.ok;
  }

  /**
   * 发起 JSON 请求，统一处理超时与错误信息。
   * 自动附带 X-Client-Id 头，实现任务归属隔离。
   * @param {string} url 请求地址
   * @param {RequestInit} [options] fetch 选项
   * @param {number} [timeoutMs] 超时毫秒数
   * @returns {Promise<any>} 响应体（非 2xx 时抛出带后端 detail 的 Error）
   */
  async function fetchJson(url, options = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    const externalSignal = options.signal;
    const abort = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener("abort", abort, { once: true });
    const headers = new Headers(options.headers || {});
    headers.set("X-Client-Id", getClientId());
    if (state.accountId && url !== "/auth/me") {
      headers.set("X-VideoInsight-Account-Id", encodeURIComponent(state.accountId));
    }
    const token = getAuthToken();
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    try {
      const response = await fetch(url, Object.assign({}, options, { headers: headers, signal: controller.signal }));
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (error) {
          data = { detail: text };
        }
      }
      if (!response.ok) {
        handleAccountError(response, data);
        const error = new Error(extractError(data, response));
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error && error.name === "AbortError" && !externalSignal?.aborted) {
        throw new Error("请求超时，请确认服务仍在运行后重试。");
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
      externalSignal?.removeEventListener("abort", abort);
    }
  }

  function rememberTask(taskId) {
    try {
      if (taskId) {
        localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify({ clientId: getClientId(), taskId }));
      } else {
        localStorage.removeItem(ACTIVE_TASK_KEY);
      }
    } catch (error) { /* Storage may be disabled. */ }
  }

  function chatStorageKey() {
    return `${CHAT_CACHE_KEY}:${getClientId()}`;
  }

  function loadChatMessages(taskId) {
    if (state.chatSessions.has(taskId)) return state.chatSessions.get(taskId);
    let messages = [];
    try {
      const cache = JSON.parse(localStorage.getItem(chatStorageKey()) || "{}");
      const saved = cache[taskId];
      if (Array.isArray(saved)) {
        messages = saved.filter((m) => m && ["user", "assistant"].includes(m.role)
          && typeof m.content === "string" && m.content.length <= 20000)
          .slice(-40).map((m) => ({
            role: m.role, content: m.content, complete: m.complete === true,
          }));
      }
    } catch (error) { /* Invalid or unavailable storage starts an empty chat. */ }
    state.chatSessions.set(taskId, messages);
    return messages;
  }

  function saveChatMessages(taskId, messages) {
    const recent = messages.slice(-40);
    state.chatSessions.set(taskId, recent);
    try {
      let cache;
      try { cache = JSON.parse(localStorage.getItem(chatStorageKey()) || "{}"); } catch { cache = {}; }
      if (!cache || typeof cache !== "object" || Array.isArray(cache)) cache = {};
      delete cache[taskId];
      cache[taskId] = recent;
      const entries = Object.entries(cache).slice(-20);
      localStorage.setItem(chatStorageKey(), JSON.stringify(Object.fromEntries(entries)));
    } catch (error) { /* Keep the in-memory chat when storage is full or disabled. */ }
  }

  function restoreChatMessages(taskId) {
    els.chatMessages.innerHTML = WELCOME_HTML;
    for (const message of loadChatMessages(taskId)) {
      const interrupted = message.role === "assistant" && !message.complete;
      appendBubble(message.role === "assistant" ? "agent" : "user",
        interrupted ? `${message.content}\n\n生成已中断。` : message.content,
        { error: interrupted });
    }
  }

  function chatHistory(messages) {
    const history = [];
    let size = 0;
    for (let i = messages.length - 2; i >= 0; i -= 2) {
      const pair = messages.slice(i, i + 2);
      if (pair[0].role !== "user" || pair[1].role !== "assistant" || !pair[1].complete) continue;
      const length = pair[0].content.length + pair[1].content.length;
      if (size + length > 60000) break;
      size += length;
      history.unshift(...pair.map(({ role, content }) => ({ role, content })));
    }
    return history;
  }

  async function fetchEvents(url, options, onEvent, controller, idleMs = 45000) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Client-Id", getClientId());
    if (state.accountId && url !== "/auth/me") {
      headers.set("X-VideoInsight-Account-Id", encodeURIComponent(state.accountId));
    }
    headers.set("Accept", "text/event-stream");
    const token = getAuthToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let timer;
    let reader;
    const refreshTimeout = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => controller.abort(), idleMs);
    };
    refreshTimeout();
    try {
      const response = await fetch(url, Object.assign({}, options, { headers, signal: controller.signal }));
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const error = new Error(extractError(data, response));
        error.status = response.status;
        handleAccountError(response, data);
        throw error;
      }
      if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
        throw new Error("实时连接不可用");
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        refreshTimeout();
        buffer += decoder.decode(value, { stream: !done });
        buffer = buffer.replace(/\r\n/g, "\n");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          let event = "message";
          const data = [];
          frame.split("\n").forEach((line) => {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
          });
          if (!data.length) continue;
          const payload = JSON.parse(data.join("\n"));
          if (event === "error") {
            const error = new Error(payload.detail || "实时连接中断");
            error.status = payload.status;
            throw error;
          }
          if (onEvent(event, payload) === true) return;
        }
        if (done) throw new Error("实时连接已断开");
      }
    } finally {
      window.clearTimeout(timer);
      if (reader) await reader.cancel().catch(() => {});
    }
  }

  /** 从 FastAPI 错误响应中取出可读信息。 */
  function extractError(data, response) {
    if (data && data.detail) {
      return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    }
    return `请求失败（HTTP ${response.status}）`;
  }

  /** HTML 转义，防止后端内容被当作标签解析。 */
  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** 行内 Markdown：加粗 / 斜体 / 行内代码。 */
  function renderInline(text) {
    return String(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
  }

  /**
   * 极简 Markdown 渲染（先转义再转标签，仅支持标题/列表/引用/分隔线）。
   * 用于 AI 总结与 Agent 回答，不依赖任何第三方库。
   */
  function renderMarkdown(text) {
    const lines = escapeHtml(String(text || "")).split("\n");
    const html = [];
    let listTag = "";

    const closeList = () => {
      if (listTag) {
        html.push(`</${listTag}>`);
        listTag = "";
      }
    };

    lines.forEach((rawLine) => {
      const line = rawLine.trim();
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        closeList();
        const level = Math.min(heading[1].length + 1, 4);
        html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        return;
      }
      const quote = line.match(/^&gt;\s?(.*)$/);
      if (quote) {
        closeList();
        html.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
        return;
      }
      if (/^([-*_])\1{2,}$/.test(line)) {
        closeList();
        html.push("<hr />");
        return;
      }
      const bullet = line.match(/^[-*]\s+(.*)$/);
      const ordered = line.match(/^\d+[.、)]\s+(.*)$/);
      if (bullet || ordered) {
        const tag = bullet ? "ul" : "ol";
        if (listTag !== tag) {
          closeList();
          html.push(`<${tag}>`);
          listTag = tag;
        }
        html.push(`<li>${renderInline((bullet || ordered)[1])}</li>`);
        return;
      }
      closeList();
      if (line) {
        html.push(`<p>${renderInline(line)}</p>`);
      }
    });

    closeList();
    return html.join("");
  }

  /** 格式化后端返回的时间字符串。 */
  function formatTime(value) {
    if (!value) {
      return "-";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    const pad = (num) => String(num).padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  /** 把秒数格式化为「分:秒」或「时:分:秒」。 */
  function formatDuration(seconds) {
    const num = Number(seconds);
    if (!seconds || Number.isNaN(num) || num <= 0) {
      return "-";
    }
    const total = Math.floor(num);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
  }

  /** 截断长文本用于列表展示。 */
  function truncate(text, max = 42) {
    const value = String(text || "");
    return value.length > max ? `${value.slice(0, max)}…` : value;
  }

  function isCookieFailure(message) {
    const value = String(message || "").toLowerCase();
    return value.includes("cookies 已失效") || value.includes("需要登录态 cookies");
  }

  function analysisModelPayload(mode, useMock) {
    if (!identityResolved) return null;
    if (mode === "transcript" || useMock) {
      return {};
    }
    const config = loadModelConfig();
    if (!config) {
      if (serverModelConfig) return {};
      setTaskStatus("failed", "AI 总结或观点提取需要先配置 API Key；逐字稿提取无需配置。");
      queueView?.notify("请先配置模型 API Key，或选择逐字稿提取。", true);
      setModelMessage("请先保存 API Key，再重新提交 AI 分析。逐字稿提取不需要 API Key。", "error");
      scrollToSettings();
      return null;
    }
    return {
      llm_provider: config.provider,
      llm_base_url: config.baseUrl,
      llm_protocol: config.protocol || "chat_completions",
      llm_model: config.model,
      llm_api_key: config.apiKey,
    };
  }

  function syncAnalysisModeUi() {
    const uploadTranscript = els.uploadMode.value === "transcript";
    const linkTranscript = els.linkMode.value === "transcript";
    els.uploadRealRow.hidden = uploadTranscript;
    els.linkRealRow.hidden = linkTranscript;
    if (!uploadClient?.running) els.uploadSubmit.textContent = uploadClient?.session ? "继续上传并入队" : "上传并入队";
    els.linkSubmit.textContent = "加入处理队列";
  }

  /* ======================================================================
   * 5. 任务状态与结果渲染
   * ==================================================================== */

  /**
   * 更新进度条与任务状态徽章。
   * @param {string} status queued / running / succeeded / failed
   * @param {string} message 展示给用户的说明
   * @param {number} [progress] 直接指定进度百分比（缺省时取该状态的目标值）
   */
  function setTaskStatus(status, message, progress) {
    const target = STATUS_PROGRESS[status] || 0;
    const percent = progress == null ? target : Math.max(0, Math.min(100, progress));
    const badgeClass = STATUS_BADGE[status] || "badge-muted";
    const label = STATUS_TEXT[status] || "待命";

    els.taskStatusBadge.className = `badge ${badgeClass}`;
    els.taskStatusBadge.textContent = label;
    els.progressBar.classList.toggle("is-done", status === "succeeded");
    els.progressBar.classList.toggle("is-failed", status === "failed");
    els.progressBar.style.width = `${percent}%`;
    els.progressPercent.textContent = `${Math.round(percent)}%`;
    els.taskMessage.textContent = message;
    els.taskMessage.classList.toggle("is-error", status === "failed");
  }

  /**
   * 让进度条在当前值与目标值之间平滑递增一段。
   * 每次调用最多增加 PROGRESS_STEP_MAX，且不会超过目标值。
   * @param {string} status 当前状态（决定目标值）
   * @returns {number} 更新后的进度
   */
  function easeProgress(status) {
    const target = STATUS_PROGRESS[status] || 0;
    const current = parseFloat(els.progressBar.style.width) || 0;
    if (current >= target) {
      return current;
    }
    const step = PROGRESS_STEP_MIN + Math.random() * (PROGRESS_STEP_MAX - PROGRESS_STEP_MIN);
    const next = Math.min(target, current + step);
    els.progressBar.style.width = `${next}%`;
    els.progressPercent.textContent = `${Math.round(next)}%`;
    return next;
  }

  /**
   * 用后端下发的真实进度渲染进度条（阶段 + 百分比 + 预计剩余）。
   * 仅当 task.progress 非空时调用；否则回退到 easeProgress 装饰性动画。
   * @param {object} task 后端 TaskRecord（含 progress / stage / eta_seconds）
   */
  function setRealProgress(task) {
    const percent = Math.max(0, Math.min(100, Number(task.progress) || 0));
    els.progressBar.style.width = `${percent}%`;
    els.progressPercent.textContent = `${Math.round(percent)}%`;
    els.progressBar.classList.remove("is-done", "is-failed");

    const stage = task.stage || "transcribing";
    let msg = STAGE_TEXT[stage] || "处理中";
    const details = [];
    if (stage === "transcribing" && task.transcribe_total) {
      details.push(`${task.transcribe_done || 0}/${task.transcribe_total} 片`);
    }
    if (task.media_duration_seconds) {
      details.push(`视频 ${formatDuration(task.media_duration_seconds)}`);
    }
    if (task.eta_seconds != null && Number.isFinite(task.eta_seconds)) {
      details.push(
        Number(task.eta_seconds) > 1
          ? `预计剩余 ${formatDuration(task.eta_seconds)}`
          : "即将完成",
      );
    }
    if (details.length) {
      msg += ` · ${details.join(" · ")}`;
    }
    els.taskMessage.textContent = msg;
    els.taskMessage.classList.remove("is-error");
  }

  /** 把带时间戳的逐字稿渲染成可扫描的分段视图，同时保留原文供复制。 */
  function syncTranscriptToolbar() {
    const activeTab = document.querySelector(".tab-btn.active");
    els.transcriptToolbar.hidden = !state.transcriptText || !activeTab || activeTab.dataset.target !== "transcriptOutput";
  }

  function renderTranscript(transcript) {
    const text = String(transcript || "").trim();
    state.transcriptText = text;
    els.transcriptCopyStatus.textContent = "";
    if (!text) {
      syncTranscriptToolbar();
      els.transcriptOutput.textContent = "无逐字稿";
      return;
    }

    syncTranscriptToolbar();
    els.transcriptOutput.innerHTML = "";
    const fragment = document.createDocumentFragment();
    text.split(/\r?\n/).forEach((line) => {
      const match = line.match(/^\[([0-9:.]+)-([0-9:.]+)\]\s*(.*)$/);
      const item = document.createElement("article");
      item.className = "transcript-segment";
      if (match) {
        const time = document.createElement("button");
        time.type = "button";
        time.className = "transcript-time";
        time.textContent = `${formatTranscriptTime(match[1])}–${formatTranscriptTime(match[2])}`;
        time.title = "跳转到此时间";
        time.dataset.seconds = String(parseTranscriptTime(match[1]));
        time.addEventListener("click", () => seekTranscript(parseTranscriptTime(match[1])));

        const body = document.createElement("p");
        body.className = "transcript-text";
        body.textContent = match[3];
        item.append(time, body);
      } else if (line.trim()) {
        item.classList.add("transcript-segment-plain");
        item.textContent = line;
      }
      if (item.textContent || item.children.length) {
        fragment.appendChild(item);
      }
    });
    els.transcriptOutput.appendChild(fragment);
  }

  function parseTranscriptTime(value) {
    const parts = String(value).split(":").map(Number);
    if (parts.some((part) => !Number.isFinite(part))) return 0;
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return parts[0] || 0;
  }

  function formatTranscriptTime(value) {
    const seconds = Math.max(0, Math.floor(parseTranscriptTime(value)));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}` : `${minutes}:${String(remainder).padStart(2, "0")}`;
  }

  function seekTranscript(seconds) {
    const media = els.mediaVideo.hidden ? els.mediaAudio : els.mediaVideo;
    if (!media.hidden && Number.isFinite(seconds) && media.duration) {
      media.currentTime = Math.min(seconds, media.duration);
      media.play().catch(() => {});
    }
  }

  /** 统一剪贴板写入：优先 async API，失败回退到隐藏 textarea。 */
  async function copyToClipboard(text) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (error) {
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.setAttribute("readonly", "");
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      try {
        helper.select();
        if (!document.execCommand("copy")) throw new Error("浏览器未允许写入剪贴板。");
      } finally {
        helper.remove();
      }
    }
  }

  function taskUsedAt(task) {
    return state.usedTasks.get(task.task_id) || task.used_at || null;
  }

  function renderUsedBadge() {
    const usedAt = state.currentTask && taskUsedAt(state.currentTask);
    els.taskUsedBadge.hidden = !usedAt;
    els.taskUsedBadge.title = usedAt ? `首次复制：${formatTime(usedAt)}` : "";
  }

  function copyFeedback(taskId, message, error = false) {
    if (state.currentTaskId !== taskId) return;
    els.copyActionMessage.textContent = message;
    els.copyActionMessage.hidden = !message;
    els.copyActionMessage.classList.toggle("is-error", error);
  }

  async function copyTaskText(text) {
    const task = state.currentTask;
    if (!task || !String(text || "").trim()) return;
    const taskId = task.task_id;
    try {
      await copyToClipboard(String(text));
    } catch (error) {
      copyFeedback(taskId, `复制失败：${error.message} 请检查浏览器剪贴板权限后重试。`, true);
      return;
    }
    try {
      const knownUsedAt = taskUsedAt(task);
      const usedAt = knownUsedAt || (await fetchJson(`/tasks/${taskId}/used`, { method: "POST" }, 10000)).used_at;
      state.usedTasks.set(taskId, usedAt);
      if (state.currentTaskId === taskId) {
        state.currentTask.used_at = usedAt;
        renderUsedBadge();
      }
      queueView?.onTaskUsed(taskId, usedAt);
      copyFeedback(taskId, "已复制");
    } catch (error) {
      copyFeedback(taskId, `文本已复制，但“已使用”标记保存失败：${error.message} 请再次点击复制重试。`, true);
    }
  }

  async function copyTranscript() {
    if (!state.transcriptText) return;
    await copyTaskText(state.transcriptText);
  }

  /* ---------- 结果区视图操作：复制 / 展开全文 ---------- */

  /* 每个面板复制时使用的原文（优先未渲染的纯文本，其次面板可见文本） */
  const PANEL_TEXT = {
    summaryOutput: () => state.summaryText || els.summaryOutput.textContent,
    transcriptOutput: () => state.transcriptText || els.transcriptOutput.textContent,
    commentsOutput: () => els.commentsOutput.textContent,
    rawOutput: () => els.rawOutput.textContent,
  };

  function activePanelId() {
    const activeTab = document.querySelector(".tab-btn.active");
    return activeTab ? activeTab.dataset.target : "summaryOutput";
  }

  /** 让「展开全文」按钮的文案与当前面板状态保持一致。 */
  function syncResultActions() {
    if (!els.expandPanelBtn) return;
    const panel = document.getElementById(activePanelId());
    const expanded = Boolean(panel && panel.classList.contains("is-expanded"));
    els.expandPanelBtn.textContent = expanded ? "收起" : "展开全文";
    els.expandPanelBtn.classList.toggle("is-active", expanded);
    els.expandPanelBtn.setAttribute("aria-pressed", String(expanded));
  }

  async function copyActivePanel() {
    const result = state.currentTask?.result;
    const available = {
      summaryOutput: Boolean(state.summaryText),
      transcriptOutput: Boolean(state.transcriptText),
      commentsOutput: Boolean(result?.comments?.length),
      rawOutput: Boolean(result),
    };
    if (!available[activePanelId()]) return;
    const text = (PANEL_TEXT[activePanelId()] || (() => ""))();
    if (!String(text || "").trim()) return;
    await copyTaskText(text);
  }

  function toggleActivePanel() {
    const panel = document.getElementById(activePanelId());
    if (!panel) return;
    panel.classList.toggle("is-expanded");
    syncResultActions();
  }

  function bindResultActions() {
    if (els.copyPanelBtn) {
      els.copyPanelBtn.addEventListener("click", copyActivePanel);
    }
    if (els.expandPanelBtn) {
      els.expandPanelBtn.addEventListener("click", toggleActivePanel);
    }
    syncResultActions();
  }

  /** 把后端返回的 TaskRecord 渲染到页面上。 */
  function applyTask(task) {
    task = { ...task, used_at: taskUsedAt(task) };
    const previous = state.currentTask;
    const switched = state.renderedTaskId !== task.task_id;
    if (switched) {
      if (state.chatController) {
        state.chatController.abort();
        state.chatController = null;
        setChatBusy(false);
      }
      els.chatMessages.querySelectorAll(".is-thinking").forEach((node) => {
        node.classList.remove("is-thinking");
        node.textContent = "已停止生成。";
      });
      if (state.chatSessions.size > 20) state.chatSessions.delete(state.chatSessions.keys().next().value);
      restoreChatMessages(task.task_id);
      els.chatInput.value = "";
      state.transcriptText = "";
      state.summaryText = "";
      renderTranscript("");
      els.commentsOutput.textContent = "暂无观众评论";
      els.copyActionMessage.hidden = true;
      els.copyActionMessage.textContent = "";
      els.transcriptCopyStatus.textContent = "";
    }
    state.currentTask = task;
    state.currentTaskId = task.task_id;
    state.renderedTaskId = task.task_id;
    renderUsedBadge();
    queueView?.onTask(task);
    rememberTask(task.task_id);

    els.taskId.textContent = task.task_id;
    els.taskSource.textContent = task.request && task.request.label ? task.request.label : (task.request ? task.request.url : "-");
    if (activePanelId() === "rawOutput") els.rawOutput.textContent = JSON.stringify(task, null, 2);

    const mediaKey = `${task.task_id}:${task.status === "succeeded"}:${task.result?.media?.audio_path || ""}`;
    if (mediaKey !== state.mediaKey) {
      renderMedia(task);
      state.mediaKey = mediaKey;
    }

    if (task.status === "queued") {
      // 保留当前进度宽度，避免打断轮询期间的平滑递增
      setTaskStatus("queued", "任务已提交，等待处理。", parseFloat(els.progressBar.style.width) || 0);
      if (!task.result?.output) els.summaryOutput.textContent = "任务排队中，完成后将显示 AI 总结。";
    } else if (task.status === "running") {
      const message = task.result?.transcript ? "逐字稿已就绪，AI 分析正在生成。" : "正在下载、转写并分析，较长视频可能需要一两分钟。";
      setTaskStatus("running", message, parseFloat(els.progressBar.style.width) || 0);
      if (!task.result?.output) {
        els.summaryOutput.textContent = task.result?.transcript ? "逐字稿已就绪，正在生成 AI 分析…" : "正在转写/分析中，请稍候…";
      }
    } else if (task.status === "failed") {
      setTaskStatus("failed", task.error || "任务失败。");
      if (!task.result?.output) els.summaryOutput.textContent = task.error || "任务失败。";
      if (task.request && isCookieFailure(task.error)) {
        state.pendingCookieRetry = {
          url: String(task.request.url || ""),
          mode: task.request.mode || "summary",
          useMock: Boolean(task.request.use_mock),
          label: task.request.label,
          ...retryAsrChoice(task),
        };
        setCookieMessage(
          ["desktop", "remote"].includes(state.cookieAuthorization?.mode)
            ? "该链接需要更新登录态，登录成功后会自动重试。"
            : "该链接需要更新登录态，导入对应平台 Cookies 后会自动重试。",
          "error",
        );
        renderCookieAuthorization(state.cookieAuthorization);
      }
    } else if (task.status === "succeeded") {
      setTaskStatus("succeeded", "分析完成。");
    } else {
      els.commentsOutput.textContent = "暂无观众评论";
    }

    if (task.result) {
      const output = String(task.result.output || "");
      if (output && (switched || output !== state.summaryText)) {
        state.summaryText = output;
        els.summaryOutput.innerHTML = renderMarkdown(output);
      }
      const transcript = String(task.result.transcript || "");
      if (transcript !== state.transcriptText) renderTranscript(transcript);
      if (switched || previous?.status !== task.status || previous?.result?.comments !== task.result.comments) {
        renderComments(task);
      }
    }
    updateAgentContext();
    renderTiming(task);
    highlightHistory(task.task_id);
  }

  /** 渲染分析计时统计：原视频时长 / 转写用时 / 总用时 / 加速倍数。 */
  function renderTiming(task) {
    const diag = (task.result && task.result.diagnostics) || null;
    if (!diag || task.status !== "succeeded") {
      els.timingStats.hidden = true;
      return;
    }
    els.timingStats.hidden = false;
    els.timingDuration.textContent = formatDuration(diag.media_duration_seconds);
    els.timingTranscribe.textContent = formatDuration(diag.transcribe_seconds);
    els.timingTotal.textContent = formatDuration(diag.total_seconds);
    els.timingSpeed.textContent = diag.asr_speed_x ? `${diag.asr_speed_x}× 实时` : "—";
  }

  /** 渲染媒体播放器与下载链接（仅在任务成功后展示）。 */
  function renderMedia(task) {
    for (const [id, format] of [["subtitleSrt", "srt"], ["subtitleVtt", "vtt"]]) {
      const link = document.getElementById(id);
      if (link) {
        link.hidden = !task.result?.segments?.length || task.result?.diagnostics?.asr_timestamps === false;
        link.href = `/tasks/${encodeURIComponent(task.task_id)}/subtitles?format=${format}`;
      }
    }
    els.mediaPanel.hidden = true;
    els.mediaVideo.hidden = true;
    els.mediaAudio.hidden = true;
    els.mediaVideo.removeAttribute("src");
    els.mediaAudio.removeAttribute("src");
    els.mediaVideo.load();
    els.mediaAudio.load();
    els.mediaDownloadMain.hidden = false;
    els.mediaDownloadAudio.hidden = true;
    els.mediaHint.hidden = true;
    delete els.mediaDownloadMain.dataset.onDemand;

    if (task.status !== "succeeded" || !task.result || !task.result.media) {
      return;
    }

    const audioPath = task.result.media.audio_path;
    if (!audioPath) {
      return;
    }

    const taskId = task.task_id;
    const filename = String(audioPath).split(/[\\/]/).pop() || "media";
    const isUpload = String(task.request.url).startsWith("upload://");

    if (VIDEO_EXT_RE.test(filename)) {
      els.mediaVideo.hidden = false;
      els.mediaVideo.src = `/tasks/${taskId}/media`;
      els.mediaVideo.load();
      els.mediaDownloadMain.href = `/tasks/${taskId}/media?download=1`;
      els.mediaDownloadMain.textContent = `下载视频（${filename}）`;
    } else if (AUDIO_EXT_RE.test(filename)) {
      els.mediaAudio.hidden = false;
      els.mediaAudio.src = `/tasks/${taskId}/media`;
      els.mediaAudio.load();
      if (isUpload) {
        // 上传任务没有源视频，只提供原始音频下载
        els.mediaDownloadMain.href = `/tasks/${taskId}/media?download=1`;
        els.mediaDownloadMain.textContent = `下载音频（${filename}）`;
      } else {
        els.mediaDownloadMain.href = `/tasks/${taskId}/video?download=1`;
        els.mediaDownloadMain.textContent = "下载源视频（mp4）";
        els.mediaDownloadMain.dataset.onDemand = "1";
        els.mediaDownloadAudio.href = `/tasks/${taskId}/media?download=1`;
        els.mediaDownloadAudio.textContent = `下载音频（${filename}）`;
        els.mediaDownloadAudio.hidden = false;
      }
    } else {
      // 扩展名未知：尝试音频播放，同时给出下载入口
      els.mediaAudio.hidden = false;
      els.mediaAudio.src = `/tasks/${taskId}/media`;
      els.mediaAudio.load();
      els.mediaDownloadMain.href = `/tasks/${taskId}/media?download=1`;
      els.mediaDownloadMain.textContent = `下载文件（${filename}）`;
    }

    els.mediaPanel.hidden = false;
  }

  const commentPreferences = {sort:"likes", scope:"roots"};

  /** 渲染观众评论（按后端归一化后的 comments 树）。无评论时显示原因/状态。 */
  function renderComments(task) {
    const box = els.commentsOutput;
    box.innerHTML = "";
    const result = task.result || {};
    const comments = result.comments;
    const commentJobId = result.diagnostics && result.diagnostics.comment_job_id;
    if (commentJobId) {
      const message = document.createElement("p");
      message.textContent = result.diagnostics.comments_source;
      const link = document.createElement("a");
      link.href = `/comments?job=${encodeURIComponent(commentJobId)}`;
      link.className = "btn btn-secondary";
      link.textContent = "查看评论、子评论与热评前 10";
      box.append(message, link);
      return;
    }
    if (!comments || !comments.length) {
      const reason = (result.diagnostics && result.diagnostics.comments_source) || "未配置 MediaCrawler 评论源";
      const empty = document.createElement("div");
      empty.className = "comments-empty";
      empty.textContent = reason;
      box.appendChild(empty);
      return;
    }
    const summary = document.createElement("div");
    summary.className = "comments-summary";
    const flat = CommentUtils.flatten(comments);
    const source = result.diagnostics?.comments_source || "";
    const limited = source.startsWith("抖音评论接口::");
    summary.textContent = `已获取 ${flat.length} 条 · 一级评论 ${comments.length} 条 · 子评论 ${flat.length-comments.length} 条`;
    box.appendChild(summary);
    if (limited) {
      const warning = document.createElement("p");
      warning.className = "comments-empty";
      warning.textContent = "旧版抖音热门样本，不含子评论；排序及前 10 仅针对这些样本，不代表完整评论区。";
      const link = document.createElement("a");
      link.href = `/comments?url=${encodeURIComponent(task.request?.url || "")}`;
      link.textContent = "前往评论中心";
      box.append(warning, link);
    }
    const toolbar = document.createElement("div");
    toolbar.className = "comments-filter-toolbar";
    const makeSelect = (name, key, options) => {
      const label = document.createElement("label");
      label.append(document.createTextNode(name));
      const select = document.createElement("select");
      select.setAttribute("aria-label", name);
      for (const [value, text] of options) {
        const option = document.createElement("option");
        option.value = value;option.textContent = text;select.append(option);
      }
      select.value = commentPreferences[key];
      select.onchange = () => {commentPreferences[key]=select.value;renderComments(task);};
      label.append(select);toolbar.append(label);
    };
    makeSelect("排序", "sort", [["likes","最热 · 点赞数"],["newest","最新 · 发布时间"],["top10","热评前 10"],["collected","采集顺序"]]);
    makeSelect("范围", "scope", [["roots","一级评论"],["all","全部评论"],["replies","仅子评论"]]);
    box.append(toolbar);
    const list = document.createElement("ul");
    list.className = "comment-list";
    const visible = CommentUtils.select(comments, commentPreferences.sort, commentPreferences.scope);
    visible.forEach((comment) => list.appendChild(buildCommentNode(comment, commentPreferences.scope==="roots")));
    if (!visible.length) {
      const empty = document.createElement("li");empty.className="comments-empty";empty.textContent="当前范围暂无已获取评论。";list.append(empty);
    }
    box.appendChild(list);
  }

  function buildCommentNode(comment, includeReplies = true) {
    const li = document.createElement("li");
    li.className = "comment-item";
    const head = document.createElement("div");
    head.className = "comment-head";
    const name = document.createElement("span");
    name.className = "comment-author";
    name.textContent = comment.author_nickname || "观众";
    const like = document.createElement("span");
    like.className = "comment-like";
    like.textContent = "👍 " + (comment.like_count || 0);
    head.appendChild(name);
    head.appendChild(like);
    const body = document.createElement("div");
    body.className = "comment-body";
    body.textContent = comment.content || "";
    li.appendChild(head);
    li.appendChild(body);
    const time = document.createElement("small");
    time.className = "comment-time";
    time.textContent = CommentUtils.formatTime(comment.create_time);
    if (comment.root_comment_id)time.textContent+=` · 回复 ${comment.parent_comment_id || comment.root_comment_id}`;
    li.append(time);
    if (includeReplies && comment.sub_comments && comment.sub_comments.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `回复 (${CommentUtils.flatten(comment.sub_comments).length})`;
      const sub = document.createElement("ul");
      sub.className = "comment-sublist";
      comment.sub_comments.forEach((subComment) => sub.appendChild(buildCommentNode(subComment)));
      details.append(summary,sub);li.append(details);
    }
    return li;
  }

  /* ======================================================================
   * 6. 任务提交与轮询
   * ==================================================================== */

  function stopPolling() {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.watchVersion += 1;
    if (state.streamController) state.streamController.abort();
    state.streamController = null;
  }

  /**
   * 新建干净对话：清空当前展示的会话视图（任务状态 / 进度条 / 结果 / AI 对话），
   * 停止轮询，但**不删除任何历史任务记录**。
   * 目的在于让用户直接在新的输入框里粘贴链接，而不必去历史任务里重新复制。
   */
  function resetCurrentSession() {
    stopPolling();
    if (state.chatController) state.chatController.abort();
    state.chatController = null;
    setChatBusy(false);
    rememberTask(null);
    state.currentTaskId = null;
    state.currentTask = null;
    state.renderedTaskId = null;
    state.mediaKey = null;
    state.pendingCookieRetry = null;
    renderUsedBadge();
    els.copyActionMessage.hidden = true;

    // 任务状态卡片回到待命
    els.taskStatusBadge.className = "badge badge-muted";
    els.taskStatusBadge.textContent = "待命";
    els.taskId.textContent = "—";
    els.taskSource.textContent = "—";
    els.taskMessage.classList.remove("is-error");
    els.taskMessage.textContent = "已新建干净对话，粘贴链接或上传文件开始分析。";
    els.progressBar.classList.remove("is-done", "is-failed");
    els.progressBar.style.width = "0%";
    els.progressPercent.textContent = "0%";

    // 计时统计
    els.timingStats.hidden = true;
    els.timingDuration.textContent = "—";
    els.timingTranscribe.textContent = "—";
    els.timingTotal.textContent = "—";
    els.timingSpeed.textContent = "—";

    // 结果区：回到默认，并切回「总结」Tab
    els.summaryOutput.innerHTML = "等待分析任务完成后显示结果...";
    els.transcriptOutput.textContent = "暂无逐字稿";
    els.transcriptToolbar.hidden = true;
    els.transcriptCopyStatus.textContent = "";
    state.transcriptText = "";
    state.summaryText = "";
    els.commentsOutput.textContent = "暂无观众评论";
    els.rawOutput.textContent = "{}";
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.target === "summaryOutput");
    });
    ["summaryOutput", "transcriptOutput", "commentsOutput", "rawOutput"].forEach((id) => {
      const panel = document.getElementById(id);
      if (panel) {
        panel.style.display = id === "summaryOutput" ? "block" : "none";
      }
    });
    syncResultActions();

    // 媒体区
    renderMedia({ status: "queued", result: null, request: { url: "" } });

    // AI 对话区：回到欢迎语
    els.chatMessages.innerHTML = WELCOME_HTML;
    updateAgentContext();

    // 清除历史列表高亮
    highlightHistory(null);
  }

  /** Prefer resumable events; fall back to non-overlapping, retryable polling. */
  function startPolling(taskId) {
    stopPolling();
    const version = state.watchVersion;
    state.currentTaskId = taskId;
    rememberTask(taskId);
    els.taskId.textContent = taskId;
    if (state.renderedTaskId !== taskId) {
      state.currentTask = null;
      els.progressBar.style.width = "0%";
      els.progressPercent.textContent = "0%";
      setTaskStatus("queued", "任务已提交，正在等待后端处理。");
    }
    let failures = 0;
    let nextStreamAt = 0;
    const current = () => state.watchVersion === version && state.currentTaskId === taskId;
    const accept = (task) => {
      if (!current()) return true;
      applyTask(task);
      if (task.status === "succeeded" || task.status === "failed") {
        stopPolling();
        loadHistory().catch(() => {});
        return true;
      }
      if (task.progress != null) setRealProgress(task);
      else easeProgress(task.status);
      return false;
    };

    const tick = async () => {
      if (!current()) return;
      if (Date.now() >= nextStreamAt) {
        const controller = new AbortController();
        state.streamController = controller;
        try {
          await fetchEvents(`/tasks/${taskId}/events`, {}, (event, data) => {
            if (!current()) return true;
            failures = 0;
            if (event === "snapshot" || event === "complete") return accept(data);
            if (!state.currentTask) return false;
            if (event === "progress") return accept(Object.assign({}, state.currentTask, data));
            if (event === "delta" && state.currentTask.result) {
              const result = Object.assign({}, state.currentTask.result, { output: state.currentTask.result.output + data.text });
              applyTask(Object.assign({}, state.currentTask, { result }));
            }
            return false;
          }, controller);
          return;
        } catch (error) {
          if (!current()) return;
          nextStreamAt = error.status === 404 ? Infinity : Date.now() + 30000;
        } finally {
          if (state.streamController === controller) state.streamController = null;
        }
      }
      try {
        const task = await fetchJson(`/tasks/${taskId}`, {}, 10000);
        if (accept(task)) return;
        failures = 0;
      } catch (error) {
        if (!current()) return;
        if ([401, 403, 404].includes(error.status)) {
          stopPolling();
          if (error.status === 404) rememberTask(null);
          els.taskMessage.textContent = error.status === 404 ? "任务不存在或已被删除。" : "登录或访问令牌已失效，请重新验证。";
          return;
        }
        failures += 1;
        els.taskMessage.textContent = "连接暂时中断，正在重试；后台任务不受影响。";
        els.taskMessage.classList.remove("is-error");
      }
      if (current()) state.pollTimer = window.setTimeout(tick, Math.min(POLL_INTERVAL_MS * (2 ** failures), 15000));
    };

    tick();
  }

  function setUploadBusy(busy) {
    els.uploadSubmit.disabled = busy || !identityResolved;
    els.mediaFile.disabled = busy;
    els.uploadMode.disabled = busy;
    els.uploadReal.disabled = busy;
  }

  function renderUploadProgress(progress) {
    const percent = progress.stage === "hashing" ? progress.hashPercent || 0
      : Math.min(100, (progress.uploadedBytes || 0) / (progress.totalBytes || 1) * 100);
    els.uploadProgress.hidden = progress.stage === "cancelled";
    els.uploadProgressBar.value = percent;
    const amount = `${((progress.uploadedBytes || 0) / 1048576).toFixed(1)} / ${((progress.totalBytes || 0) / 1048576).toFixed(1)} MB`;
    const messages = {
      hashing: `正在校验文件 ${Math.floor(percent)}%`,
      uploading: `已上传 ${Math.floor(percent)}% · ${amount}${progress.retrying ? " · 连接中断，正在重试" : ""}`,
      completing: "上传完成，正在校验并加入分析队列…",
      paused: `已暂停 · ${amount}，点击继续即可续传。`,
      done: "上传完成，已加入分析队列。",
      cancelled: "上传已取消。",
    };
    els.uploadProgressText.textContent = messages[progress.stage] || "正在准备上传…";
    els.uploadPause.hidden = !["hashing", "uploading"].includes(progress.stage);
    els.uploadCancel.hidden = ["done", "cancelled"].includes(progress.stage);
    els.uploadCancel.disabled = progress.stage === "completing";
    if (progress.stage === "hashing") els.uploadSubmit.textContent = "正在校验文件…";
    if (progress.stage === "uploading") els.uploadSubmit.textContent = `上传中 ${Math.floor(percent)}%`;
    if (progress.stage === "completing") els.uploadSubmit.textContent = "正在提交分析…";
  }

  function getUploadClient() {
    if (!uploadClient) uploadClient = new window.ResumableUpload.ResumableUploader({
      request: fetchJson,
      storageKey: `videoinsight_uploads_v1:${state.uploadOwner || getClientId()}`,
      onProgress: renderUploadProgress,
    });
    return uploadClient;
  }

  function renderUploadResumeHint() {
    if (!identityResolved) return;
    const pending = getUploadClient().pending();
    els.uploadResumeHint.hidden = pending.length === 0;
    if (pending.length) {
      els.uploadResumeHint.textContent = `有未完成的上传：${pending[0].filename}。重新选择原文件并点击上传即可续传；也可以取消此上传。`;
      els.uploadCancel.hidden = false;
    }
  }

  /** 分片上传文件，完成后接入原有分析队列。 */
  async function submitUpload(event) {
    event.preventDefault();
    if (!identityResolved) return;
    const file = els.mediaFile.files && els.mediaFile.files[0];
    if (!file) {
      setTaskStatus("failed", "请先选择一个音频或视频文件。");
      queueView?.notify("请先选择一个音频或视频文件。", true);
      return;
    }

    const mode = els.uploadMode.value;
    const useMock = mode === "transcript" ? false : !els.uploadReal.checked;
    const llmPayload = analysisModelPayload(mode, useMock);
    if (llmPayload === null) {
      return;
    }

    setUploadBusy(true);
    els.uploadResumeHint.hidden = true;
    try {
      const data = await getUploadClient().upload(file, { mode, use_mock: useMock, ...asrPayload(), ...llmPayload });
      if (!data) return;
      els.taskSource.textContent = file.name;
      await queueView.onSubmitted(data.task_id);
    } catch (error) {
      els.uploadProgress.hidden = false;
      els.uploadProgressText.textContent = `上传暂未完成：${error.message} 已确认的分片会保留，可点击继续或取消。`;
      els.uploadPause.hidden = true;
      els.uploadCancel.hidden = false;
      els.uploadCancel.disabled = false;
      queueView?.notify(`上传暂未完成：${error.message}`, true);
    } finally {
      setUploadBusy(false);
      syncAnalysisModeUi();
      renderUploadResumeHint();
    }
  }

  function retryAsrChoice(task) {
    const previous = task.request?.asr_provider || task.result?.diagnostics?.asr_provider;
    const provider = ["groq", "cloudflare", "siliconflow", "faster_whisper"].includes(previous) ? previous : "faster_whisper";
    return { asrProvider: provider, requireTimestamps: task.request?.asr_require_timestamps !== false };
  }

  function asrPayload() {
    const provider = document.getElementById("asrProvider")?.value;
    return provider ? { asr_provider: provider, asr_require_timestamps: provider !== "siliconflow" } : {};
  }

  const asrSettingsView = window.AsrSettings?.create({ document, request: fetchJson, onChange: (config, chosen) => renderAsrProviders(config, chosen) });

  function renderAsrProviders(config, chosen) {
    const select = document.getElementById("asrProvider");
    if (!select || !config) return;
    const names = { faster_whisper: "离线 Whisper · 时间戳 · 无 API 费用", siliconflow: "硅基流动 SenseVoice · 仅纯文本", cloudflare: "Cloudflare Whisper · 时间戳 · 需验证免费计划", groq: "Groq Whisper · 时间戳" };
    select.replaceChildren();
    for (const item of config.providers) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = (names[item.id] || item.id) + (item.configured ? "" : "（未配置）");
      option.disabled = !item.configured;
      select.appendChild(option);
    }
    select.value = config.providers.some(item => item.id === chosen && item.configured) ? chosen : config.default;
  }

  async function startLinkAnalysis({ url, mode, useMock, label, asrProvider = null, requireTimestamps = true }, automaticRetry = false) {
    if (!identityResolved) return;
    if (!url) {
      setTaskStatus("failed", "请输入视频链接。");
      queueView?.notify("请输入视频链接。", true);
      return;
    }

    const llmPayload = analysisModelPayload(mode, useMock);
    if (llmPayload === null) {
      return;
    }

    els.linkSubmit.disabled = true;
    els.linkSubmit.textContent = automaticRetry ? "登录成功，正在重试..." : "提交中...";
    try {
      const body = Object.assign({ url: url, mode: mode, use_mock: useMock }, asrProvider ? { asr_provider: asrProvider, asr_require_timestamps: requireTimestamps } : asrPayload(), llmPayload);
      if (label) {
        body.label = label;
      }
      const data = await fetchJson(
        "/analyze",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
        20000,
      );
      state.pendingCookieRetry = null;
      renderCookieAuthorization(state.cookieAuthorization);
      els.videoUrl.value = url;
      els.linkMode.value = mode;
      setCookieMessage(
        automaticRetry ? "Cookies 已刷新，链接任务已自动重新提交。" : "链接任务已提交。",
        "ok",
      );
      await queueView.onSubmitted(data.task_id);
    } catch (error) {
      setTaskStatus("failed", `提交失败：${error.message}`);
      queueView?.notify(`提交失败：${error.message}`, true);
    } finally {
      els.linkSubmit.disabled = !identityResolved;
      syncAnalysisModeUi();
    }
  }

  /** 链接分析。 */
  async function submitLink(event) {
    event.preventDefault();
    const mode = els.linkMode.value;
    // 优先使用解析出的干净链接与标签；未解析（如纯链接）时退回输入框原文。
    const parsed = state.parsedLink;
    const url = (parsed && parsed.url) ? parsed.url : els.videoUrl.value.trim();
    const label = (parsed && parsed.url && els.linkLabel.value.trim()) ? els.linkLabel.value.trim() : (parsed ? parsed.label : null);
    await startLinkAnalysis({
      url: url,
      mode: mode,
      useMock: mode === "transcript" ? false : !els.linkReal.checked,
      label: label,
    });
  }

  /** 防抖解析分享文案：实时调用 /parse-link 预览干净链接与可编辑标签。 */
  function attachLinkParser() {
    let timer = null;
    els.videoUrl.addEventListener("input", () => {
      const text = els.videoUrl.value.trim();
      if (timer) {
        clearTimeout(timer);
      }
      if (!text) {
        state.parsedLink = null;
        els.linkPreview.classList.add("hidden");
        return;
      }
      timer = setTimeout(async () => {
        try {
          const data = await fetchJson("/parse-link", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: text }),
          }, 8000);
          state.parsedLink = data;
          renderLinkPreview(data);
        } catch (error) {
          state.parsedLink = null;
          els.linkPreview.classList.add("hidden");
        }
      }, 350);
    });
  }

  /** 渲染解析预览：平台徽标 + 干净链接 + 可编辑标签。 */
  function renderLinkPreview(data) {
    els.linkPreviewBadge.textContent = data.platform || "未知平台";
    els.linkPreviewBadge.className = `platform-badge platform-${String(data.platform || "").toLowerCase()}`;
    els.linkPreviewUrl.textContent = data.url || "（未识别到链接）";
    els.linkLabel.value = data.label || "";
    els.linkPreviewNote.textContent = data.note || "";
    els.linkPreview.classList.toggle("hidden", !data.url);
  }

  async function retryPendingCookieTask() {
    if (!state.pendingCookieRetry) {
      return;
    }
    const pending = state.pendingCookieRetry;
    setCookieMessage("Cookies 已保存，正在自动重试刚才的链接...", "ok");
    await startLinkAnalysis(pending, true);
  }

  /* ======================================================================
   * 7. 历史任务
   * ==================================================================== */

  /** 侧栏与工作台共用轻量队列快照，避免独立轮询产生陈旧状态。 */
  async function loadHistory() {
    await queueView?.refresh(true);
  }

  /** 计算任务的显示标题：优先 AI 摘要标题，其次媒体标题，最后模式名。 */
  function taskDisplayTitle(task) {
    if (task.title && task.title.trim()) return task.title.trim();
    if (task.media_title && task.media_title.trim() && !task.media_title.startsWith("upload://")) return task.media_title;
    const result = task.result || {};
    if (result.title && result.title.trim()) {
      return result.title.trim();
    }
    const mediaTitle = result.media && result.media.title;
    if (mediaTitle && mediaTitle.trim() && !String(mediaTitle).startsWith("upload://")) {
      return mediaTitle.trim();
    }
    const mode = task.request && task.request.mode;
    if (task.request?.label) return task.request.label;
    if (task.request?.url?.startsWith("upload://")) return task.request.url.slice(9);
    return MODE_TEXT[mode] || mode || "任务";
  }

  /** 渲染历史任务列表。 */
  function renderHistory(tasks) {
    for (const task of tasks) {
      if (task.used_at) state.usedTasks.set(task.task_id, task.used_at);
    }
    renderUsedBadge();
    const signature = JSON.stringify(tasks.map((task) => [
      task.task_id, task.status, taskDisplayTitle(task), task.request?.label,
      task.request?.url, task.created_at, taskUsedAt(task),
    ]));
    els.historyMessage.textContent = tasks.length ? `共 ${tasks.length} 条（最多显示最近 50 条）` : "";
    if (signature === state.historySignature) return;
    state.historySignature = signature;
    els.historyList.innerHTML = "";

    if (!tasks.length) {
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = "暂无历史任务";
      els.historyList.appendChild(empty);
      els.historyMessage.textContent = "";
      return;
    }
    els.historyMessage.textContent = `共 ${tasks.length} 条（最多显示最近 50 条）`;

    tasks.forEach((task) => {
      const item = document.createElement("div");
      item.className = "history-mini-item";
      item.dataset.taskId = task.task_id;

      const top = document.createElement("div");
      top.className = "history-mini-top";
      const mode = document.createElement("span");
      mode.className = "history-mini-mode";
      mode.textContent = truncate(taskDisplayTitle(task), 30);
      mode.title = taskDisplayTitle(task);
      const badge = document.createElement("span");
      badge.className = `badge ${STATUS_BADGE[task.status] || "badge-muted"}`;
      badge.textContent = STATUS_TEXT[task.status] || task.status;
      top.append(mode, badge);

      const url = document.createElement("div");
      url.className = "history-mini-url";
      const labelText = task.request && task.request.label;
      url.textContent = truncate(labelText || (task.request && task.request.url), 36);
      url.title = labelText ? `${labelText}\n${task.request && task.request.url || ""}` : (task.request && task.request.url) || "";

      const meta = document.createElement("div");
      meta.className = "history-mini-meta";
      const time = document.createElement("span");
      time.textContent = formatTime(task.created_at);
      const usedAt = taskUsedAt(task);
      if (usedAt) {
        const used = document.createElement("span");
        used.className = "task-used-badge";
        used.textContent = "已使用";
        used.title = `首次复制：${formatTime(usedAt)}`;
        meta.append(used);
      }
      const del = document.createElement("button");
      del.type = "button";
      del.className = "history-delete";
      del.textContent = "删除";
      del.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteHistoryTask(task.task_id);
      });
      meta.prepend(time);
      meta.append(del);

      item.append(top, url, meta);
      item.addEventListener("click", () => openHistoryTask(task.task_id));
      els.historyList.appendChild(item);
    });

    highlightHistory(state.currentTaskId);
  }

  /** 高亮当前查看的任务。 */
  function highlightHistory(taskId) {
    els.historyList.querySelectorAll(".history-mini-item").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.taskId === taskId);
    });
  }

  /** 回看某个历史任务。 */
  async function openHistoryTask(taskId) {
    queueView?.showDetail(taskId);
    stopPolling();
    const version = state.watchVersion;
    try {
      const task = await fetchJson(`/tasks/${taskId}`, {}, 10000);
      if (version !== state.watchVersion) return;
      applyTask(task);
      if (task.status === "queued" || task.status === "running") {
        startPolling(taskId);
      }
    } catch (error) {
      if (version !== state.watchVersion) return;
      if (error.status === 404) {
        rememberTask(null);
        setTaskStatus("failed", "任务不存在或已被删除。");
        queueView?.onTaskError("任务不存在、已被删除或不属于当前浏览器。");
      } else if ([401, 403].includes(error.status)) {
        els.taskMessage.textContent = "登录或访问令牌已失效，请重新验证。";
        queueView?.onTaskError(els.taskMessage.textContent);
      } else {
        startPolling(taskId);
        els.taskMessage.textContent = "连接暂时中断，正在恢复任务状态。";
      }
    }
  }

  /** 删除历史任务并刷新列表。 */
  async function deleteHistoryTask(taskId) {
    if (!window.confirm(`确认删除任务 ${taskId}？关联的结果文件与临时文件也会一并删除。`)) {
      return;
    }
    try {
      await fetchJson(`/tasks/${taskId}`, { method: "DELETE" }, 15000);
      state.chatSessions.delete(taskId);
      try {
        const cache = JSON.parse(localStorage.getItem(chatStorageKey()) || "{}");
        delete cache[taskId];
        localStorage.setItem(chatStorageKey(), JSON.stringify(cache));
      } catch (error) { /* Storage may be disabled. */ }
      if (state.currentTaskId === taskId) {
        stopPolling();
        state.currentTaskId = null;
        state.currentTask = null;
        rememberTask(null);
        updateAgentContext();
        queueView?.showHome();
      }
      await loadHistory();
    } catch (error) {
      els.historyMessage.textContent = `删除失败：${error.message}`;
    }
  }

  /* ======================================================================
   * 8. Cookies 授权
   * ==================================================================== */

  function setCookieMessage(message, kind = "") {
    els.cookieMessage.textContent = message;
    els.cookieMessage.className = `cookie-message${kind ? ` is-${kind}` : ""}`;
  }

  /** 根据后端返回的摘要渲染状态徽章。 */
  function cookieBadge(summary) {
    if (!summary || !summary.exists) {
      return { text: "未配置", cls: "badge-danger" };
    }
    if (summary.has_login_cookie) {
      return { text: `已登录（${summary.count} 条）`, cls: "badge-success" };
    }
    return { text: `已配置，无登录态（${summary.count} 条）`, cls: "badge-accent" };
  }

  function renderCookieAuthorization(capabilities) {
    const previousMode = state.cookieAuthorization?.mode;
    const desktop = capabilities?.mode === "desktop" && capabilities.desktop_available === true;
    const remote = capabilities?.mode === "remote" && capabilities.qr_available === true;
    state.cookieAuthorization = { ...capabilities, mode: remote ? "remote" : desktop ? "desktop" : "import" };
    els.cookieAdminControls.hidden = false;
    els.cookieReadOnly.hidden = true;
    els.startQrLogin.hidden = !desktop && !remote;
    els.checkCookieBrowser.hidden = !desktop;
    els.desktopCookieActions.hidden = !desktop;
    els.cookieTextActions.classList.toggle("two", desktop);
    els.cookieTextActions.classList.toggle("single", !desktop);
    els.cookieImportSummary.textContent = desktop || remote ? "手动导入与故障排查" : "导入平台 Cookies";
    els.cookieAuthHint.textContent = !state.accountIsAdmin ? "我的抖音授权 · 仅当前账号使用" : remote ? "管理员抖音授权" : desktop
      ? "本地桌面授权 · 抖音扫码 / 文件导入" : "文件 / 文本导入 · 当前服务无桌面登录窗口";
    els.startQrLogin.textContent = state.pendingCookieRetry
      ? "登录并自动重试该链接" : "登录/刷新抖音 Cookies";
    if (previousMode !== state.cookieAuthorization.mode) {
      els.cookieImport.open = !desktop && !remote;
    }
  }

  /** 拉取 /cookies/status 并刷新两处状态徽章。 */
  async function refreshCookieStatus() {
    try {
      const summary = await fetchJson("/cookies/status", {}, 8000);
      renderCookieAuthorization(summary.authorization);
      window.VideoInsightLocalLogin?.render(summary.local_helper);
      const badge = cookieBadge(summary);
      els.cookieState.className = `badge ${badge.cls}`;
      els.cookieState.textContent = badge.text;
      els.sidebarCookieStatus.className = `badge ${badge.cls}`;
      els.sidebarCookieStatus.textContent = badge.text;
      els.clearCookieLogin.disabled = state.clearingCookies;
      return summary;
    } catch (error) {
      renderCookieAuthorization(null);
      window.VideoInsightLocalLogin?.render(null);
      els.cookieAuthHint.textContent = "授权状态检查失败";
      els.cookieState.className = "badge badge-danger";
      els.cookieState.textContent = "检查失败";
      els.sidebarCookieStatus.className = "badge badge-danger";
      els.sidebarCookieStatus.textContent = "检查失败";
      els.clearCookieLogin.disabled = true;
      return null;
    }
  }

  /** 描述 Cookie 摘要为一句中文说明。 */
  function describeCookieSummary(summary) {
    if (!summary || !summary.exists) {
      return "未保存 Cookies";
    }
    const loginState = summary.has_login_cookie ? "检测到登录态" : "未检测到关键登录态";
    const present = summary.present_important && summary.present_important.length
      ? `；已有：${summary.present_important.join(", ")}`
      : "";
    const missing = summary.missing_important && summary.missing_important.length
      ? `；缺少：${summary.missing_important.join(", ")}`
      : "";
    return `共 ${summary.count || 0} 条，${loginState}${present}${missing}`;
  }

  async function clearCookieLogin() {
    if (state.clearingCookies || !window.confirm("清除当前账号保存的抖音登录态？不会删除历史任务，也不会影响其他账号。")) return;
    state.clearingCookies = true;
    const controls = [...els.cookieAdminControls.querySelectorAll("button, input, textarea")];
    const disabled = controls.map(control => control.disabled);
    controls.forEach(control => { control.disabled = true; });
    state.pendingCookieRetry = null;
    stopQrPolling();
    setCookieMessage("正在清除本账号的抖音登录态...");
    try {
      const result = await fetchJson("/cookies", { method: "DELETE" }, 50000);
      els.cookieText.value = "";
      els.cookieFile.value = "";
      els.qrImage.removeAttribute("src");
      els.qrView.hidden = true;
      window.VideoInsightLocalLogin?.cleared();
      await refreshCookieStatus();
      setCookieMessage(result.comments_cleared
        ? "本账号的抖音登录态已清除，历史任务已保留。"
        : "视频授权已清除；评论授权同步暂时失败，请稍后再次清除。", result.comments_cleared ? "ok" : "error");
    } catch (error) {
      setCookieMessage(`清除失败：${error.message}`, "error");
    } finally {
      state.clearingCookies = false;
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      els.clearCookieLogin.disabled = false;
    }
  }

  /** 保存粘贴的 Cookie 文本或 JSON。 */
  async function saveCookieText() {
    const text = els.cookieText.value.trim();
    if (!text) {
      setCookieMessage("请先粘贴 Cookie 或 Cookie Editor JSON。", "error");
      return;
    }
    const form = new FormData();
    form.append("file", new Blob([text], { type: "text/plain" }), "cookies.txt");
    setCookieMessage("正在保存粘贴内容...");
    try {
      const data = await fetchJson("/cookies", { method: "POST", body: form }, 15000);
      els.cookieText.value = "";
      setCookieMessage(`已保存 ${data.count} 条 Cookies（${data.size} bytes）。`, "ok");
      await refreshCookieStatus();
      await retryPendingCookieTask();
    } catch (error) {
      setCookieMessage(`保存失败：${error.message}`, "error");
    }
  }

  /** 上传 cookies.txt / JSON 文件。 */
  async function uploadCookieFile() {
    const file = els.cookieFile.files && els.cookieFile.files[0];
    if (!file) {
      setCookieMessage("请先选择 cookies.txt 或 Cookie Editor JSON 文件。", "error");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    setCookieMessage("正在保存上传文件...");
    try {
      const data = await fetchJson("/cookies", { method: "POST", body: form }, 15000);
      setCookieMessage(`已保存 ${data.count} 条 Cookies（${data.size} bytes）。`, "ok");
      await refreshCookieStatus();
      await retryPendingCookieTask();
    } catch (error) {
      setCookieMessage(`保存失败：${error.message}`, "error");
    }
  }

  function stopQrPolling() {
    window.clearTimeout(state.qrTimer);
    state.qrTimer = null;
    state.qrSessionId = null;
    state.qrAttempts = 0;
    state.qrFailures = 0;
    els.cancelQrLogin.hidden = true;
    els.saveQrImage.hidden = true;
    els.saveQrImage.removeAttribute("href");
    els.qrMobileHint.hidden = true;
    els.qrExpiry.hidden = true;
    els.qrStage.setAttribute("aria-busy", "false");
    els.startQrLogin.textContent = state.pendingCookieRetry
      ? "登录并自动重试该链接" : "登录/刷新抖音 Cookies";
  }

  function scheduleQrPoll() {
    window.clearTimeout(state.qrTimer);
    if (state.qrSessionId) {
      state.qrTimer = window.setTimeout(pollQrLogin, QR_POLL_INTERVAL_MS);
    }
  }

  function resumeQrLogin() {
    if (document.visibilityState === "visible" && state.qrSessionId) {
      window.clearTimeout(state.qrTimer);
      state.qrFailures = 0;
      void pollQrLogin();
    }
  }

  function setQrMessage(message) {
    els.qrView.hidden = false;
    if (els.qrMessage.textContent !== message) els.qrMessage.textContent = message;
  }

  function renderQrSession(session) {
    const slow = session.status === "starting" && Date.now() - state.qrStartedAt > 15000;
    setQrMessage(slow ? "抖音页面响应较慢，正在继续加载；无需反复刷新。" : session.message || "等待扫码确认...");
    const imageUrl = session.qr_image_url;
    if (imageUrl && imageUrl !== els.qrImage.getAttribute("src")) {
      els.qrImage.src = imageUrl;
    }
    els.qrImage.hidden = !imageUrl;
    els.qrPlaceholder.hidden = !!imageUrl;
    els.qrStage.setAttribute("aria-busy", String(session.status === "starting"));
    els.saveQrImage.hidden = !imageUrl;
    els.qrMobileHint.hidden = !imageUrl;
    if (imageUrl) els.saveQrImage.href = imageUrl;
    else els.saveQrImage.removeAttribute("href");
    els.qrExpiry.hidden = session.expires_in == null || session.completed;
    if (session.expires_in != null && !session.completed) {
      els.qrExpiry.textContent = `本次登录会话剩余 ${session.expires_in} 秒`;
    }
  }

  async function cancelQrLogin() {
    const sessionId = state.qrSessionId;
    stopQrPolling();
    els.qrImage.hidden = true;
    els.qrImage.removeAttribute("src");
    els.qrPlaceholder.textContent = "登录已取消";
    els.qrPlaceholder.hidden = false;
    setQrMessage("已取消扫码登录。");
    if (sessionId && sessionId !== "browser") {
      await fetchJson(`/cookies/qr/${sessionId}`, { method: "DELETE" }, 20000);
    }
  }

  /** 启动扫码登录会话并轮询状态。 */
  async function startQrLogin() {
    if (!["desktop", "remote"].includes(state.cookieAuthorization?.mode)) {
      els.cookieImport.open = true;
      els.cookieFile.focus();
      return;
    }
    const remote = state.cookieAuthorization.mode === "remote";
    els.startQrLogin.disabled = true;
    try {
      await window.VideoInsightLocalLogin?.cancel();
      if (state.qrSessionId) await cancelQrLogin();
    } catch (error) {
      setQrMessage(`取消旧会话失败：${error.message}`);
      els.startQrLogin.disabled = false;
      return;
    }
    stopQrPolling();
    state.qrStartedAt = Date.now();
    els.qrStage.hidden = !remote;
    els.qrImage.hidden = true;
    els.qrImage.removeAttribute("src");
    els.qrPlaceholder.textContent = "正在生成二维码...";
    els.qrPlaceholder.hidden = false;
    setQrMessage(remote ? "正在加载抖音二维码..." : "正在启动扫码登录窗口...");

    try {
      const session = await fetchJson("/cookies/qr/start", { method: "POST" }, 15000);
      state.qrSessionId = session.session_id;
      state.qrAttempts = 0;
      renderQrSession(session);
      els.cancelQrLogin.hidden = !remote;
      els.startQrLogin.textContent = remote ? "刷新二维码" : "重新打开登录窗口";
      await pollQrLogin();
    } catch (error) {
      els.qrPlaceholder.textContent = "二维码未生成";
      setQrMessage(`启动失败：${error.message}`);
    } finally {
      els.startQrLogin.disabled = false;
    }
  }

  /** 轮询扫码登录状态直至完成或超时。 */
  async function pollQrLogin() {
    if (!state.qrSessionId) {
      return;
    }
    if (state.qrPolling) return;
    const sessionId = state.qrSessionId;
    state.qrAttempts += 1;
    if (state.qrAttempts > QR_POLL_MAX_ATTEMPTS) {
      stopQrPolling();
      setQrMessage("扫码检测已暂停（超过 5 分钟），如需继续请重新点击「扫码登录」。");
      return;
    }
    state.qrPolling = true;
    try {
      const status = await fetchJson(`/cookies/qr/${sessionId}/status`, {}, 10000);
      if (state.qrSessionId !== sessionId) return;
      state.qrFailures = 0;
      if (status.completed) {
        stopQrPolling();
        els.qrImage.hidden = true;
        els.qrImage.removeAttribute("src");
        els.qrStage.hidden = true;
        setQrMessage(status.message || "扫码登录成功，Cookies 已保存。");
        setCookieMessage(status.message || "扫码登录成功，Cookies 已保存。", "ok");
        await refreshCookieStatus();
        await retryPendingCookieTask();
        return;
      }
      renderQrSession(status);
      if (["expired", "failed", "cancelled"].includes(status.status)) {
        stopQrPolling();
        els.qrPlaceholder.textContent = status.status === "expired" ? "二维码已过期" : "二维码不可用";
        els.qrImage.removeAttribute("src");
        els.startQrLogin.textContent = status.status === "expired" ? "刷新二维码" : "重新生成二维码";
      }
    } catch (error) {
      if (state.qrSessionId !== sessionId) return;
      state.qrFailures += 1;
      if (![400, 401, 403, 404].includes(error.status) && state.qrFailures <= QR_POLL_MAX_ERRORS) {
        setQrMessage(`连接暂时中断，正在自动重连（${state.qrFailures}/${QR_POLL_MAX_ERRORS}）；原登录会话仍保留。`);
        return;
      }
      stopQrPolling();
      els.qrImage.hidden = true;
      els.qrImage.removeAttribute("src");
      els.qrPlaceholder.hidden = false;
      els.qrPlaceholder.textContent = "连接中断";
      setQrMessage(`检测失败：${error.message}`);
      els.startQrLogin.textContent = "重新连接扫码登录";
    } finally {
      state.qrPolling = false;
      scheduleQrPoll();
    }
  }

  /** 打开专用登录窗口。 */
  async function openCookieBrowser() {
    setCookieMessage("正在打开专用登录窗口...");
    try {
      const data = await fetchJson("/cookies/browser/start", { method: "POST" }, 15000);
      setCookieMessage(data.message || "请在新打开的浏览器窗口登录抖音，登录后点击「抓取登录态」。");
    } catch (error) {
      setCookieMessage(`打开失败：${error.message}`, "error");
    }
  }

  /** 检查专用登录窗口连接状态。 */
  async function checkCookieBrowser() {
    setCookieMessage("正在检查专用登录窗口...");
    try {
      const data = await fetchJson("/cookies/browser/status", {}, 10000);
      if (!data.running) {
        setCookieMessage("专用登录窗口未连接，请先点击「打开登录窗口」。", "error");
        return;
      }
      const tabText = data.has_douyin_tab ? "已找到抖音页面" : "未找到抖音页面，可再次点击「打开登录窗口」";
      const count = Array.isArray(data.tabs) ? data.tabs.length : 0;
      setCookieMessage(`DevTools 已连接（端口 ${data.port}），${tabText}，标签页 ${count} 个。`);
    } catch (error) {
      setCookieMessage(`检查失败：${error.message}`, "error");
    }
  }

  /** 抓取专用窗口中的登录态。 */
  async function captureCookieBrowser() {
    setCookieMessage("正在抓取专用窗口里的 douyin.com cookies...");
    try {
      const data = await fetchJson("/cookies/browser/capture", { method: "POST" }, CAPTURE_TIMEOUT_MS);
      const savedText = data.saved
        ? `已保存 ${data.count} 条 cookies`
        : `抓到 ${data.count} 条 cookies，未覆盖本地文件`;
      const loginState = data.has_login_cookie ? "检测到登录态" : "未检测到关键登录态";
      const missing = data.missing_important && data.missing_important.length
        ? `；缺少：${data.missing_important.join(", ")}`
        : "";
      setCookieMessage(`${savedText}，${loginState}${missing}。`, data.saved ? "ok" : "error");
      await refreshCookieStatus();
      if (data.saved) {
        await retryPendingCookieTask();
      }
    } catch (error) {
      setCookieMessage(`抓取失败：${error.message}`, "error");
    }
  }

  /* ======================================================================
   * 10. AI Agent 对话
   * ==================================================================== */

  /** 更新对话框右上角的上下文徽章。 */
  function updateAgentContext() {
    if (!loadModelConfig() && !serverModelConfig) {
      els.agentContext.className = "badge badge-danger";
      els.agentContext.textContent = "未配置 API Key";
      els.agentContext.title = "请先在模型设置中配置你的 API Key";
      return;
    }
    const task = state.currentTask;
    if (!task) {
      els.agentContext.className = "badge badge-muted";
      els.agentContext.textContent = "未选择任务";
      return;
    }
    if (task.status !== "succeeded") {
      els.agentContext.className = "badge badge-accent";
      els.agentContext.textContent = `任务${STATUS_TEXT[task.status] || task.status}，暂不可提问`;
      return;
    }
    const title = (task.result && task.result.media && task.result.media.title) || task.task_id.slice(0, 8);
    const config = loadModelConfig() || serverModelConfig;
    const modelLabel = config && config.model ? ` · ${config.model}` : "";
    els.agentContext.className = "badge badge-agent";
    els.agentContext.textContent = `上下文：${truncate(title, 16)}${modelLabel}`;
    els.agentContext.title = task.task_id;
  }

  /**
   * 追加一条气泡消息。
   * @param {"user"|"agent"} role 发言角色
   * @param {string} text 文本内容
   * @param {{error?: boolean, thinking?: boolean}} [options] 附加样式
   * @returns {HTMLElement} 气泡内容节点，便于后续更新
   */
  function appendBubble(role, text, options = {}) {
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}`;
    if (options.error) {
      bubble.classList.add("is-error");
    }

    const avatar = document.createElement("div");
    avatar.className = "chat-avatar";
    avatar.textContent = role === "user" ? "我" : "AI";

    const content = document.createElement("div");
    content.className = "chat-content";
    if (options.thinking) {
      content.classList.add("is-thinking");
      content.textContent = text;
    } else if (role === "agent") {
      content.innerHTML = renderMarkdown(text);
    } else {
      content.textContent = text;
    }

    bubble.append(avatar, content);
    els.chatMessages.appendChild(bubble);
    scrollChatToBottom();
    return content;
  }

  function scrollChatToBottom() {
    els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
  }

  /** 切换发送按钮与快捷 Prompt 的可用状态。 */
  function setChatBusy(busy) {
    els.sendChatBtn.disabled = busy || !identityResolved;
    els.sendChatBtn.textContent = busy ? "思考中..." : "发送";
    els.promptChips.querySelectorAll(".chip-btn").forEach((chip) => {
      chip.disabled = busy || !identityResolved;
    });
  }

  /**
   * 发送问题到后端 /tasks/{id}/chat。
   * 未配置用户自己的 API Key 时，先提示去「模型设置」配置，不发起对话。
   * @param {string} rawQuestion 用户问题
   */
  async function sendChat(rawQuestion) {
    if (!identityResolved || state.chatController) return;
    const question = String(rawQuestion || "").trim();
    if (!question) {
      return;
    }

    // 用户必须配置自己的 LLM API Key 才能对话
    const llmConfig = loadModelConfig();
    if (!llmConfig && !serverModelConfig) {
      appendBubble(
        "agent",
        "开始对话前，请先在左侧「⚙️ 模型设置」中填写你自己的模型 API Key（如 DeepSeek / OpenAI）。\n\n配置完成后即可使用你自己的模型进行 AI 对话。",
        { error: true },
      );
      scrollToSettings();
      els.chatInput.focus();
      return;
    }

    let task = state.currentTask;
    if (!task) {
      // 未选中任务时，自动拾取最近一个成功任务作为对话上下文
      task = await pickRecentSucceededTask();
      if (!identityResolved) return;
      if (task) {
        applyTask(task);
        updateAgentContext();
        highlightHistory(task.task_id);
      }
    }
    if (!task) {
      appendBubble("agent", "还没有可对话的分析结果。请先上传文件或提交链接完成一次分析，完成后我就能基于转写稿回答你的问题。", { error: true });
      return;
    }
    if (task.status !== "succeeded") {
      appendBubble(
        "agent",
        `当前任务还${STATUS_TEXT[task.status] || "未完成"}，请等分析完成后再提问。`,
        { error: true },
      );
      return;
    }

    const messages = loadChatMessages(task.task_id).slice();
    const history = chatHistory(messages);
    const reply = { role: "assistant", content: "", complete: false };
    messages.push({ role: "user", content: question.slice(0, 20000), complete: true }, reply);
    saveChatMessages(task.task_id, messages);
    appendBubble("user", question);
    els.chatInput.value = "";
    const thinking = appendBubble("agent", "思考中...", { thinking: true });
    setChatBusy(true);
    const controller = new AbortController();
    state.chatController = controller;
    let answer = "";
    let failureMessage = "";
    let renderTimer = null;
    const render = () => {
      renderTimer = null;
      if (!thinking.isConnected) return;
      thinking.classList.remove("is-thinking");
      thinking.innerHTML = renderMarkdown(answer);
      reply.content = answer.slice(0, 20000);
      saveChatMessages(task.task_id, messages);
      scrollChatToBottom();
    };

    try {
      // 携带用户自备模型配置，让后端优先使用用户的 LLM
      await fetchEvents(
        `/tasks/${task.task_id}/chat/stream`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(
            Object.assign(
              { question: question, history },
              llmConfig ? {
                llm_provider: llmConfig.provider,
                llm_base_url: llmConfig.baseUrl,
                llm_protocol: llmConfig.protocol || "chat_completions",
                llm_model: llmConfig.model,
                llm_api_key: llmConfig.apiKey,
              } : {},
            ),
          ),
        },
        (event, data) => {
          if (event === "delta") {
            answer += data.text;
            if (renderTimer === null) renderTimer = window.setTimeout(render, 80);
          }
          if (event === "done") reply.complete = Boolean(answer.trim());
          return event === "done";
        },
        controller,
        130000,
      );
    } catch (error) {
      if (!thinking.isConnected) return;
      thinking.classList.remove("is-thinking");
      const message = error.name === "AbortError" ? "连接超时，请稍后重试。" : error.message;
      failureMessage = `提问失败：${message}`;
      thinking.innerHTML = renderMarkdown(answer ? `${answer}\n\n生成中断：${message}` : `提问失败：${message}`);
      thinking.parentElement.classList.add("is-error");
      scrollChatToBottom();
    } finally {
      window.clearTimeout(renderTimer);
      reply.content = (answer || failureMessage).slice(0, 20000);
      saveChatMessages(task.task_id, messages);
      if (!thinking.parentElement.classList.contains("is-error") && answer) render();
      if (state.chatController === controller) {
        state.chatController = null;
        setChatBusy(false);
        els.chatInput.focus();
      }
    }
  }

  /** 平滑滚动到模型设置区。 */
  function scrollToSettings() {
    const target = document.getElementById("settings");
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  /** 从历史任务中取最近一个成功任务，作为默认对话上下文。 */
  async function pickRecentSucceededTask() {
    try {
      const tasks = await fetchJson("/tasks?limit=1&status=succeeded&view=summary", {}, 10000);
      return tasks.length ? await fetchJson(`/tasks/${tasks[0].task_id}`) : null;
    } catch (error) {
      return null;
    }
  }

  /**
   * 快捷 Prompt 入口：由页面上的 chip 按钮（onclick）调用。
   * @param {string} promptText 预设问题文本
   */
  window.sendPrompt = function sendPrompt(promptText) {
    sendChat(promptText);
  };

  /* ======================================================================
   * 9. 模型设置（用户自备 LLM / API Key）
   * ==================================================================== */

  const MODEL_CONFIG_KEY = "videoinsight_llm_config";
  const PROVIDER_DEFAULTS = {
    deepseek: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-flash", protocol: "chat_completions" },
    openai: { baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini", protocol: "chat_completions" },
    custom: { baseUrl: "", model: "", protocol: "chat_completions" },
  };

  /** 模型配置存储键：登录账号按 account_id 隔离，未登录沿用原有共用键。 */
  function modelConfigKey() {
    return state.accountId ? `${MODEL_CONFIG_KEY}:${state.accountId}` : MODEL_CONFIG_KEY;
  }

  /** 读取当前账号的模型配置；未配置返回 null。 */
  function loadModelConfig() {
    if (!identityResolved) {
      // 身份未就绪前不读任何模型配置：登录模式下账号可能已切换。
      return null;
    }
    try {
      const raw = localStorage.getItem(modelConfigKey());
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      return parsed && parsed.apiKey ? parsed : null;
    } catch (error) {
      return null;
    }
  }

  /** 账号身份就绪后，按账号加载/清理模型配置并启用表单。 */
  function applyAccountIdentity(account) {
    state.accountId = account?.account_id ?? null;
    state.accountIsAdmin = account ? account.is_admin === true : true;
    state.uploadOwner = account?.username ?? null; // Keep existing resumable upload records accessible.

    identityResolved = true;

    let hadLegacy = false;
    if (state.accountId) {
      // 登录账号：旧的共用配置无法确认归属，直接清除并提示重新配置。
      try {
        hadLegacy = localStorage.getItem(MODEL_CONFIG_KEY) !== null;
        if (hadLegacy) localStorage.removeItem(MODEL_CONFIG_KEY);
      } catch (error) { /* 存储不可用 */ }
    }
    legacyConfigCleared = hadLegacy;

    const config = loadModelConfig();
    fillModelForm(config);
    renderModelState();
    updateAgentContext();
    setIdentityControls(true);
    if (hadLegacy && !config) {
      setModelMessage("已切换到账号登录：旧版共用模型配置已清除，请为本账号重新配置 API Key。", "error");
    }
  }

  function setIdentityControls(ready) {
    const modelControls = [els.llmProvider, els.llmBaseUrl, els.llmProtocol, els.llmModel, els.llmApiKey,
      els.saveModelConfig, els.testModelConfig, els.clearModelConfig];
    for (const control of modelControls) control.disabled = !ready;
    els.linkSubmit.disabled = !ready;
    setUploadBusy(Boolean(uploadClient?.running));
    setChatBusy(Boolean(state.chatController));
  }

  function clearStaleAccountState() {
    accountStateEpoch++;
    identityResolved = false;
    stopPolling();
    state.chatController?.abort();
    state.chatController = null;
    uploadClient?.pause();
    uploadClient = null;
    state.pendingCookieRetry = null;
    serverModelConfig = null;
    asrSettingsView?.reset();
    const asrSelect = document.getElementById("asrProvider");
    if (asrSelect) asrSelect.replaceChildren();
    fillModelForm(null);
    renderModelState();
    updateAgentContext();
    setModelMessage("");
    setIdentityControls(false);
  }

  /** 把表单内容填入 UI。 */
  function fillModelForm(config) {
    config = config || serverModelConfig;
    const provider = (config && config.provider) || "deepseek";
    const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.custom;
    els.llmProvider.value = provider;
    els.llmBaseUrl.value = (config && config.baseUrl) || defaults.baseUrl;
    els.llmProtocol.value = (config && config.protocol) || defaults.protocol;
    els.llmModel.value = (config && config.model) || defaults.model;
    els.llmApiKey.value = (config && config.apiKey) || "";
  }

  /** provider 切换时：仅在输入为空或仍为旧默认值时才替换为新的默认 base_url / model。 */
  function applyProviderDefaults(provider, previous) {
    const prevDefaults = PROVIDER_DEFAULTS[previous] || PROVIDER_DEFAULTS.custom;
    const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.custom;
    if (!els.llmBaseUrl.value || (previous && els.llmBaseUrl.value === prevDefaults.baseUrl)) {
      els.llmBaseUrl.value = defaults.baseUrl;
    }
    if (!els.llmModel.value || (previous && els.llmModel.value === prevDefaults.model)) {
      els.llmModel.value = defaults.model;
    }
    if (!els.llmProtocol.value || (previous && els.llmProtocol.value === prevDefaults.protocol)) {
      els.llmProtocol.value = defaults.protocol;
    }
  }

  /** 更新右上角状态徽章与提示。 */
  function renderModelState() {
    const local = loadModelConfig();
    const config = local || serverModelConfig;
    if (!config) {
      els.modelState.className = "badge badge-danger";
      els.modelState.textContent = "未配置（逐字稿可用）";
      els.modelState.title = "AI 总结、观点提取和对话需要 API Key；逐字稿不需要";
      return;
    }
    els.modelState.className = "badge badge-success";
    const providerLabel = {
      deepseek: "DeepSeek",
      openai: "OpenAI",
      custom: "自定义",
    }[config.provider] || config.provider;
    els.modelState.textContent = `${providerLabel} · ${config.model || "默认模型"}${local ? "" : " · 服务器默认"}`;
    els.modelState.title = config.baseUrl || "";
  }

  /** Validate display names only for the official endpoint; gateways may use aliases. */
  function validateModelId(baseUrl, model) {
    try {
      const url = new URL(/^https?:\/\//i.test(baseUrl) ? baseUrl : `https://${baseUrl}`);
      if (url.hostname === "api.deepseek.com" && /\s/.test(model)) {
        setModelMessage("模型名称请填 deepseek-flash 或 deepseek-v4-flash，不能填 DeepSeek V4 Flash 这样的显示名称。", "error");
        els.llmModel.focus();
        return false;
      }
    } catch (error) {
      // URL validation remains the responsibility of the request layer.
    }
    return true;
  }

  /** 保存表单配置到 localStorage。 */
  function saveModelConfig() {
    if (!identityResolved) return;
    const provider = els.llmProvider.value;
    const baseUrl = els.llmBaseUrl.value.trim();
    const protocol = els.llmProtocol.value;
    const model = els.llmModel.value.trim();
    const apiKey = els.llmApiKey.value.trim();
    if (!apiKey) {
      setModelMessage("请填写 API Key。", "error");
      return;
    }
    if (provider === "custom" && !baseUrl) {
      setModelMessage("自定义服务商需要填写 API Base URL。", "error");
      return;
    }
    if (!validateModelId(baseUrl, model)) {
      return;
    }
    try {
      localStorage.setItem(
        modelConfigKey(),
        JSON.stringify({ provider, baseUrl, protocol, model, apiKey }),
      );
    } catch (error) {
      setModelMessage(`保存失败：${error.message}`, "error");
      return;
    }
    renderModelState();
    updateAgentContext();
    setModelMessage(`已保存，AI 对话将使用 ${model || provider} 模型（${protocol} 协议）。`, "ok");
  }

  /** 用一条极短请求验证「Base URL + 模型 + Key + 协议」是否真的可用。 */
  async function testModelConnection() {
    if (!identityResolved) return;
    const provider = els.llmProvider.value;
    const baseUrl = els.llmBaseUrl.value.trim();
    const protocol = els.llmProtocol.value;
    const model = els.llmModel.value.trim();
    const apiKey = els.llmApiKey.value.trim();
    if (!apiKey && !serverModelConfig) {
      setModelMessage("请先填写 API Key 再测试连接。", "error");
      return;
    }
    if (apiKey && !baseUrl) {
      setModelMessage("请先填写 API Base URL（多数中转站需以 /v1 结尾）。", "error");
      return;
    }
    if (apiKey && !validateModelId(baseUrl, model)) {
      return;
    }
    const originalText = els.testModelConfig.textContent;
    els.testModelConfig.disabled = true;
    els.testModelConfig.textContent = "测试中…";
    setModelMessage("正在连接模型接口，请稍候…");
    try {
      const data = await fetchJson(
        "/llm/test",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(apiKey ? {
            llm_provider: provider,
            llm_base_url: baseUrl,
            llm_protocol: protocol,
            llm_model: model,
            llm_api_key: apiKey,
          } : {}),
        },
        60000,
      );
      if (!identityResolved) return;
      if (data && data.ok) {
        setModelMessage(
          `连接成功：${data.protocol_label || data.protocol} · ${data.model || model} · ${data.latency_ms}ms` +
            (data.endpoint ? ` · ${data.endpoint}` : ""),
          "ok",
        );
      } else {
        setModelMessage(`连接失败：${(data && data.error) || "未知错误"}`, "error");
      }
    } catch (error) {
      if (identityResolved) setModelMessage(`连接失败：${error.message}`, "error");
    } finally {
      els.testModelConfig.disabled = !identityResolved;
      els.testModelConfig.textContent = originalText;
    }
  }

  /** 清除本地模型配置，恢复服务端默认。 */
  function clearModelConfig() {
    if (!identityResolved) return;
    try {
      localStorage.removeItem(modelConfigKey());
    } catch (error) {
      setModelMessage("浏览器未能清除模型配置。", "error");
      return;
    }
    fillModelForm(null);
    renderModelState();
    updateAgentContext();
    setModelMessage(serverModelConfig ? "已清除本地配置，恢复使用服务器默认模型。" : "已清除本地配置。重新配置 API Key 后即可恢复 AI 对话。", "ok");
  }

  function setModelMessage(message, kind = "") {
    els.modelMessage.textContent = message;
    els.modelMessage.className = `cookie-message${kind ? ` is-${kind}` : ""}`;
  }

  function bindModelSettings() {
    let previousProvider = els.llmProvider.value;
    els.llmProvider.addEventListener("change", () => {
      const next = els.llmProvider.value;
      applyProviderDefaults(next, previousProvider);
      previousProvider = next;
    });
    els.saveModelConfig.addEventListener("click", saveModelConfig);
    els.testModelConfig.addEventListener("click", testModelConnection);
    els.clearModelConfig.addEventListener("click", clearModelConfig);
    // 模型配置的加载延后到 refreshServiceState 拿到账号身份之后（applyAccountIdentity）。
    renderModelState();
  }

  /* ======================================================================
   * 9.5 主题切换（深色 / 浅色）
   * ==================================================================== */

  const THEME_KEY = "videoinsight_theme";

  /** 应用主题到 <html> 并同步按钮文案。 */
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const isDark = theme !== "light";
    els.themeToggleIcon.textContent = isDark ? "🌙" : "☀️";
    els.themeToggleText.textContent = isDark ? "深色模式" : "浅色模式";
  }

  /** 读取本地主题偏好，默认深色。 */
  function loadTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "dark";
    } catch (error) {
      return "dark";
    }
  }

  function bindThemeToggle() {
    applyTheme(loadTheme());
    els.themeToggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (error) {
        // 忽略存储失败
      }
    });
  }

  /* ======================================================================
   * 11. 访问令牌（仅在服务端启用鉴权时出现）
   * ==================================================================== */

  /**
   * 用已保存的令牌换取 HttpOnly Cookie。
   * 媒体由 `<video src>` / `<a href>` 原生发起，带不了 Authorization 头，
   * 只有 Cookie 能让它们通过鉴权。
   * @returns {Promise<boolean>} 令牌是否有效
   */
  async function ensureAuthSession() {
    const token = getAuthToken();
    if (!token) {
      return false;
    }
    try {
      return await syncAuthSession(token);
    } catch (error) {
      return false;
    }
  }

  function renderAuthState(valid) {
    const token = getAuthToken();
    if (!token) {
      els.authState.className = "badge badge-danger";
      els.authState.textContent = "未设置";
      return;
    }
    if (valid === false) {
      els.authState.className = "badge badge-danger";
      els.authState.textContent = "令牌无效";
      return;
    }
    els.authState.className = "badge badge-success";
    els.authState.textContent = "已验证";
  }

  function setAuthMessage(message, kind = "") {
    els.authMessage.textContent = message;
    els.authMessage.className = `cookie-message${kind ? ` is-${kind}` : ""}`;
  }

  async function saveAuthToken() {
    const token = els.authToken.value.trim();
    if (!token) {
      setAuthMessage("请填写访问令牌。", "error");
      return;
    }
    let ok = false;
    try {
      ok = await syncAuthSession(token);
    } catch (error) {
      setAuthMessage(`验证失败：${error.message}`, "error");
      return;
    }
    if (!ok) {
      setAuthMessage("访问令牌不正确。", "error");
      renderAuthState(false);
      return;
    }
    try {
      localStorage.setItem(AUTH_TOKEN_KEY, token);
    } catch (error) {
      setAuthMessage(`保存失败：${error.message}`, "error");
      return;
    }
    renderAuthState(true);
    setAuthMessage("令牌已验证，媒体预览与下载也已生效。", "ok");
    // 鉴权通过后把之前失败的请求补上
    await refreshCookieStatus();
    await loadHistory();
    queueView?.start();
  }

  async function clearAuthToken() {
    try {
      localStorage.removeItem(AUTH_TOKEN_KEY);
    } catch (error) {
      // 忽略清除失败
    }
    els.authToken.value = "";
    try {
      await fetch("/auth/session", { method: "DELETE" });
    } catch (error) {
      // 清除 Cookie 失败不影响本地令牌移除
    }
    renderAuthState();
    setAuthMessage("已清除本地令牌。", "ok");
  }

  function bindAuthSettings() {
    els.saveAuthToken.addEventListener("click", saveAuthToken);
    els.clearAuthToken.addEventListener("click", clearAuthToken);
    els.authToken.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        saveAuthToken();
      }
    });
    els.authToken.value = getAuthToken();
  }

  /* ======================================================================
   * 12. 服务状态
   * ==================================================================== */

  /** 拉取 /health 并展示服务与默认模式。 */
  async function refreshServiceState() {
    try {
      const health = await fetchJson("/health", {}, 8000);
      if (typeof health?.login_required !== "boolean") throw new Error("Invalid health response");
      const cookieState = health.cookies_file
        ? "cookies 文件已配置"
        : health.cookies_from_browser
          ? `浏览器 cookies：${health.cookies_from_browser}`
          : "未配置 cookies";
      els.statusDot.classList.remove("is-offline");
      els.serviceState.textContent = `服务正常 · 默认 ${health.mock_mode ? "Mock" : "真实"} 模式 · ${cookieState}`;

      accountLoginEnabled = health.login_required;
      document.getElementById("accountLogout").hidden = !accountLoginEnabled;
      document.getElementById("accountSecurity").hidden = !accountLoginEnabled;
      if (accountLoginEnabled) {
        const account = await fetchJson("/auth/me");
        if (account?.authenticated !== true) {
          window.location.replace("/login");
          return;
        }
        if (typeof account.account_id !== "string" || !account.account_id) {
          els.serviceState.textContent = "无法确认当前账号，请刷新页面重试。";
          return;
        }
        applyAccountIdentity(account);
        document.getElementById("accountLogout").title = account.username || "退出登录";
      } else {
        applyAccountIdentity(null);
      }
      if (health.auth_required && !accountLoginEnabled) {
        els.authSection.hidden = false;
        const valid = await ensureAuthSession();
        renderAuthState(valid);
        if (!valid) {
          setAuthMessage("本服务已启用访问鉴权，请填写访问令牌后使用。", "error");
        }
      }
    } catch (error) {
      els.statusDot.classList.add("is-offline");
      els.serviceState.textContent = "本地服务未连接";
    }
  }

  /* ======================================================================
   * 12. 事件绑定与初始化
   * ==================================================================== */

  function bindTabs() {
    document.querySelectorAll(".tab-btn").forEach((button) => {
      button.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((item) => item.classList.remove("active"));
        button.classList.add("active");
        const target = document.getElementById(button.dataset.target);
        if (button.dataset.target === "rawOutput") els.rawOutput.textContent = JSON.stringify(state.currentTask || {}, null, 2);
        ["summaryOutput", "transcriptOutput", "commentsOutput", "rawOutput"].forEach((id) => {
          const panel = document.getElementById(id);
          if (panel) {
            panel.style.display = panel === target ? "block" : "none";
          }
        });
        syncTranscriptToolbar();
        syncResultActions();
      });
    });
  }

  function bindSidebarNav() {
    document.querySelectorAll(".sidebar-nav-item").forEach((item) => {
      item.addEventListener("click", (event) => {
        if (item.getAttribute("href") === "#agent" && document.body.dataset.view !== "detail") {
          event.preventDefault();
          if (state.currentTaskId) {
            openHistoryTask(state.currentTaskId).then(() => document.getElementById("agent").scrollIntoView({ block: "start" }));
          } else {
            queueView?.notify("请先打开一条视频任务。");
            document.getElementById("queueDashboard").scrollIntoView({ block: "start" });
            return;
          }
        }
        document.querySelectorAll(".sidebar-nav-item").forEach((nav) => nav.classList.remove("active"));
        item.classList.add("active");
      });
    });
  }

  function bindDropZone() {
    const setFile = (file) => {
      if (!file) {
        return;
      }
      try {
        const transfer = new DataTransfer();
        transfer.items.add(file);
        els.mediaFile.files = transfer.files;
      } catch (error) {
        // 部分浏览器不允许写入 input.files，此时仅提示文件名
      }
      els.fileName.textContent = file.name;
    };

    ["dragenter", "dragover"].forEach((name) => {
      els.dropZone.addEventListener(name, (event) => {
        event.preventDefault();
        els.dropZone.classList.add("dragover");
      });
    });
    ["dragleave", "dragend"].forEach((name) => {
      els.dropZone.addEventListener(name, () => els.dropZone.classList.remove("dragover"));
    });
    els.dropZone.addEventListener("drop", (event) => {
      event.preventDefault();
      els.dropZone.classList.remove("dragover");
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      setFile(file);
    });
    // 阻止拖放到页面其他位置时浏览器直接打开文件
    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());

    els.mediaFile.addEventListener("change", () => {
      const file = els.mediaFile.files && els.mediaFile.files[0];
      els.fileName.textContent = file ? file.name : "点击或拖入音视频文件";
    });
  }

  function bindEvents() {
    els.uploadForm.addEventListener("submit", submitUpload);
    els.uploadPause.addEventListener("click", () => uploadClient?.pause());
    els.uploadCancel.addEventListener("click", async () => {
      if (!identityResolved) return;
      els.uploadCancel.disabled = true;
      try {
        await getUploadClient().cancel();
        els.uploadResumeHint.hidden = true;
        syncAnalysisModeUi();
        renderUploadResumeHint();
      } catch (error) {
        els.uploadProgress.hidden = false;
        els.uploadProgressText.textContent = `取消失败：${error.message}`;
      } finally { els.uploadCancel.disabled = false; }
    });
    els.linkForm.addEventListener("submit", submitLink);
    attachLinkParser();
    els.uploadMode.addEventListener("change", syncAnalysisModeUi);
    els.linkMode.addEventListener("change", syncAnalysisModeUi);
    syncAnalysisModeUi();
    bindTabs();
    bindResultActions();
    bindSidebarNav();
    bindDropZone();

    els.refreshHistory.addEventListener("click", loadHistory);

    els.saveCookieText.addEventListener("click", saveCookieText);
    els.clearCookieLogin.addEventListener("click", clearCookieLogin);
    els.refreshSharedCookies.addEventListener("click", async () => {
      els.refreshSharedCookies.disabled = true;
      setCookieMessage("正在刷新授权状态...");
      try {
        const data = await refreshCookieStatus();
        setCookieMessage(data ? `${describeCookieSummary(data)}。` : "刷新失败，请稍后重试。", data ? "" : "error");
      } finally {
        els.refreshSharedCookies.disabled = false;
      }
    });
    els.verifyCookies.addEventListener("click", async () => {
      setCookieMessage("正在检查已保存的 Cookies...");
      const data = await refreshCookieStatus();
      if (data) {
        setCookieMessage(`${describeCookieSummary(data)}。`);
      } else {
        setCookieMessage("检查 Cookies 失败，请确认服务已启动。", "error");
      }
    });
    els.uploadCookieFile.addEventListener("click", uploadCookieFile);
    els.startQrLogin.addEventListener("click", startQrLogin);
    window.addEventListener("videoinsight:helper-saved", async () => {
      stopQrPolling();
      els.qrView.hidden = true;
      setCookieMessage("抖音登录成功，Cookies 已保存到当前账号。", "ok");
      await refreshCookieStatus();
      await retryPendingCookieTask();
    });
    window.addEventListener("videoinsight:helper-starting", () => {
      if (state.qrSessionId) void cancelQrLogin().catch(() => {});
    });
    els.cancelQrLogin.addEventListener("click", () => cancelQrLogin().catch(error => setQrMessage(`取消失败：${error.message}`)));
    els.qrImage.addEventListener("error", () => {
      if (!els.qrImage.getAttribute("src")) return;
      els.qrImage.hidden = true;
      els.qrPlaceholder.hidden = false;
      els.qrPlaceholder.textContent = "二维码图片加载失败";
      els.saveQrImage.hidden = true;
      setQrMessage("二维码未能显示，请点击刷新二维码重试。");
    });
    document.addEventListener("visibilitychange", resumeQrLogin);
    window.addEventListener("pageshow", resumeQrLogin);
    window.addEventListener("online", resumeQrLogin);
    els.openCookieBrowser.addEventListener("click", openCookieBrowser);
    els.checkCookieBrowser.addEventListener("click", checkCookieBrowser);
    els.captureCookieBrowser.addEventListener("click", captureCookieBrowser);

    els.sendChatBtn.addEventListener("click", () => sendChat(els.chatInput.value));
    els.newChatBtn.addEventListener("click", () => {
      resetCurrentSession();
      queueView?.showHome();
      els.videoUrl.focus();
    });
    els.copyTranscriptBtn.addEventListener("click", copyTranscript);
    els.chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        sendChat(els.chatInput.value);
      }
    });
    els.promptChips.addEventListener("click", (event) => {
      const chip = event.target.closest(".chip-btn");
      if (chip && !chip.disabled && chip.dataset.prompt) {
        sendChat(chip.dataset.prompt);
      }
    });

    els.mediaDownloadMain.addEventListener("click", () => {
      if (els.mediaDownloadMain.dataset.onDemand === "1") {
        els.mediaHint.hidden = false;
        els.mediaHint.textContent = "正在下载源视频，短视频约需 5~60 秒，请稍候…";
      }
    });

    bindModelSettings();
    bindAuthSettings();
    document.getElementById("accountLogout").addEventListener("click", async () => {
      if (!identityResolved || !state.accountId) return;
      identityResolved = false;
      setIdentityControls(false);
      try {
        await fetchJson("/auth/logout", { method: "POST" });
      } catch (error) {
        if (error.status !== 409 && error.status !== 401) {
          identityResolved = true;
          setIdentityControls(true);
          window.alert(`退出失败：${error.message}`);
        }
        return;
      }
      try { localStorage.removeItem(modelConfigKey()); } catch (error) { /* 浏览器禁止写入时仍清空当前页面 */ }
      try { localStorage.removeItem(AUTH_TOKEN_KEY); } catch (error) { /* 登录 Cookie 已在服务端撤销 */ }
      clearStaleAccountState();
      window.location.replace("/login");
    });
    window.addEventListener("pageshow", (event) => {
      if (event.persisted && accountLoginEnabled) {
        clearStaleAccountState();
        window.location.reload();
      }
    });
    bindThemeToggle();
  }

  /** 页面初始化。 */
  async function boot() {
    setIdentityControls(false);
    bindEvents();
    queueView = window.VideoQueue.create({
      fetchJson,
      openTask: openHistoryTask,
      stopWatching: stopPolling,
      title: taskDisplayTitle,
      usedAt: taskUsedAt,
      onTasks: renderHistory,
      onRefreshError: (error) => {
        els.historyMessage.textContent = `状态更新暂时中断，正在重试：${error.message}`;
      },
      retry: (task) => startLinkAnalysis({
        url: task.request.url,
        mode: task.request.mode,
        useMock: task.request.use_mock,
        label: task.request.label,
        ...retryAsrChoice(task),
      }),
    });
    queueView.prepare();
    updateAgentContext();
    await refreshServiceState();
    if (!identityResolved) return;
    renderUploadResumeHint();
    queueView.start();
    try {
      const configEpoch = accountStateEpoch;
      const config = await fetchJson("/config");
      if (!identityResolved || configEpoch !== accountStateEpoch) return;
      renderAsrProviders(config.asr);
      asrSettingsView?.load(config.asr);
      if (identityResolved && config.llm.configured) {
        serverModelConfig = { provider: config.llm.provider, baseUrl: config.llm.base_url, model: config.llm.model, protocol: config.llm.protocol };
        els.llmApiKey.placeholder = "留空使用服务器默认模型";
        fillModelForm(loadModelConfig());
        renderModelState();
        if (!legacyConfigCleared) {
          setModelMessage("服务器默认模型已就绪，可直接分析和追问。填写自己的 Key 可覆盖默认模型。", "ok");
        }
        updateAgentContext();
      }
    } catch (error) { /* Service state already reports connection errors. */ }
    await refreshCookieStatus().catch(() => {});
  }

  boot();
})();
