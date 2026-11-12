(() => {
  "use strict";
  const form = document.getElementById("loginForm");
  const password = document.getElementById("password");
  const toggle = document.getElementById("togglePassword");
  const submit = document.getElementById("loginSubmit");
  const error = document.getElementById("loginError");
  const registerForm = document.getElementById("registerForm");
  const registerTab = document.getElementById("registerTab");
  const loginTab = document.getElementById("loginTab");
  const registerError = document.getElementById("registerError");
  const registerSubmit = document.getElementById("registerSubmit");
  const sendCode = document.getElementById("sendCode");
  const email = document.getElementById("registerEmail");
  const codeStatus = document.getElementById("codeStatus");
  let cooldownTimer = null;
  let registrationAvailable = false;

  async function post(path, payload) {
    const response = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal: AbortSignal.timeout(20000),
    });
    const data = await response.json();
    if (!response.ok) {
      const failure = new Error(typeof data.detail === "string" ? data.detail : "请检查填写内容后重试。");
      failure.retryAfter = Number(response.headers.get("Retry-After")) || 0;
      throw failure;
    }
    return data;
  }

  function completedLogin() {
    try { localStorage.removeItem("videoinsight_auth_token"); } catch (_) { /* Cookie works without localStorage. */ }
    window.location.replace("/");
  }

  function showForm(register) {
    register = register && registrationAvailable;
    form.hidden = register;
    registerForm.hidden = !register;
    loginTab.setAttribute("aria-selected", String(!register));
    registerTab.setAttribute("aria-selected", String(register));
    loginTab.tabIndex = register ? -1 : 0;
    registerTab.tabIndex = register ? 0 : -1;
    document.getElementById("login-title").textContent = register ? "创建账号" : "欢迎回来";
    document.getElementById("accountNote").textContent = register ? "验证邮箱，开启你的视频工作台。" : "登录账号，继续探索视频中的价值。";
    document.title = `${register ? "注册" : "登录"} · VideoInsight Agent`;
    history.replaceState(null, "", register ? "/register" : "/login");
  }

  loginTab.addEventListener("click", () => showForm(false));
  registerTab.addEventListener("click", () => showForm(true));
  for (const tab of [loginTab, registerTab]) {
    tab.addEventListener("keydown", event => {
      if (!registrationAvailable || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const register = tab === loginTab;
      showForm(register);
      (register ? registerTab : loginTab).focus();
    });
  }

  function cooldown(seconds) {
    window.clearInterval(cooldownTimer);
    const until = Date.now() + Math.min(86400, Math.max(1, seconds)) * 1000;
    const update = () => {
      const remaining = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      sendCode.disabled = remaining > 0;
      sendCode.textContent = remaining ? `${remaining} 秒后重发` : "获取验证码";
      if (!remaining) window.clearInterval(cooldownTimer);
    };
    update();
    cooldownTimer = window.setInterval(update, 1000);
  }

  sendCode.addEventListener("click", async () => {
    if (!email.reportValidity()) return;
    sendCode.disabled = true;
    sendCode.textContent = "发送中...";
    registerError.textContent = "";
    codeStatus.textContent = "";
    try {
      const data = await post("/auth/register/code", { email: email.value.trim() });
      codeStatus.textContent = data.message;
      cooldown(data.retry_after || 60);
      document.getElementById("registerCode").focus();
    } catch (failure) {
      registerError.textContent = failure.name === "TimeoutError" ? "发送超时，请稍后检查邮箱或重试。" : failure.message || "发送失败，请稍后重试。";
      if (failure.retryAfter) cooldown(failure.retryAfter);
      else {
        sendCode.disabled = false;
        sendCode.textContent = "获取验证码";
      }
    }
  });

  registerForm.addEventListener("submit", async event => {
    event.preventDefault();
    registerError.textContent = "";
    const newPassword = document.getElementById("registerPassword");
    const confirm = document.getElementById("confirmPassword");
    if (newPassword.value !== confirm.value) {
      registerError.textContent = "两次输入的密码不一致。";
      confirm.focus();
      return;
    }
    registerSubmit.disabled = true;
    registerSubmit.textContent = "正在注册...";
    try {
      await post("/auth/register", {
        email: email.value.trim(), code: document.getElementById("registerCode").value.trim(),
        password: newPassword.value,
      });
      completedLogin();
    } catch (failure) {
      registerError.textContent = failure.name === "TimeoutError" ? "连接超时，请稍后尝试登录。" : failure.message || "注册失败，请稍后重试。";
    } finally {
      registerSubmit.disabled = false;
      registerSubmit.textContent = "注册并登录 →";
    }
  });
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
      await post("/auth/login", { username: form.elements.username.value.trim(), password: password.value });
      completedLogin();
    } catch (failure) {
      error.textContent = failure.name === "TimeoutError" ? "连接超时，请稍后重试。" : failure.message || "暂时无法连接服务器。";
      password.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = "登录工作台 →";
    }
  });
  fetch("/auth/me", { cache: "no-store", signal: AbortSignal.timeout(8000) })
    .then(response => response.ok ? response.json() : Promise.reject())
    .then(account => {
      registrationAvailable = account.registration_available === true;
      registerTab.hidden = !registrationAvailable;
      showForm(location.pathname === "/register");
    })
    .catch(() => { registerTab.hidden = true; });
})();
