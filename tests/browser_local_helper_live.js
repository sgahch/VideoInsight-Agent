// Authenticated production check. Never approve a real Douyin login or replace Cookies.
async (page) => {
  const errors = [], layouts = [];
  const base = new URL(page.url()).origin;
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "platform", { get: () => "Win32", configurable: true });
    window.addEventListener("videoinsight:helper-launch", event => {
      event.preventDefault();
      window.testHelperLaunch = event.detail.url;
    });
  });
  await page.reload();
  await page.locator("#localHelperControls").waitFor({ state: "visible" });
  const account = await (await page.request.get(base + "/auth/me")).json();
  if (!account.authenticated) throw new Error("Production fixture is not authenticated");
  const role = account.is_admin ? "admin" : "user";
  const before = await (await page.request.get(base + "/cookies/status")).json();
  if (!before.local_helper.available || !before.local_helper.secure) throw new Error("HTTPS helper unavailable");
  if (before.scope !== (account.is_admin ? "administrator" : "account")) throw new Error("Wrong Cookie scope");
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.locator('nav a[href="#cookies"]').click();
    const layout = await page.evaluate(() => ({
      width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      bounds: ["cookies", "localHelperControls", "startLocalLogin", "downloadLocalHelper"].map(id => {
        const box = document.getElementById(id).getBoundingClientRect();
        return { id, left: box.left, right: box.right, height: box.height };
      }),
    }));
    if (layout.scrollWidth > width || layout.bounds.some(b => b.left < -1 || b.right > width + 1)) {
      throw new Error("Production helper overflow: " + JSON.stringify(layout));
    }
    if (layout.bounds.find(b => b.id === "startLocalLogin").height < 44) throw new Error("Small primary action");
    await page.locator("#cookies").screenshot({ path: `output/playwright/helper-live-${role}-${width}.png` });
    layouts.push(layout);
  }
  await page.locator("#startLocalLogin").click();
  await page.waitForFunction(() => window.testHelperLaunch && !document.getElementById("cancelLocalLogin").hidden);
  const safeLaunch = await page.evaluate(() => {
    const url = new URL(window.testHelperLaunch);
    window.testHelperLaunch = null;
    return url.protocol === "videoinsight-login:" && url.searchParams.get("server") === location.origin;
  });
  if (!safeLaunch) throw new Error("Production helper launch origin mismatch");
  await page.locator("#cancelLocalLogin").click();
  await page.waitForFunction(() => document.getElementById("localHelperMessage").textContent.includes("已取消"));
  const after = await (await page.request.get(base + "/cookies/status")).json();
  if (before.count !== after.count || before.has_login_cookie !== after.has_login_cookie) {
    throw new Error("Starting and cancelling the helper altered existing authorization");
  }
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", { get: () => "Android Mobile Chrome", configurable: true });
    Object.defineProperty(navigator, "platform", { get: () => "Linux armv8l", configurable: true });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
  if (await page.locator("#localHelperControls").isVisible()) throw new Error("Windows helper shown to phone");
  if (!await page.locator("#startQrLogin").isVisible()) throw new Error("Mobile QR fallback missing");
  await page.locator("#cookies").screenshot({ path: `output/playwright/helper-live-${role}-mobile-390.png` });
  if (errors.length) throw new Error(errors.join("; "));
  return { role, https: true, startAndCancel: true, existingCookiesPreserved: true, mobileQrFallback: true, layouts };
}
