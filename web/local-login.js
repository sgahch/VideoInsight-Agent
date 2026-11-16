(function () {
  "use strict";

  const byId = id => document.getElementById(id);
  const controls = byId("localHelperControls");
  const start = byId("startLocalLogin");
  const download = byId("downloadLocalHelper");
  const reopen = byId("reopenLocalHelper");
  const cancelButton = byId("cancelLocalLogin");
  const secureLink = byId("helperSecureLink");
  const message = byId("localHelperMessage");
  const windows = /Win/i.test(navigator.userAgentData?.platform || navigator.platform) &&
    !/Android|iPhone|iPad/i.test(navigator.userAgent);
  const storageKey = "videoinsight.local-login";
  let config = null, sessionId = null, timer = null, failures = 0, polling = false;
  let pendingRequest = false, lastStatus = null, startedAt = 0;

  function say(text, error = false) {
    message.textContent = text;
    message.className = "cookie-message" + (error ? " is-error" : "");
  }

  async function request(path, method = "GET") {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const result = await fetch(path, { method, credentials: "same-origin", cache: "no-store", signal: controller.signal });
      const body = await result.json();
      if (!result.ok) {
        const error = new Error(typeof body.detail === "string" ? body.detail : "授权请求失败");
        error.status = result.status;
        throw error;
      }
      return body;
    } finally {
      clearTimeout(timeout);
    }
  }

  function remember() {
    try {
      if (sessionId) sessionStorage.setItem(storageKey, JSON.stringify({ sessionId, username: config?.username }));
      else sessionStorage.removeItem(storageKey);
    } catch (_) {}
  }

  function stop() {
    clearTimeout(timer);
    timer = null;
    sessionId = null;
    failures = 0;
    cancelButton.hidden = true;
    reopen.hidden = true;
    reopen.removeAttribute("href");
    remember();
    start.disabled = !config?.available;
    start.textContent = "打开抖音登录窗口";
  }

  function launch(value) {
    const url = new URL(value);
    const server = url.searchParams.get("server");
    if (url.protocol !== "videoinsight-login:" || url.hostname !== "authorize" || server !== location.origin) {
      throw new Error("助手授权链接异常，已停止打开。");
    }
    const event = new CustomEvent("videoinsight:helper-launch", { detail: { url: value }, cancelable: true });
    if (window.dispatchEvent(event)) location.assign(value);
  }

  function schedule() {
    clearTimeout(timer);
    if (sessionId) timer = setTimeout(poll, Math.min(30000, 2500 * 2 ** Math.min(failures, 4)));
  }

  async function poll() {
    if (!sessionId || polling) return;
    const id = sessionId;
    polling = true;
    try {
      const result = await request(`/cookies/helper/${encodeURIComponent(id)}/status`);
      if (id !== sessionId) return;
      failures = 0;
      lastStatus = result.status;
      if (result.completed && result.status === "confirmed") {
        stop();
        say("抖音登录成功，Cookies 已保存到当前账号。");
        window.dispatchEvent(new CustomEvent("videoinsight:helper-saved"));
      } else if (["cancelled", "expired", "failed"].includes(result.status)) {
        stop();
        say({
          cancelled: "本次本地授权已取消。",
          expired: "本次授权已过期，请重新打开登录窗口。",
          failed: "本地登录未完成，请查看助手窗口后重试。",
        }[result.status], result.status !== "cancelled");
      } else {
        say(result.status === "claimed"
          ? `等待抖音登录，授权将保存到 ${config?.username || "当前账号"}。`
          : Date.now() - startedAt > 20000
            ? "尚未连接到助手，请确认已安装并允许浏览器打开助手。"
            : "等待本地助手连接…");
      }
    } catch (error) {
      if (id !== sessionId) return;
      failures += 1;
      if ([401, 403, 404].includes(error.status)) {
        stop();
        say("授权会话不可用，请重新登录网站后再试。", true);
      } else if (failures >= 5) {
        say("连接暂时中断，正在等待服务器恢复；授权结果将自动重新检查。", true);
      } else {
        say("连接暂时中断，正在重新检查授权结果…");
      }
    } finally {
      polling = false;
      if (sessionId) schedule();
    }
  }

  async function cancel() {
    if (!sessionId) return;
    const id = sessionId;
    await request(`/cookies/helper/${encodeURIComponent(id)}`, "DELETE");
    if (id === sessionId) {
      stop();
      say("本次本地授权已取消。");
    }
  }

  start.addEventListener("click", async () => {
    if (pendingRequest || !config?.available) return;
    pendingRequest = true;
    start.disabled = true;
    try {
      if (sessionId) await cancel();
      window.dispatchEvent(new CustomEvent("videoinsight:helper-starting"));
      const result = await request("/cookies/helper/start", "POST");
      sessionId = result.session_id;
      startedAt = Date.now();
      lastStatus = "pending";
      failures = 0;
      remember();
      reopen.href = result.launch_url;
      reopen.hidden = false;
      cancelButton.hidden = false;
      say("等待本地助手连接…");
      schedule();
      launch(result.launch_url);
    } catch (error) {
      say(`打开失败：${error.message}`, true);
    } finally {
      pendingRequest = false;
      start.disabled = !config?.available;
      start.textContent = sessionId ? "重新开始授权" : "打开抖音登录窗口";
    }
  });

  reopen.addEventListener("click", event => {
    event.preventDefault();
    try { launch(reopen.href); }
    catch (error) { say(error.message, true); }
  });
  cancelButton.addEventListener("click", () => {
    cancelButton.disabled = true;
    cancel().catch(error => say(`取消失败：${error.message}`, true))
      .finally(() => { cancelButton.disabled = false; });
  });

  function resume() {
    if (document.visibilityState === "visible" && sessionId) {
      clearTimeout(timer);
      failures = 0;
      void poll();
    }
  }
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("pageshow", resume);
  window.addEventListener("online", resume);

  window.VideoInsightLocalLogin = {
    cancel,
    cleared() { stop(); say("本账号的抖音登录态已清除。"); },
    render(value) {
      config = value;
      controls.hidden = !windows || !value || !value.username || !value.download_url;
      start.disabled = pendingRequest || !value?.available;
      download.hidden = !value?.secure;
      secureLink.hidden = value?.secure !== false;
      if (value?.secure === false) {
        secureLink.href = value.secure_url;
        say("请切换到 HTTPS 后使用本地助手。", true);
      }
      if (value?.available && !sessionId && lastStatus === null) {
        try {
          const saved = JSON.parse(sessionStorage.getItem(storageKey));
          if (saved?.username === value.username && /^[a-f0-9]{32}$/.test(saved.sessionId)) {
            sessionId = saved.sessionId;
            startedAt = Date.now() - 20001;
            cancelButton.hidden = false;
            void poll();
          } else {
            sessionStorage.removeItem(storageKey);
          }
        } catch (_) {}
      }
      const qr = byId("startQrLogin");
      if (windows && value?.available &&
          ["登录/刷新抖音 Cookies", "登录并自动重试该链接"].includes(qr.textContent)) {
        qr.textContent = "改用页内扫码";
      }
    },
  };
})();
