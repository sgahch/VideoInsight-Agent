/**
 * VideoInsight Agent 前端交互逻辑。
 *
 * 所有数据均来自后端真实接口，界面中不保留任何假数据：
 *   POST /analyze-file   上传分析
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
    sidebarCookieStatus: $("#sidebarCookieStatus"),
    cookieText: $("#cookieText"),
    cookieFile: $("#cookieFile"),
    cookieMessage: $("#cookieMessage"),
    qrImage: $("#qrImage"),
    qrMessage: $("#qrMessage"),
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
    pendingCookieRetry: null,
    parsedLink: null, // 最新的 /parse-link 解析结果（含干净 url 与建议标签）
    transcriptText: "",
    summaryText: "", // AI 总结原始 Markdown，供「复制」按钮使用
  };

  /* ======================================================================
   * 4. 通用工具
   * ==================================================================== */

  const CLIENT_ID_KEY = "videoinsight_client_id";
  const ACTIVE_TASK_KEY = "videoinsight_active_task";
  let accountLoginEnabled = false;
  let serverModelConfig = null;
  const AUTH_TOKEN_KEY = "videoinsight_auth_token";

  /**
   * 获取本浏览器的持久化客户端身份标识。
   * 用于任务归属隔离：历史任务/对话/媒体都只允许访问本客户端创建的任务。
   */
  function getClientId() {
    try {
      let value = localStorage.getItem(CLIENT_ID_KEY);
      if (!value) {
        value = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
        localStorage.setItem(CLIENT_ID_KEY, value);
      }
      return value;
    } catch (error) {
      return `ephemeral-${Math.random().toString(36).slice(2, 12)}`;
    }
  }

  /** 读取本地保存的访问令牌；服务端未启用鉴权时它一直是空串。 */
  function getAuthToken() {
    try {
      return localStorage.getItem(AUTH_TOKEN_KEY) || "";
    } catch (error) {
      return "";
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
    const headers = new Headers(options.headers || {});
    headers.set("X-Client-Id", getClientId());
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
        if (response.status === 401 && accountLoginEnabled) window.location.replace("/login");
        const error = new Error(extractError(data, response));
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("请求超时，请确认服务仍在运行后重试。");
      }
      throw error;
    } finally {
      window.clearTimeout(timer);
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

  async function fetchEvents(url, options, onEvent, controller, idleMs = 45000) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Client-Id", getClientId());
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
        if (error.status === 401 && accountLoginEnabled) window.location.replace("/login");
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
    if (mode === "transcript" || useMock) {
      return {};
    }
    const config = loadModelConfig();
    if (!config) {
      if (serverModelConfig) return {};
      setTaskStatus("failed", "AI 总结或观点提取需要先配置 API Key；逐字稿提取无需配置。");
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
    els.uploadSubmit.textContent = uploadTranscript ? "开始提取逐字稿" : "开始分析文件";
    els.linkSubmit.textContent = linkTranscript ? "立即提取逐字稿" : "立即链接分析";
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
      helper.select();
      document.execCommand("copy");
      helper.remove();
    }
  }

  async function copyTranscript() {
    if (!state.transcriptText) return;
    await copyToClipboard(state.transcriptText);
    els.transcriptCopyStatus.textContent = "已复制";
    window.setTimeout(() => {
      els.transcriptCopyStatus.textContent = "";
    }, 1800);
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
    const text = (PANEL_TEXT[activePanelId()] || (() => ""))();
    if (!String(text || "").trim()) return;
    await copyToClipboard(String(text));
    if (els.copyPanelBtn) {
      els.copyPanelBtn.textContent = "已复制";
      window.setTimeout(() => {
        els.copyPanelBtn.textContent = "复制";
      }, 1600);
    }
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
    const previous = state.currentTask;
    const switched = state.renderedTaskId !== task.task_id;
    if (switched) {
      state.transcriptText = "";
      state.summaryText = "";
      renderTranscript("");
      els.commentsOutput.textContent = "暂无观众评论";
    }
    state.currentTask = task;
    state.currentTaskId = task.task_id;
    state.renderedTaskId = task.task_id;
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
        };
        setCookieMessage(
          "检测到抖音登录态失效。点击“登录/刷新抖音 Cookies”，扫码成功后会自动重试该链接。",
          "error",
        );
        els.startQrLogin.textContent = "登录并自动重试该链接";
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

  /** 上传文件分析。 */
  async function submitUpload(event) {
    event.preventDefault();
    const file = els.mediaFile.files && els.mediaFile.files[0];
    if (!file) {
      setTaskStatus("failed", "请先选择一个音频或视频文件。");
      return;
    }

    const mode = els.uploadMode.value;
    const useMock = mode === "transcript" ? false : !els.uploadReal.checked;
    const llmPayload = analysisModelPayload(mode, useMock);
    if (llmPayload === null) {
      return;
    }

    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    form.append("use_mock", String(useMock));
    Object.entries(llmPayload).forEach(([key, value]) => form.append(key, value));

    els.uploadSubmit.disabled = true;
    els.uploadSubmit.textContent = "上传中...";
    try {
      const data = await fetchJson("/analyze-file", { method: "POST", body: form }, 60000);
      els.taskSource.textContent = file.name;
      startPolling(data.task_id);
    } catch (error) {
      setTaskStatus("failed", `提交失败：${error.message}`);
    } finally {
      els.uploadSubmit.disabled = false;
      syncAnalysisModeUi();
    }
  }

  async function startLinkAnalysis({ url, mode, useMock, label }, automaticRetry = false) {
    if (!url) {
      setTaskStatus("failed", "请输入视频链接。");
      return;
    }

    const llmPayload = analysisModelPayload(mode, useMock);
    if (llmPayload === null) {
      return;
    }

    els.linkSubmit.disabled = true;
    els.linkSubmit.textContent = automaticRetry ? "登录成功，正在重试..." : "提交中...";
    try {
      const body = Object.assign({ url: url, mode: mode, use_mock: useMock }, llmPayload);
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
      els.startQrLogin.textContent = "登录/刷新抖音 Cookies";
      els.videoUrl.value = url;
      els.linkMode.value = mode;
      setCookieMessage(
        automaticRetry ? "Cookies 已刷新，链接任务已自动重新提交。" : "链接任务已提交。",
        "ok",
      );
      startPolling(data.task_id);
    } catch (error) {
      setTaskStatus("failed", `提交失败：${error.message}`);
    } finally {
      els.linkSubmit.disabled = false;
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

  /** 加载最近 50 条任务。 */
  async function loadHistory() {
    try {
      const tasks = await fetchJson("/tasks?limit=50&view=summary", {}, 10000);
      renderHistory(tasks);
    } catch (error) {
      els.historyList.innerHTML = "";
      const empty = document.createElement("div");
      empty.className = "history-empty";
      empty.textContent = "历史任务加载失败。";
      els.historyList.appendChild(empty);
      els.historyMessage.textContent = error.message;
    }
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
    return MODE_TEXT[mode] || mode || "任务";
  }

  /** 渲染历史任务列表。 */
  function renderHistory(tasks) {
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
      const del = document.createElement("button");
      del.type = "button";
      del.className = "history-delete";
      del.textContent = "删除";
      del.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteHistoryTask(task.task_id);
      });
      meta.append(time, del);

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
      } else if ([401, 403].includes(error.status)) {
        els.taskMessage.textContent = "登录或访问令牌已失效，请重新验证。";
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
      if (state.currentTaskId === taskId) {
        stopPolling();
        state.currentTaskId = null;
        state.currentTask = null;
        rememberTask(null);
        updateAgentContext();
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

  /** 拉取 /cookies/status 并刷新两处状态徽章。 */
  async function refreshCookieStatus() {
    try {
      const summary = await fetchJson("/cookies/status", {}, 8000);
      const badge = cookieBadge(summary);
      els.cookieState.className = `badge ${badge.cls}`;
      els.cookieState.textContent = badge.text;
      els.sidebarCookieStatus.className = `badge ${badge.cls}`;
      els.sidebarCookieStatus.textContent = badge.text;
      return summary;
    } catch (error) {
      els.cookieState.className = "badge badge-danger";
      els.cookieState.textContent = "检查失败";
      els.sidebarCookieStatus.className = "badge badge-danger";
      els.sidebarCookieStatus.textContent = "检查失败";
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
    window.clearInterval(state.qrTimer);
    state.qrTimer = null;
    state.qrSessionId = null;
    state.qrAttempts = 0;
  }

  function setQrMessage(message) {
    els.qrMessage.hidden = false;
    els.qrMessage.textContent = message;
  }

  /** 启动扫码登录会话并轮询状态。 */
  async function startQrLogin() {
    stopQrPolling();
    els.qrImage.hidden = true;
    els.qrImage.removeAttribute("src");
    setQrMessage("正在启动扫码登录窗口...");

    try {
      const session = await fetchJson("/cookies/qr/start", { method: "POST" }, 15000);
      state.qrSessionId = session.session_id;
      state.qrAttempts = 0;
      // 后端当前基于专用浏览器窗口，仅在返回二维码图片地址时展示图片
      const imageUrl = session.qr_image_url || session.qrcode_url || (session.qr && session.qr.image_url);
      if (imageUrl) {
        els.qrImage.src = imageUrl;
        els.qrImage.hidden = false;
      }
      setQrMessage(session.message || "请在打开的窗口中扫码登录，本页会自动检测。");
      state.qrTimer = window.setInterval(pollQrLogin, QR_POLL_INTERVAL_MS);
    } catch (error) {
      setQrMessage(`启动失败：${error.message}`);
    }
  }

  /** 轮询扫码登录状态直至完成或超时。 */
  async function pollQrLogin() {
    if (!state.qrSessionId) {
      return;
    }
    state.qrAttempts += 1;
    if (state.qrAttempts > QR_POLL_MAX_ATTEMPTS) {
      stopQrPolling();
      setQrMessage("扫码检测已暂停（超过 5 分钟），如需继续请重新点击「扫码登录」。");
      return;
    }
    try {
      const status = await fetchJson(`/cookies/qr/${state.qrSessionId}/status`, {}, 10000);
      if (status.completed) {
        stopQrPolling();
        els.qrImage.hidden = true;
        setQrMessage(status.message || "扫码登录成功，Cookies 已保存。");
        setCookieMessage(status.message || "扫码登录成功，Cookies 已保存。", "ok");
        await refreshCookieStatus();
        await retryPendingCookieTask();
        return;
      }
      setQrMessage(status.message || "等待扫码确认...");
    } catch (error) {
      stopQrPolling();
      setQrMessage(`检测失败：${error.message}`);
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
    els.sendChatBtn.disabled = busy;
    els.sendChatBtn.textContent = busy ? "思考中..." : "发送";
    els.promptChips.querySelectorAll(".chip-btn").forEach((chip) => {
      chip.disabled = busy;
    });
  }

  /**
   * 发送问题到后端 /tasks/{id}/chat。
   * 未配置用户自己的 API Key 时，先提示去「模型设置」配置，不发起对话。
   * @param {string} rawQuestion 用户问题
   */
  async function sendChat(rawQuestion) {
    if (state.chatController) return;
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
      if (task) {
        state.currentTask = task;
        state.currentTaskId = task.task_id;
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

    appendBubble("user", question);
    els.chatInput.value = "";
    const thinking = appendBubble("agent", "思考中...", { thinking: true });
    setChatBusy(true);
    const controller = new AbortController();
    state.chatController = controller;
    let answer = "";
    let renderTimer = null;
    const render = () => {
      renderTimer = null;
      if (!thinking.isConnected) return;
      thinking.classList.remove("is-thinking");
      thinking.innerHTML = renderMarkdown(answer);
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
              { question: question },
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
          return event === "done";
        },
        controller,
        130000,
      );
    } catch (error) {
      if (!thinking.isConnected) return;
      thinking.classList.remove("is-thinking");
      const message = error.name === "AbortError" ? "连接超时，请稍后重试。" : error.message;
      thinking.innerHTML = renderMarkdown(answer ? `${answer}\n\n生成中断：${message}` : `提问失败：${message}`);
      thinking.parentElement.classList.add("is-error");
      scrollChatToBottom();
    } finally {
      window.clearTimeout(renderTimer);
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
    deepseek: { baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat", protocol: "chat_completions" },
    openai: { baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini", protocol: "chat_completions" },
    custom: { baseUrl: "", model: "", protocol: "chat_completions" },
  };

  /** 读取本地保存的模型配置；未配置返回 null。 */
  function loadModelConfig() {
    try {
      const raw = localStorage.getItem(MODEL_CONFIG_KEY);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      return parsed && parsed.apiKey ? parsed : null;
    } catch (error) {
      return null;
    }
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

  /** 保存表单配置到 localStorage。 */
  function saveModelConfig() {
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
    try {
      localStorage.setItem(
        MODEL_CONFIG_KEY,
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
      setModelMessage(`连接失败：${error.message}`, "error");
    } finally {
      els.testModelConfig.disabled = false;
      els.testModelConfig.textContent = originalText;
    }
  }

  /** 清除本地模型配置，恢复服务端默认。 */
  function clearModelConfig() {
    try {
      localStorage.removeItem(MODEL_CONFIG_KEY);
    } catch (error) {
      // 忽略清除失败
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
    fillModelForm(loadModelConfig());
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
      const cookieState = health.cookies_file
        ? "cookies 文件已配置"
        : health.cookies_from_browser
          ? `浏览器 cookies：${health.cookies_from_browser}`
          : "未配置 cookies";
      els.statusDot.classList.remove("is-offline");
      els.serviceState.textContent = `服务正常 · 默认 ${health.mock_mode ? "Mock" : "真实"} 模式 · ${cookieState}`;

      accountLoginEnabled = Boolean(health.login_required);
      document.getElementById("accountLogout").hidden = !accountLoginEnabled;
      if (accountLoginEnabled) {
        const account = await fetchJson("/auth/me");
        if (!account.authenticated) window.location.replace("/login");
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
      item.addEventListener("click", () => {
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
    els.verifyCookies.addEventListener("click", async () => {
      setCookieMessage("正在检查本地 Cookies...");
      const data = await refreshCookieStatus();
      if (data) {
        setCookieMessage(`${describeCookieSummary(data)}。`);
      } else {
        setCookieMessage("检查 Cookies 失败，请确认服务已启动。", "error");
      }
    });
    els.uploadCookieFile.addEventListener("click", uploadCookieFile);
    els.startQrLogin.addEventListener("click", startQrLogin);
    els.openCookieBrowser.addEventListener("click", openCookieBrowser);
    els.checkCookieBrowser.addEventListener("click", checkCookieBrowser);
    els.captureCookieBrowser.addEventListener("click", captureCookieBrowser);

    els.sendChatBtn.addEventListener("click", () => sendChat(els.chatInput.value));
    els.newChatBtn.addEventListener("click", resetCurrentSession);
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
      try {
        await fetchJson("/auth/logout", { method: "POST" });
        localStorage.removeItem(AUTH_TOKEN_KEY);
        window.location.replace("/login");
      } catch (error) {
        window.alert(`退出失败：${error.message}`);
      }
    });
    bindThemeToggle();
  }

  /** 页面初始化。 */
  async function boot() {
    const initialWatchVersion = state.watchVersion;
    bindEvents();
    updateAgentContext();
    await refreshServiceState();
    try {
      const config = await fetchJson("/config");
      if (config.llm.configured) {
        serverModelConfig = { provider: config.llm.provider, baseUrl: config.llm.base_url, model: config.llm.model, protocol: config.llm.protocol };
        els.llmApiKey.placeholder = "留空使用服务器默认模型";
        fillModelForm(loadModelConfig());
        renderModelState();
        setModelMessage("服务器默认模型已就绪，可直接分析和追问。填写自己的 Key 可覆盖默认模型。", "ok");
        updateAgentContext();
      }
    } catch (error) { /* Service state already reports connection errors. */ }
    await Promise.allSettled([refreshCookieStatus(), loadHistory()]);
    try {
      const saved = JSON.parse(localStorage.getItem(ACTIVE_TASK_KEY) || "null");
      if (saved && saved.clientId === getClientId() && saved.taskId && state.watchVersion === initialWatchVersion) {
        await openHistoryTask(saved.taskId);
      }
    } catch (error) { /* Storage may be disabled or contain an older value. */ }
  }

  boot();
})();
