// Isolated fixture only. The launch event is intercepted, not sent to the OS.
async (page) => {
  const base = "http://127.0.0.1:8003";
  const errors = [];
  const layouts = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.unrouteAll({ behavior: "wait" });
  await page.context().clearCookies();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "platform", { get: () => "Win32", configurable: true });
    window.addEventListener("videoinsight:helper-launch", event => {
      event.preventDefault();
      window.testHelperLaunch = event.detail.url;
    });
  });
  const email = `helper-ui-${Date.now()}@example.com`;
  await page.request.post(base + "/auth/register/code", { data: { email } });
  const registration = await page.request.post(base + "/auth/register", {
    data: { email, code: "123456", password: "helper-ui-password" },
  });
  if (registration.status() !== 201) throw new Error("Isolated registration failed");
  await page.goto(base + "/#cookies");
  await page.locator("#localHelperControls").waitFor({ state: "visible" });
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.locator('nav a[href="#cookies"]').click();
    const layout = await page.evaluate(() => ({
      width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      bounds: ["localHelperControls", "startLocalLogin", "downloadLocalHelper", "localHelperMessage"].map(id => {
        const box = document.getElementById(id).getBoundingClientRect();
        return { id, left: box.left, right: box.right, height: box.height };
      }),
    }));
    if (layout.scrollWidth > width || layout.bounds.some(box => box.left < -1 || box.right > width + 1)) {
      throw new Error("Helper layout overflow: " + JSON.stringify(layout));
    }
    if (layout.bounds.find(box => box.id === "startLocalLogin").height < 44) throw new Error("Small login target");
    await page.locator("#cookies").screenshot({ path: `output/playwright/helper-ui-user-${width}.png` });
    layouts.push(layout);
  }
  const download = await page.request.get(base + "/cookies/helper/download");
  if (download.status() !== 200 || (await download.body()).subarray(0, 2).toString() !== "MZ") {
    throw new Error("Helper download is not a Windows executable");
  }

  async function launch() {
    await page.locator("#startLocalLogin").click();
    await page.waitForFunction(() => window.testHelperLaunch && !document.getElementById("reopenLocalHelper").hidden);
    const url = await page.evaluate(() => {
      const value = window.testHelperLaunch;
      window.testHelperLaunch = null;
      return value;
    });
    const token = new URL(url).searchParams.get("token");
    const storage = await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]));
    if (storage.includes(token)) throw new Error("Handoff credential persisted in browser storage");
    const claim = await page.request.post(base + "/cookies/helper/claim", {
      headers: { Authorization: "Bearer " + token },
    });
    if (claim.status() !== 200) throw new Error("Helper claim failed");
    return await claim.json();
  }
  let claim = await launch();
  await page.waitForFunction(() => document.getElementById("localHelperMessage").textContent.includes("等待抖音登录"));
  await page.reload();
  await page.waitForFunction(() => document.getElementById("localHelperMessage").textContent.includes("等待抖音登录"));
  const saved = await page.request.post(base + "/cookies/helper/complete", {
    headers: { Authorization: "Bearer " + claim.upload_token },
    data: { cookies: [{ name: "sessionid", value: "synthetic-ui-only", domain: ".douyin.com", path: "/", httpOnly: true }] },
  });
  if (saved.status() !== 200) throw new Error("Automatic save failed");
  await page.waitForFunction(() => document.getElementById("localHelperMessage").textContent.includes("登录成功"));
  await page.waitForFunction(() => document.getElementById("cookieState").textContent.includes("已登录"));
  if (!(await page.locator("#cookieMessage").textContent()).includes("当前账号")) throw new Error("Save feedback missing");

  claim = await launch();
  await page.locator("#cancelLocalLogin").click();
  await page.waitForFunction(() => document.getElementById("localHelperMessage").textContent.includes("已取消"));
  const stale = await page.request.post(base + "/cookies/helper/complete", {
    headers: { Authorization: "Bearer " + claim.upload_token },
    data: { cookies: [{ name: "sessionid", value: "stale-ui-only", domain: ".douyin.com", path: "/" }] },
  });
  if (stale.status() !== 401) throw new Error("Cancelled helper saved Cookies");
  claim = await launch();
  await page.request.post(base + "/cookies/helper/fail", {
    headers: { Authorization: "Bearer " + claim.upload_token, "X-Login-Result": "failed" },
  });
  await page.waitForFunction(() => document.getElementById("localHelperMessage").textContent.includes("未完成"));

  await page.request.post(base + "/auth/logout");
  const login = await page.request.post(base + "/auth/login", {
    data: { username: "browser-admin", password: "browser-admin-password" },
  });
  if (login.status() !== 200) throw new Error("Fixture admin login failed");
  await page.goto(base + "/#cookies");
  await page.locator("#localHelperControls").waitFor({ state: "visible" });
  await page.locator("#cookies").screenshot({ path: "output/playwright/helper-ui-admin-1440.png" });

  await page.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", { get: () => "Android Mobile Chrome", configurable: true });
    Object.defineProperty(navigator, "platform", { get: () => "Linux armv8l", configurable: true });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
  if (await page.locator("#localHelperControls").isVisible()) throw new Error("Windows helper shown to phone");
  await page.locator("#cookies").screenshot({ path: "output/playwright/helper-ui-mobile-390.png" });
  if (errors.length) throw new Error("Browser errors: " + errors.join("; "));
  return { success: true, layouts, reloadResume: true, cancelledUploadRejected: true, mobileFallback: true };
}
