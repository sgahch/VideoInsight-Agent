// Run with playwright-cli run-code --filename against an authenticated server.
async (page) => {
  await page.unrouteAll({ behavior: "wait" });
  const failures = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await page.locator("#startQrLogin").waitFor({ state: "visible" });
  const started = Date.now();
  const response = page.waitForResponse((item) =>
    item.url().endsWith("/cookies/qr/start") && item.request().method() === "POST");
  await page.locator("#startQrLogin").click();
  const session = await (await response).json();
  if (!session.session_id) throw new Error("Real QR session did not start");
  try {
    await page.waitForFunction(() => {
      const image = document.querySelector("#qrImage");
      return image && !image.hidden && image.complete && image.naturalWidth >= 128;
    }, null, { timeout: 90000 });
    const loadedInMs = Date.now() - started;
    const pixels = await page.locator("#qrImage").evaluate((image) => {
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext("2d");
      context.drawImage(image, 0, 0);
      const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let dark = 0, light = 0;
      for (let index = 0; index < data.length; index += 4) {
        if (data[index] < 50) dark++;
        if (data[index] > 220) light++;
      }
      return { width: canvas.width, height: canvas.height, dark, light };
    });
    if (pixels.dark < 1000 || pixels.light < 1000) throw new Error("QR pixels are blank");
    const downloading = page.waitForEvent("download");
    await page.locator("#saveQrImage").click();
    const download = await downloading;
    if (download.suggestedFilename() !== "douyin-login.png") throw new Error("QR download filename mismatch");
    await download.saveAs("output/playwright/qr-live-download.png");
    const sizes = [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 320, height: 740 }];
    const viewports = [];
    for (const size of sizes) {
      await page.setViewportSize(size);
      await page.locator("#cookies").scrollIntoViewIfNeeded();
      const layout = await page.locator("#cookies").evaluate((panel) => {
        const image = panel.querySelector("#qrImage").getBoundingClientRect();
        const actions = [...panel.querySelectorAll(".qr-login-actions .mini-btn")]
          .map((button) => button.getBoundingClientRect().height);
        return { viewport: innerWidth, width: document.documentElement.scrollWidth,
          imageLeft: image.left, imageRight: image.right, imageWidth: image.width, actions };
      });
      if (layout.width > size.width || layout.imageLeft < 0 || layout.imageRight > size.width ||
          layout.actions.some((height) => height < 44)) {
        throw new Error("QR layout overflow: " + JSON.stringify(layout));
      }
      await page.locator("#cookies").screenshot({ path: `output/playwright/qr-live-${size.width}.png` });
      viewports.push(layout);
    }
    if (failures.length) throw new Error(failures.join("; "));
    const device = await page.evaluate(() => ({ userAgent: navigator.userAgent, touchPoints: navigator.maxTouchPoints }));
    return { realPlatformQr: true, loadedInMs, pixels, viewports, device,
      download: true, phoneConfirmation: "not performed" };
  } finally {
    const headers = await page.evaluate(() => ({
      "X-Client-ID": localStorage.getItem("videoinsight_client_id"),
      "Origin": location.origin,
    }));
    const cancelled = await page.request.delete(new URL(`/cookies/qr/${session.session_id}`, page.url()).href, { headers });
    if (!cancelled.ok()) throw new Error("Real QR session cleanup failed: " + cancelled.status());
  }
}
