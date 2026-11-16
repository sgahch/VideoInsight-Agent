(() => {
  "use strict";

  async function post(path, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        credentials: "same-origin", cache: "no-store",
        body: JSON.stringify(payload), signal: controller.signal,
      });
      const data = await response.json().catch(() => null);
      if (!data) throw new Error("服务器响应异常，请稍后重试。");
      if (!response.ok) {
        const error = new Error(typeof data.detail === "string" ? data.detail : "请检查填写内容后重试。");
        error.status = response.status;
        error.retryAfter = Number(response.headers.get("Retry-After")) || 0;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  function message(error) {
    if (error instanceof TypeError) return "网络连接中断，请检查网络后重试。";
    return ["AbortError", "TimeoutError"].includes(error.name)
      ? "请求超时，请先检查邮箱或稍后重试。"
      : error.message || "暂时无法连接服务器。";
  }

  function clearLegacyToken() {
    try { localStorage.removeItem("videoinsight_auth_token"); } catch (_) { /* HttpOnly sessions do not need localStorage. */ }
  }

  function codeSender({ button, endpoint, payload, validate, status, error, code, email, key }) {
    const deadlines = new Map();
    let pending = false;
    const recipient = () => email ? email.value.trim().toLowerCase() : key();
    function update() {
      const seconds = Math.max(0, Math.ceil(((deadlines.get(recipient()) || 0) - Date.now()) / 1000));
      button.disabled = pending || seconds > 0;
      button.textContent = pending ? "发送中..." : seconds ? `${seconds} 秒后重发` : "获取验证码";
    }
    const timer = setInterval(update, 1000);
    window.addEventListener("pageshow", update);
    window.addEventListener("pagehide", event => { if (!event.persisted) clearInterval(timer); });
    if (email) email.addEventListener("input", () => {
      code.value = "";
      status.textContent = "";
      error.textContent = "";
      update();
    });
    button.addEventListener("click", async () => {
      if (pending || !validate()) return;
      const target = recipient();
      if ((deadlines.get(target) || 0) > Date.now()) return;
      pending = true;
      status.textContent = "";
      error.textContent = "";
      update();
      try {
        const data = await post(endpoint, payload());
        deadlines.set(target, Date.now() + (data.retry_after || 60) * 1000);
        if (target === recipient()) {
          status.textContent = data.message;
          code.focus();
        }
      } catch (failure) {
        if (failure.retryAfter || ["AbortError", "TimeoutError"].includes(failure.name)) {
          deadlines.set(target, Date.now() + Math.min(86400, failure.retryAfter || 60) * 1000);
        }
        if (target === recipient()) error.textContent = message(failure);
      } finally {
        pending = false;
        update();
      }
    });
    return { update };
  }

  for (const button of document.querySelectorAll("[data-toggle-password]")) {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.togglePassword);
      const visible = input.type === "password";
      input.type = visible ? "text" : "password";
      button.textContent = visible ? "隐藏" : "显示";
      button.setAttribute("aria-pressed", String(visible));
    });
  }
  globalThis.AccountAuth = { post, message, clearLegacyToken, codeSender };
})();
