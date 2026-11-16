// Isolated UI fixtures. Real platform loading is checked by browser_qr_live.js.
async (page) => {
  const base = page.url().startsWith("http") ? new URL(page.url()).origin : "http://127.0.0.1:8002";
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const image = await page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 256;
    const context = canvas.getContext("2d");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, 256, 256);
    context.fillStyle = "#000";
    for (let row = 0; row < 16; row++) {
      for (let column = 0; column < 16; column++) {
        if ((row + column) % 2) context.fillRect(row * 16, column * 16, 16, 16);
      }
    }
    return canvas.toDataURL("image/png");
  });
  let phase = "starting", sequence = 0, reads = 0, cancelled = 0, saved = false, disconnect = false;
  await page.route("**/cookies/status", (route) => route.fulfill({ json: {
    exists: saved, count: saved ? 2 : 0, has_login_cookie: saved,
    authorization: { mode: "remote", qr_available: true, desktop_available: false },
  } }));
  await page.route("**/cookies/qr/**", async (route) => {
    const request = route.request();
    if (request.method() === "DELETE") {
      cancelled++;
      return route.fulfill({ json: { status: "cancelled" } });
    }
    if (request.method() === "POST") {
      sequence++;
      return route.fulfill({ json: { session_id: "fixture-" + sequence, status: "starting", expires_in: 180 } });
    }
    reads++;
    if (disconnect) {
      disconnect = false;
      return route.fulfill({ status: 503, json: { detail: "test disconnect" } });
    }
    if (phase === "confirmed") saved = true;
    return route.fulfill({ json: {
      session_id: "fixture-" + sequence, status: phase, completed: phase === "confirmed", expires_in: 120,
      qr_image_url: ["waiting", "scanned", "confirming"].includes(phase) ? image : null,
      message: { starting: "正在加载", waiting: "等待扫码", scanned: "已扫码，请在手机上确认",
        confirming: "正在核验登录结果", confirmed: "登录成功，已自动保存", expired: "二维码已过期" }[phase],
    } });
  });
  const resume = () => page.evaluate(() => window.dispatchEvent(new Event("pageshow")));
  await page.goto(base + "/#cookies");
  await page.reload();
  await page.locator("#startQrLogin").waitFor({ state: "visible" });
  const started = Date.now();
  await page.locator("#startQrLogin").click();
  await page.waitForFunction(() => document.querySelector("#qrMessage").textContent === "正在加载");
  if (!reads || Date.now() - started > 2500) throw new Error("First poll was not immediate");
  phase = "waiting";
  await resume();
  await page.waitForFunction(() => document.querySelector("#qrImage").naturalWidth === 256);
  const layouts = [];
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.locator("#cookies").scrollIntoViewIfNeeded();
    const layout = await page.locator("#cookies").evaluate((panel) => {
      const buttons = [...panel.querySelectorAll(".qr-login-actions .mini-btn")].map((button) => ({
        height: button.getBoundingClientRect().height, width: button.getBoundingClientRect().width,
      }));
      const image = panel.querySelector("#qrImage").getBoundingClientRect();
      return { viewport: innerWidth, document: document.documentElement.scrollWidth,
        imageLeft: image.left, imageRight: image.right, buttons };
    });
    if (layout.document > width || layout.imageLeft < 0 || layout.imageRight > width ||
        layout.buttons.some((button) => button.height < 44 || button.width < 44)) {
      throw new Error("Mobile layout or touch target failure: " + JSON.stringify(layout));
    }
    layouts.push(layout);
  }
  const downloadEvent = page.waitForEvent("download");
  await page.locator("#saveQrImage").click();
  const download = await downloadEvent;
  if (download.suggestedFilename() !== "douyin-login.png") throw new Error("QR download filename mismatch");
  await download.saveAs("output/playwright/qr-fixture-download.png");
  disconnect = true;
  await resume();
  await page.waitForFunction(() => document.querySelector("#qrMessage").textContent.includes("自动重连"));
  if (!await page.locator("#qrImage").isVisible()) throw new Error("Transient error removed a valid QR");
  phase = "scanned";
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.waitForFunction(() => document.querySelector("#qrMessage").textContent.includes("已扫码"));
  if (saved) throw new Error("Scanning alone was mistaken for confirmed login");
  phase = "confirming";
  await resume();
  await page.waitForFunction(() => document.querySelector("#qrMessage").textContent.includes("核验"));
  if (saved || (await page.locator("#cookieMessage").textContent()).includes("已自动保存")) {
    throw new Error("Pending confirmation was mistaken for saved login");
  }
  phase = "confirmed";
  await resume();
  await page.waitForFunction(() => document.querySelector("#cookieMessage").textContent.includes("已自动保存"));
  if (await page.locator("#saveQrImage").isVisible()) throw new Error("Completed QR remained downloadable");
  if (await page.locator("#saveQrImage").getAttribute("href")) throw new Error("Completed QR URL was retained");
  phase = "expired";
  await page.locator("#startQrLogin").click();
  await page.waitForFunction(() => document.querySelector("#startQrLogin").textContent === "刷新二维码");
  if (await page.locator("#qrImage").isVisible()) throw new Error("Expired QR still visible");
  phase = "waiting";
  await page.locator("#startQrLogin").click();
  await page.waitForFunction(() => !document.querySelector("#qrImage").hidden);
  await page.locator("#cancelQrLogin").click();
  if (cancelled !== 1) throw new Error("Cancellation did not reach the server");
  if (await page.locator("#saveQrImage").getAttribute("href")) throw new Error("Cancelled QR URL was retained");
  if (errors.length) throw new Error(errors.join("; "));
  return { fixtureTests: true, immediatePoll: true, download: true, reconnect: true,
    foregroundResume: true, confirmingFeedback: true, confirmedOnly: true, expiry: true, cancellation: true, layouts };
}
