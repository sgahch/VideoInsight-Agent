// Run against the isolated Mock fixture, never against a production account.
async (page) => {
  const base = "http://127.0.0.1:8003";
  await page.context().clearCookies();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "platform", { get: () => "Win32", configurable: true });
    window.addEventListener("videoinsight:helper-launch", event => event.preventDefault());
  });
  const email = `readme-${Date.now()}@example.com`;
  await page.request.post(base + "/auth/register/code", { data: { email } });
  const registration = await page.request.post(base + "/auth/register", {
    data: { email, code: "123456", password: "readme-fixture-password" },
  });
  if (registration.status() !== 201) throw new Error("Screenshot fixture registration failed");
  const submit = await page.request.post(base + "/analyze", {
    data: { url: "https://www.bilibili.com/video/BV1xx411c7mD", mode: "summary", use_mock: true },
  });
  if (submit.status() !== 202) throw new Error("Mock example submission failed");
  const task = await submit.json();
  for (let attempt = 0; attempt < 50; attempt++) {
    const response = await page.request.get(base + "/tasks/" + task.task_id);
    const result = await response.json();
    if (result.status === "succeeded") break;
    if (result.status === "failed") throw new Error("Mock example failed");
    await page.waitForTimeout(200);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base + "/");
  await page.locator("#accountLogout").waitFor({ state: "visible" });
  await page.waitForTimeout(600);
  await page.screenshot({ path: "output/playwright/readme-workspace-desktop.png" });
  await page.locator('nav a[href="#cookies"]').click();
  await page.locator("#localHelperControls").waitFor({ state: "visible" });
  await page.screenshot({ path: "output/playwright/readme-cookies-desktop.png" });
  await page.goto(base + "/account");
  await page.locator("#changeSection").waitFor({ state: "visible" });
  await page.setViewportSize({ width: 650, height: 1050 });
  await page.locator(".account-main").screenshot({ path: "output/playwright/readme-account-security.png" });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", { get: () => "Android Mobile Chrome", configurable: true });
    Object.defineProperty(navigator, "platform", { get: () => "Linux armv8l", configurable: true });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base + "/#cookies");
  await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
  if (await page.locator("#localHelperControls").isVisible()) throw new Error("Windows helper shown on mobile");
  await page.locator('nav a[href="#cookies"]').click();
  await page.locator("#cookies").screenshot({ path: "output/playwright/readme-cookies-mobile.png" });
  return { isolated: true, mock: true, screenshots: 4 };
}
