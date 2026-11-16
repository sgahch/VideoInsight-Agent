// Read-only production checks. Never request an OTP, bind an email, or change a real password.
async (page) => {
  await page.goto("http://206.187.222.21/forgot-password");
  await page.locator("#resetForm").waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.getElementById("resetFields").disabled);
  const layouts = [];
  for (const size of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 320, height: 740 }]) {
    await page.setViewportSize(size);
    const layout = await page.evaluate(() => ({
      viewport: innerWidth, width: document.documentElement.scrollWidth,
      controls: ["sendResetCode", "resetSubmit", "backToLogin"].map(id => {
        const rect = document.getElementById(id).getBoundingClientRect();
        return { id, left: rect.left, right: rect.right, height: rect.height };
      }),
    }));
    if (layout.width > size.width || layout.controls.some(rect => rect.left < 0 || rect.right > size.width || rect.height < 44)) {
      throw new Error("Production recovery layout failed: " + JSON.stringify(layout));
    }
    await page.locator(".login-card").screenshot({ path: `output/playwright/password-production-forgot-${size.width}.png` });
    layouts.push(layout);
  }
  await page.locator("#backToLogin").click();
  await page.locator("#forgotPassword").waitFor({ state: "visible" });
  return { production: true, passwordEmailAvailable: true, forgotButton: true,
    noEmailSent: true, noRealPasswordChanged: true, layouts };
}
