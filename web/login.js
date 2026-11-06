(() => {
  "use strict";
  const form = document.getElementById("loginForm");
  const password = document.getElementById("password");
  const toggle = document.getElementById("togglePassword");
  const submit = document.getElementById("loginSubmit");
  const error = document.getElementById("loginError");
  toggle.addEventListener("click", () => {
    const visible = password.type === "password";
    password.type = visible ? "text" : "password";
    toggle.textContent = visible ? "隐藏" : "显示";
    toggle.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
    toggle.setAttribute("aria-pressed", String(visible));
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    submit.disabled = true;
    submit.textContent = "正在登录…";
    try {
      const response = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: form.elements.username.value.trim(), password: password.value }),
        signal: AbortSignal.timeout(15000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "登录失败，请检查账号密码。");
      try { localStorage.removeItem("videoinsight_auth_token"); } catch (_) { /* Cookie works without localStorage. */ }
      window.location.replace("/");
    } catch (failure) {
      error.textContent = failure.name === "TimeoutError" ? "连接超时，请稍后重试。" : failure.message || "暂时无法连接服务器。";
      password.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = "登录工作台 →";
    }
  });
})();
