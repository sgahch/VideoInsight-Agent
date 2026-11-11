// Run against the isolated server on 8001 with playwright-cli run-code --filename.
async (page) => {
  const base = "http://127.0.0.1:8001";
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.unrouteAll({ behavior: "wait" });
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: base });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector("#poolCore").textContent === "2");
  const headers = await page.evaluate(() => ({
    "X-Client-Id": localStorage.getItem("videoinsight_client_id"),
    Authorization: `Bearer ${localStorage.getItem("videoinsight_auth_token") || ""}`,
  }));
  const ids = [];
  const taskUrl = (id) => `${base}/tasks/${id}`;
  const history = (id) => page.locator(`.history-mini-item[data-task-id="${id}"]`);
  const waitForHistory = (id, text) => page.waitForFunction(({ id, text }) =>
    document.querySelector(`.history-mini-item[data-task-id="${id}"]`)?.textContent.includes(text), { id, text });
  try {
    for (let index = 0; index < 3; index++) {
      const response = await page.request.post(`${base}/analyze`, {
        headers, data: { url: `https://example.com/usage-${index}`, label: `复制标记回归 ${index + 1}`, use_mock: true, with_comments: false },
      });
      if (response.status() !== 202) throw new Error(`Mock submission failed: ${await response.text()}`);
      ids.push((await response.json()).task_id);
    }
    await waitForHistory(ids[2], "已完成");
    const open = async (id) => {
      await history(id).click();
      await page.waitForFunction((id) => document.querySelector("#taskId").textContent === id &&
        document.querySelector("#taskStatusBadge").textContent === "已完成", id);
    };

    await open(ids[0]);
    await page.locator("#copyPanelBtn").click();
    await page.waitForFunction(() => !document.querySelector("#taskUsedBadge").hidden);
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    if (!clipboard.includes("视频总结")) throw new Error("The summary was not copied to the real clipboard");
    const usedAt = (await (await page.request.get(taskUrl(ids[0]), { headers })).json()).used_at;
    if (!usedAt) throw new Error("Usage was not persisted");
    await waitForHistory(ids[0], "已使用");
    await page.reload();
    await page.waitForFunction(() => !document.querySelector("#taskUsedBadge").hidden);
    await page.locator("#backToQueue").click();
    await page.waitForFunction((id) => document.querySelector(`.queue-task-row[data-task-id="${id}"] .task-used-badge`), ids[0]);

    await open(ids[1]);
    await page.evaluate(() => {
      window.__originalClipboard = navigator.clipboard;
      window.__originalExecCommand = document.execCommand;
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
        writeText: async () => { throw new Error("Simulated clipboard denial"); },
      } });
      document.execCommand = () => false;
    });
    await page.locator("#copyPanelBtn").click();
    await page.waitForFunction(() => document.querySelector("#copyActionMessage").textContent.includes("复制失败"));
    if (await page.locator("#taskUsedBadge").isVisible()) throw new Error("Failed copy marked a task as used");
    if ((await (await page.request.get(taskUrl(ids[1]), { headers })).json()).used_at) throw new Error("Failed copy persisted usage");
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: window.__originalClipboard });
      document.execCommand = window.__originalExecCommand;
    });

    await page.route(`**/tasks/${ids[1]}/used`, (route) => route.fulfill({ status: 503, json: { detail: "Simulated save failure" } }));
    await page.locator("#copyPanelBtn").click();
    await page.waitForFunction(() => document.querySelector("#copyActionMessage").textContent.includes("标记保存失败"));
    if (await page.locator("#taskUsedBadge").isVisible()) throw new Error("Failed save showed a persistent badge");
    await page.unroute(`**/tasks/${ids[1]}/used`);

    // A delayed clipboard operation must keep the task that initiated it.
    await page.evaluate(() => {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
        writeText: () => new Promise((resolve) => { window.__finishCopy = resolve; }),
      } });
    });
    await page.locator("#copyPanelBtn").click();
    await page.waitForFunction(() => typeof window.__finishCopy === "function");
    await open(ids[2]);
    await page.evaluate(() => window.__finishCopy());
    await waitForHistory(ids[1], "已使用");
    if (await page.locator("#taskUsedBadge").isVisible()) throw new Error("Copy marked the newly selected task");
    if ((await (await page.request.get(taskUrl(ids[2]), { headers })).json()).used_at) throw new Error("Usage leaked across tasks");
    if (!(await page.locator("#copyActionMessage").isHidden())) throw new Error("A different task's copy feedback leaked");
    await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: window.__originalClipboard }));
    await page.locator('[data-target="transcriptOutput"]').click();
    await page.locator("#copyTranscriptBtn").click();
    await page.waitForFunction(() => !document.querySelector("#taskUsedBadge").hidden);
    await page.screenshot({ path: "output/playwright/usage-detail-desktop.png", fullPage: true });
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error("Usage badge overflow on details");
      await page.locator("#taskUsedBadge").scrollIntoViewIfNeeded();
      await page.screenshot({ path: `output/playwright/usage-detail-${width}.png` });
    }
    await page.locator("#backToQueue").click();
    await waitForHistory(ids[2], "已使用");

    const layouts = [];
    for (const theme of ["dark", "light"]) {
      if (await page.evaluate(() => document.documentElement.dataset.theme) !== theme) await page.locator("#themeToggle").click();
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`Usage badge overflow at ${width}, ${theme}`);
        await page.screenshot({ path: `output/playwright/usage-queue-${theme}-${width}.png`, fullPage: true });
        layouts.push({ width, theme });
      }
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    for (const id of ids) {
      const response = await page.request.delete(taskUrl(id), { headers });
      if (response.status() !== 204) throw new Error("Could not remove isolated mock task");
    }
    ids.length = 0;

    // Explicit browser-only fixtures for automatic status updates, never real tasks.
    const fixtures = [
      { task_id: "history-updating", status: "queued", title: "历史状态自动更新测试" },
      { task_id: "history-detail", status: "succeeded", title: "当前查看的其他视频" },
    ].map((task) => ({
      ...task, request: { url: "https://www.bilibili.com/video/test", mode: "summary" },
      created_at: "2026-11-06T20:00:00", updated_at: "2026-11-06T20:00:00", used_at: null,
    }));
    let queueCalls = 0;
    let failQueue = false;
    let legacyHistoryCalls = 0;
    page.on("request", (request) => { if (new URL(request.url()).pathname === "/tasks") legacyHistoryCalls++; });
    await page.route("**/queue?*", (route) => {
      queueCalls++;
      if (failQueue) return route.fulfill({ status: 503, json: { detail: "Simulated queue outage" } });
      const counts = { running: 0, queued: 0, succeeded: 0, failed: 0 };
      fixtures.forEach((task) => counts[task.status]++);
      return route.fulfill({ json: {
        tasks: fixtures, counts,
        pool: { core_workers: 2, max_workers: 4, active_workers: counts.running, queued: counts.queued, queue_capacity: 32 },
        engine: { asr_global_workers: 8 },
      } });
    });
    await page.route("**/tasks/history-detail", (route) => route.fulfill({ json: {
      ...fixtures[1], result: {
        mode: "summary", transcript: "A ready transcript", output: "Ready summary", title: fixtures[1].title,
        segments: [], comments: [], media: { source_url: fixtures[1].request.url, title: fixtures[1].title },
      },
    } }));
    await page.goto(base);
    await waitForHistory(fixtures[0].task_id, "排队中");
    fixtures[0].status = "running";
    fixtures[0].title = "AI 生成的新标题";
    await waitForHistory(fixtures[0].task_id, "处理中");
    await waitForHistory(fixtures[0].task_id, "AI 生成的新标题");
    await history(fixtures[1].task_id).click();
    await page.waitForFunction(() => document.querySelector("#taskId").textContent === "history-detail");
    fixtures[0].status = "succeeded";
    fixtures[0].used_at = "2026-11-06T20:05:00";
    await waitForHistory(fixtures[0].task_id, "已完成");
    await waitForHistory(fixtures[0].task_id, "已使用");
    fixtures[1].used_at = "2026-11-06T20:06:00";
    await page.waitForFunction(() => !document.querySelector("#taskUsedBadge").hidden);

    await page.evaluate(() => { window.__historyNode = document.querySelector(".history-mini-item"); });
    const priorCalls = queueCalls;
    await page.waitForResponse((response) => new URL(response.url()).pathname === "/queue");
    if (queueCalls <= priorCalls) throw new Error("No automatic queue poll while viewing details");
    if (!(await page.evaluate(() => window.__historyNode === document.querySelector(".history-mini-item")))) throw new Error("Unchanged history was rebuilt");
    failQueue = true;
    await page.waitForFunction(() => document.querySelector("#historyMessage").textContent.includes("正在重试"));
    if (!(await history(fixtures[0].task_id).count())) throw new Error("A transient outage erased history");
    failQueue = false;
    await page.waitForFunction(() => !document.querySelector("#historyMessage").textContent.includes("正在重试"));
    if (legacyHistoryCalls) throw new Error("Sidebar still makes duplicate independent history requests");
    if (errors.length) throw new Error(errors.join("\n"));
    return { passed: ["real clipboard", "durable usage", "failed copy", "failed save", "task-switch race", "transcript copy", "live sidebar on home and details", "stable DOM", "network recovery", "shared polling"], layouts, pageErrors: errors };
  } finally {
    await page.unrouteAll({ behavior: "wait" });
    for (const id of ids) await page.request.delete(taskUrl(id), { headers });
  }
}
