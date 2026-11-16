// Run only against tests/password_browser_server.py; SMTP and accounts are synthetic.
async (page) => {
  const base = "http://127.0.0.1:8003";
  await page.context().clearCookies();
  const email = `browser-${Date.now()}@example.com`;
  const oldPassword = "browser-user-original";
  const changedPassword = "browser-user-changed";
  const resetPassword = "browser-user-recovered";
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const fill = (id, value) => page.locator("#" + id).fill(value);
  const click = id => page.locator("#" + id).click();
  const hasText = (id, text) => page.waitForFunction(
    ({ id, text }) => document.getElementById(id).textContent.includes(text), { id, text });
  const layouts = [];
  async function checkLayouts(label, selector, targets) {
    for (const size of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
      await page.setViewportSize(size);
      const layout = await page.evaluate(ids => ({
        viewport: innerWidth, width: document.documentElement.scrollWidth,
        targets: ids.map(id => {
          const rect = document.getElementById(id).getBoundingClientRect();
          return { id, left: rect.left, right: rect.right, height: rect.height };
        }),
      }), targets);
      if (layout.width > size.width || layout.targets.some(t => t.left < 0 || t.right > size.width || t.height < 44)) {
        throw new Error("Password UI overflow or touch target failure: " + JSON.stringify(layout));
      }
      await page.locator(selector).screenshot({ path: `output/playwright/password-${label}-${size.width}.png` });
      layouts.push({ label, ...layout });
    }
  }
  await page.goto(base + "/register");
  await page.locator("#registerForm").waitFor({ state: "visible" });
  await fill("registerEmail", email);
  await click("sendCode");
  await hasText("codeStatus", "验证码");
  await fill("registerCode", "123456");
  await fill("registerPassword", oldPassword);
  await fill("confirmPassword", oldPassword);
  await click("registerSubmit");
  await page.waitForURL(base + "/");
  await page.locator("#accountSecurity").waitFor({ state: "visible" });
  await click("accountSecurity");
  await page.waitForURL(base + "/account");
  await page.locator("#changeSection").waitFor({ state: "visible" });
  await fill("changeCurrentPassword", "wrong-password");
  await click("sendChangeCode");
  await hasText("changeError", "当前密码不正确");
  await fill("changeCurrentPassword", oldPassword);
  await click("sendChangeCode");
  await hasText("changeCodeStatus", "验证码已发送");
  await fill("changeCode", "123456");
  await fill("changeNewPassword", changedPassword);
  await fill("changeConfirmPassword", "does-not-match");
  await click("changeSubmit");
  await hasText("changeError", "不一致");
  await checkLayouts("account", ".account-main", ["sendChangeCode", "changeSubmit"]);
  await fill("changeConfirmPassword", changedPassword);
  await click("changeSubmit");
  await page.locator("#passwordSuccess").waitFor({ state: "visible" });
  if ((await page.request.get(base + "/tasks")).status() !== 401) throw new Error("Password change kept old session");
  await page.locator("#passwordSuccess a").click();
  await page.waitForURL(base + "/login");
  await fill("username", email);
  await fill("password", changedPassword);
  await click("loginSubmit");
  await page.waitForURL(base + "/");
  await page.locator("#accountLogout").waitFor({ state: "visible" });
  await click("accountLogout");
  await page.waitForURL(base + "/login");
  await click("forgotPassword");
  await page.waitForURL(base + "/forgot-password");
  await fill("resetEmail", email);
  await click("sendResetCode");
  await hasText("resetCodeStatus", "验证码");
  if (!await page.locator("#sendResetCode").isDisabled()) throw new Error("Resend cooldown missing");
  await fill("resetCode", "000000");
  await fill("resetPassword", resetPassword);
  await fill("resetConfirmPassword", resetPassword);
  await click("resetSubmit");
  await hasText("resetError", "验证码错误");
  await checkLayouts("forgot", ".login-card", ["sendResetCode", "resetSubmit", "backToLogin"]);
  await fill("resetCode", "123456");
  await click("resetSubmit");
  await page.waitForURL(base + "/login");
  await hasText("authSuccess", "密码已更新");
  if (await page.locator("#username").inputValue() !== email) throw new Error("Recovered email was not kept for login");
  await fill("password", resetPassword);
  await click("loginSubmit");
  await page.waitForURL(base + "/");
  await page.locator("#accountLogout").waitFor({ state: "visible" });
  await click("accountLogout");
  await page.waitForURL(base + "/login");
  await fill("username", "browser-admin");
  await fill("password", "browser-admin-password");
  await click("loginSubmit");
  await page.waitForURL(base + "/");
  await page.locator("#accountSecurity").waitFor({ state: "visible" });
  await click("accountSecurity");
  await page.locator("#bindingSection").waitFor({ state: "visible" });
  await fill("bindingCurrentPassword", "browser-admin-password");
  await fill("bindingEmail", `owner-${Date.now()}@example.com`);
  await click("sendBindingCode");
  await hasText("bindingCodeStatus", "验证码已发送");
  await fill("bindingCode", "123456");
  await checkLayouts("binding", ".account-main", ["sendBindingCode", "bindingSubmit"]);
  await click("bindingSubmit");
  await page.locator("#changeSection").waitFor({ state: "visible" });
  await hasText("accountStatus", "安全邮箱已绑定");
  await page.reload();
  await page.locator("#changeSection").waitFor({ state: "visible" });
  if (await page.locator("#bindingSection").isVisible()) throw new Error("Email binding did not persist");
  await fill("changeCurrentPassword", "browser-admin-password");
  await click("sendChangeCode");
  await hasText("changeCodeStatus", "验证码已发送");
  await fill("changeCode", "123456");
  await fill("changeNewPassword", "browser-admin-updated");
  await fill("changeConfirmPassword", "browser-admin-updated");
  await click("changeSubmit");
  await page.locator("#passwordSuccess").waitFor({ state: "visible" });
  if ((await page.request.get(base + "/tasks")).status() !== 401) throw new Error("Administrator old session survived");
  if (errors.length) throw new Error(errors.join("; "));
  return { syntheticSmtp: true, registration: true, passwordChange: true, passwordReset: true,
    adminBinding: true, adminPasswordChange: true, oldSessionsInvalidated: true, layouts };
}
