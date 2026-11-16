// Read-only navigation checks against an authenticated production session.
async (page) => {
  const base = new URL(page.url()).origin;
  const errors = [];
  const layouts = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.unrouteAll({ behavior: "wait" });
  const me = await page.request.get(base + "/auth/me").then(response => response.json());
  if (!me.authenticated) throw new Error("An authenticated test session is required");
  const role = me.is_admin ? "admin" : "user";
  await page.goto(base + "/#cookies");
  await page.reload();
  await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
  await page.waitForFunction(() => !["检查中...", "检查失败"].includes(document.querySelector("#cookieState").textContent));
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const cookies = page.locator('nav a[href="#cookies"]');
    const settings = page.locator('nav a[href="#settings"]');
    if (!await cookies.isVisible() || !await settings.isVisible()) throw new Error("Navigation is hidden");
    if (width === 390) {
      await page.locator(".notion-sidebar").screenshot({ path: `output/playwright/cookie-access-live-nav-${role}.png` });
    }
    await cookies.click();
    const metrics = await page.evaluate(() => {
      const panel = document.querySelector("#cookies").getBoundingClientRect();
      const nav = document.querySelector('nav a[href="#cookies"]').getBoundingClientRect();
      return { width: innerWidth, document: document.documentElement.scrollWidth,
        left: panel.left, right: panel.right, top: panel.top, navHeight: nav.height };
    });
    if (metrics.document > width || metrics.left < 0 || metrics.right > width ||
        metrics.top < -2 || metrics.top > 900 || (width < 960 && metrics.navHeight < 44)) {
      throw new Error("Production layout failure: " + JSON.stringify(metrics));
    }
    if (!await page.locator("#cookieAdminControls").isVisible() ||
        await page.locator("#cookieReadOnly").isVisible()) {
      throw new Error("Production role controls are incorrect");
    }
    if (!await page.locator("#startQrLogin").isVisible()) throw new Error("Account QR action missing");
    if (!me.is_admin && !(await page.locator("#cookieAuthHint").textContent()).includes("仅当前账号使用")) {
      throw new Error("Private authorization hint missing");
    }
    await page.locator("#cookies").screenshot({ path: `output/playwright/cookie-access-live-${role}-${width}.png` });
    await settings.click();
    const top = await page.locator("#settings").evaluate(el => el.getBoundingClientRect().top);
    if (top < -2 || top > 900) throw new Error("Production model settings navigation failed");
    layouts.push(metrics);
  }
  if (!me.is_admin) {
    const response = await page.request.post(base + "/cookies/browser/start");
    if (response.status() !== 403) throw new Error("Shared desktop administrator guard failed");
  }
  if (errors.length) throw new Error(errors.join("; "));
  return { role, realServerResponses: true, layouts, errors };
}
