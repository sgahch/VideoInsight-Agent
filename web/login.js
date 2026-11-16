(() => {
  "use strict";
  const { post, message, clearLegacyToken, codeSender } = AccountAuth;
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
  const resetForm = document.getElementById("resetForm");
  const resetEmail = document.getElementById("resetEmail");
  const resetError = document.getElementById("resetError");
  const resetFields = document.getElementById("resetFields");
  const success = document.getElementById("authSuccess");
  let registrationAvailable = false;
  let passwordAvailable = false;
  let mode = location.pathname === "/forgot-password" ? "reset" : location.pathname === "/register" ? "register" : "login";

  function completedLogin() {
    clearLegacyToken();
    window.location.replace("/");
  }

  function showForm(next) {
    mode = next === "register" && !registrationAvailable ? "login" : next;
    const register = mode === "register", reset = mode === "reset";
    form.hidden = mode !== "login";
    registerForm.hidden = !register;
    resetForm.hidden = !reset;
    document.querySelector(".account-tabs").hidden = reset;
    document.querySelector(".login-session-note").hidden = reset;
    loginTab.setAttribute("aria-selected", String(mode === "login"));
    registerTab.setAttribute("aria-selected", String(register));
    loginTab.tabIndex = mode === "login" ? 0 : -1;
    registerTab.tabIndex = register ? 0 : -1;
    document.getElementById("login-title").textContent = reset ? "重置密码" : register ? "创建账号" : "欢迎回来";
    document.getElementById("accountNote").textContent = reset ? "验证安全邮箱，设置新的登录密码。" : register ? "验证邮箱，开启你的视频工作台。" : "登录账号，继续探索视频中的价值。";
    document.title = `${reset ? "重置密码" : register ? "注册" : "登录"} · VideoInsight Agent`;
    history.replaceState(null, "", reset ? "/forgot-password" : register ? "/register" : "/login");
  }

  loginTab.addEventListener("click", () => { success.textContent = ""; showForm("login"); });
  registerTab.addEventListener("click", () => { success.textContent = ""; showForm("register"); });
  document.getElementById("forgotPassword").addEventListener("click", event => {
    event.preventDefault();
    success.textContent = "";
    const username = form.elements.username.value.trim();
    if (username.includes("@")) resetEmail.value = username;
    showForm("reset");
    resetEmail.focus();
  });
  document.getElementById("backToLogin").addEventListener("click", event => {
    event.preventDefault();
    showForm("login");
    form.elements.username.focus();
  });
  for (const tab of [loginTab, registerTab]) {
    tab.addEventListener("keydown", event => {
      if (!registrationAvailable || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const register = tab === loginTab;
      showForm(register ? "register" : "login");
      (register ? registerTab : loginTab).focus();
    });
  }

  codeSender({
    button: sendCode, endpoint: "/auth/register/code", email,
    validate: () => email.reportValidity(),
    payload: () => ({ email: email.value.trim() }),
    status: codeStatus, error: registerError, code: document.getElementById("registerCode"),
  });
  codeSender({
    button: document.getElementById("sendResetCode"), endpoint: "/auth/password/reset/code", email: resetEmail,
    validate: () => passwordAvailable && resetEmail.reportValidity(),
    payload: () => ({ email: resetEmail.value.trim() }),
    status: document.getElementById("resetCodeStatus"), error: resetError, code: document.getElementById("resetCode"),
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
      registerError.textContent = message(failure);
    } finally {
      registerSubmit.disabled = false;
      registerSubmit.textContent = "注册并登录 →";
    }
  });
  resetForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (!passwordAvailable) return;
    resetError.textContent = "";
    const nextPassword = document.getElementById("resetPassword");
    const confirm = document.getElementById("resetConfirmPassword");
    if (nextPassword.value !== confirm.value) {
      resetError.textContent = "两次输入的密码不一致。";
      confirm.focus();
      return;
    }
    const submitReset = document.getElementById("resetSubmit");
    const loginEmail = resetEmail.value.trim();
    resetFields.disabled = true;
    submitReset.textContent = "正在重置...";
    try {
      const data = await post("/auth/password/reset", {
        email: loginEmail, code: document.getElementById("resetCode").value.trim(), password: nextPassword.value,
      });
      clearLegacyToken();
      resetForm.reset();
      form.elements.username.value = loginEmail;
      password.value = "";
      success.textContent = data.message;
      showForm("login");
      password.focus();
    } catch (failure) {
      resetError.textContent = message(failure);
    } finally {
      resetFields.disabled = !passwordAvailable;
      submitReset.textContent = "重置密码";
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
      error.textContent = message(failure);
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
      passwordAvailable = account.password_reset_available === true;
      resetFields.disabled = !passwordAvailable;
      if (!passwordAvailable) resetError.textContent = "邮箱验证暂不可用，请联系管理员。";
      registerTab.hidden = !registrationAvailable;
      showForm(mode);
    })
    .catch(() => {
      registerTab.hidden = true;
      resetError.textContent = "无法读取账号服务状态，请刷新页面重试。";
    });
  if (mode === "reset") showForm(mode);
})();
