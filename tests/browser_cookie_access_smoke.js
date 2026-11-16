// Use only the isolated password_browser_server.py fixture; no real accounts or SMTP.
async (page) => {
  const base = "http://127.0.0.1:8003";
  const errors = [];
  const layouts = [];
  page.on("pageerror", error => errors.push(error.message));
  const modes = {
    remote: { mode: "remote", qr_available: true, desktop_available: false },
    desktop: { mode: "desktop", qr_available: false, desktop_available: true },
    import: { mode: "import", qr_available: false, desktop_available: false },
  };
  let mode = "remote", failure = false;
  await page.unrouteAll({ behavior: "wait" });
  await page.context().clearCookies();
  await page.route("**/cookies/status", route => route.fulfill(failure
    ? { status: 503, json: { detail: "fixture unavailable" } }
    : { json: { exists: true, count: 3, has_login_cookie: true, authorization: modes[mode] } }));
  await page.goto(base + "/register");
  await page.locator("#registerEmail").fill(`cookie-access-${Date.now()}@example.com`);
  await page.locator("#sendCode").click();
  await page.waitForFunction(() => document.querySelector("#codeStatus").textContent.includes("验证码"));
  await page.locator("#registerCode").fill("123456");
  await page.locator("#registerPassword").fill("browser-cookie-access-user");
  await page.locator("#confirmPassword").fill("browser-cookie-access-user");
  await page.locator("#registerSubmit").click();
  await page.waitForURL(base + "/");

  async function checkLayout(role, width) {
    await page.setViewportSize({ width, height: 900 });
    await page.locator('nav a[href="#cookies"]').click();
    await page.locator("#cookieState").filter({ hasText: "3" }).waitFor();
    const layout = await page.evaluate(() => {
      const ids = ["cookies", "cookieAuthHint", "cookieMessage"];
      const controls = ["#cookieReadOnly", "#cookieAdminControls"];
      const visible = selector => !!document.querySelector(selector).getClientRects().length;
      const nav = document.querySelector('nav a[href="#cookies"]').getBoundingClientRect();
      const panel = document.getElementById("cookies").getBoundingClientRect();
      return {
        viewport: innerWidth, document: document.documentElement.scrollWidth,
        navHeight: nav.height, panelTop: panel.top,
        admin: visible(controls[1]), readOnly: visible(controls[0]),
        bounds: ids.map(id => {
          const rect = document.getElementById(id).getBoundingClientRect();
          return { id, left: rect.left, right: rect.right };
        }),
      };
    });
    if (layout.document > width || layout.bounds.some(b => b.left < 0 || b.right > width) ||
        layout.panelTop < -2 || layout.panelTop > 900 ||
        (width < 960 && layout.navHeight < 44) ||
        !layout.admin || layout.readOnly) {
      throw new Error("Cookie access layout failure: " + JSON.stringify({ role, ...layout }));
    }
    if (role === "user") {
      if (!await page.locator("#startQrLogin").isVisible() || !await page.locator("#cookieImport").isVisible()) {
        throw new Error("Private authorization controls are missing");
      }
      if (!(await page.locator("#cookieAuthHint").textContent()).includes("仅当前账号使用")) {
        throw new Error("Account ownership hint is missing");
      }
      if ((await page.locator("#startQrLogin").boundingBox()).height < 44) throw new Error("QR touch target is too small");
    }
    await page.locator("#cookies").screenshot({ path: `output/playwright/cookie-access-${role}-${width}.png` });
    await page.locator('nav a[href="#settings"]').click();
    const settingsTop = await page.locator("#settings").evaluate(el => el.getBoundingClientRect().top);
    if (settingsTop < -2 || settingsTop > 900) throw new Error("Settings navigation failed");
    layouts.push({ role, ...layout });
  }
  for (mode of Object.keys(modes)) {
    await page.reload();
    await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
    await page.locator("#cookieState").filter({ hasText: "3" }).waitFor();
    if (await page.locator("#cookieReadOnly").isVisible()) throw new Error("User shown shared authorization");
    if (await page.locator("#startQrLogin").isVisible() !== (mode !== "import")) throw new Error("User mode mismatch");
  }
  mode = "remote";
  await page.reload();
  for (const width of [320, 390, 768, 1440]) await checkLayout("user", width);
  if ((await page.request.post(base + "/cookies/browser/start")).status() !== 403) throw new Error("Shared desktop was exposed");
  const uploaded = await page.request.post(base + "/cookies", {
    multipart: { file: { name: "cookies.txt", mimeType: "text/plain", buffer: Buffer.from("sessionid=fixture-private") } },
  });
  if (uploaded.status() !== 200) throw new Error("Private cookie import denied");
  if ((await page.request.get(base + "/cookies/status")).status() !== 200) throw new Error("Read-only status denied");
  failure = true;
  await page.locator("#cookieImportSummary").click();
  await page.locator("#verifyCookies").click();
  await page.waitForFunction(() => document.querySelector("#cookieState").textContent.includes("检查失败"));
  if (await page.locator("#verifyCookies").isDisabled()) throw new Error("Failed refresh stayed disabled");
  failure = false;

  await page.locator("#accountLogout").click();
  await page.waitForURL(base + "/login");
  await page.locator("#username").fill("browser-admin");
  await page.locator("#password").fill("browser-admin-password");
  await page.locator("#loginSubmit").click();
  await page.waitForURL(base + "/");
  mode = "remote";
  await page.goto(base + "/#cookies");
  await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
  for (const width of [320, 390, 768, 1440]) {
    await checkLayout("admin", width);
    if (!await page.locator("#startQrLogin").isVisible()) throw new Error("Admin QR missing");
  }
  for (mode of ["desktop", "import"]) {
    await page.reload();
    await page.locator("#cookieAdminControls").waitFor({ state: "visible" });
    await page.locator("#cookieState").filter({ hasText: "3" }).waitFor();
    if (await page.locator("#cookieReadOnly").isVisible()) throw new Error("Admin shown read-only notice");
    if (await page.locator("#startQrLogin").isVisible() !== (mode === "desktop")) throw new Error("Admin mode mismatch");
    if (await page.locator("#cookieImport").getAttribute("open") === null && mode === "import") {
      throw new Error("Import-only authorization was collapsed");
    }
  }
  if (errors.length) throw new Error("Browser errors: " + errors.join("; "));
  return { normalUserModes: 3, adminModes: 3, privateImport: true, sharedDesktopForbidden: true, layouts, errors };
}
