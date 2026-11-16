(() => {
  "use strict";
  const { post, message, clearLegacyToken, codeSender } = AccountAuth;
  const $ = id => document.getElementById(id);
  let account = null;

  function render() {
    $("accountPanel").hidden = false;
    $("accountUsername").textContent = account.username;
    $("accountEmail").textContent = account.email || "尚未绑定";
    $("bindingSection").hidden = !account.email_binding_required;
    $("changeSection").hidden = account.email_binding_required;
    $("bindingFields").disabled = !account.password_reset_available;
    $("changeFields").disabled = !account.password_reset_available;
    if (!account.password_reset_available) {
      $("accountStatus").textContent = "邮箱验证暂不可用，请联系管理员。";
    }
  }

  codeSender({
    button: $("sendBindingCode"), endpoint: "/auth/email/bind/code", email: $("bindingEmail"),
    validate: () => account?.password_reset_available && $("bindingCurrentPassword").reportValidity() && $("bindingEmail").reportValidity(),
    payload: () => ({ email: $("bindingEmail").value.trim(), current_password: $("bindingCurrentPassword").value }),
    status: $("bindingCodeStatus"), error: $("bindingError"), code: $("bindingCode"),
  });
  codeSender({
    button: $("sendChangeCode"), endpoint: "/auth/password/change/code", key: () => account?.email || "",
    validate: () => account?.password_reset_available && $("changeCurrentPassword").reportValidity(),
    payload: () => ({ current_password: $("changeCurrentPassword").value }),
    status: $("changeCodeStatus"), error: $("changeError"), code: $("changeCode"),
  });

  $("bindingForm").addEventListener("submit", async event => {
    event.preventDefault();
    $("bindingError").textContent = "";
    $("bindingFields").disabled = true;
    $("bindingSubmit").textContent = "正在验证...";
    try {
      const data = await post("/auth/email/bind", {
        email: $("bindingEmail").value.trim(), code: $("bindingCode").value.trim(),
        current_password: $("bindingCurrentPassword").value,
      });
      account.email = data.email;
      account.email_binding_required = false;
      $("bindingForm").reset();
      $("accountStatus").textContent = data.message;
      render();
      $("changeCurrentPassword").focus();
    } catch (failure) {
      $("bindingError").textContent = message(failure);
      if (failure.status === 401) window.location.replace("/login");
    } finally {
      $("bindingFields").disabled = !account?.password_reset_available;
      $("bindingSubmit").textContent = "验证并绑定邮箱";
    }
  });

  $("changePasswordForm").addEventListener("submit", async event => {
    event.preventDefault();
    $("changeError").textContent = "";
    if ($("changeNewPassword").value !== $("changeConfirmPassword").value) {
      $("changeError").textContent = "两次输入的密码不一致。";
      $("changeConfirmPassword").focus();
      return;
    }
    $("changeFields").disabled = true;
    $("changeSubmit").textContent = "正在修改...";
    try {
      const data = await post("/auth/password/change", {
        current_password: $("changeCurrentPassword").value,
        code: $("changeCode").value.trim(), password: $("changeNewPassword").value,
      });
      clearLegacyToken();
      $("changePasswordForm").reset();
      $("changeSection").hidden = true;
      $("passwordSuccess").hidden = false;
      $("accountStatus").textContent = data.message;
      $("passwordSuccess").scrollIntoView({ block: "nearest" });
      $("passwordSuccess a").focus();
    } catch (failure) {
      $("changeError").textContent = message(failure);
      if (failure.status === 401) window.location.replace("/login");
    } finally {
      $("changeFields").disabled = !account?.password_reset_available;
      $("changeSubmit").textContent = "验证并修改密码";
    }
  });

  async function load() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/auth/me", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("无法读取账号，请刷新页面重试。");
      account = await response.json();
      if (!account.authenticated) {
        window.location.replace("/login");
        return;
      }
      $("accountStatus").textContent = "";
      render();
    } catch (failure) {
      $("accountStatus").textContent = message(failure);
    } finally {
      clearTimeout(timeout);
    }
  }
  void load();
})();
