// Run against the isolated server on 8001 with playwright-cli run-code --filename.
async (page) => {
  const base = "http://127.0.0.1:8001";
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.unrouteAll({ behavior: "wait" });
  await page.route("**/analyze", async (route) => {
    const data = route.request().postDataJSON();
    if (data.use_mock !== true) throw new Error("A browser smoke test attempted real analysis");
    await route.continue({ postData: JSON.stringify({ ...data, with_comments: false }) });
  });
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector("#poolCore").textContent === "2");
  const ids = [];
  for (let index = 1; index <= 2; index++) {
    if (await page.locator("#linkReal").isChecked()) await page.locator("#linkRealRow .switch-label").click();
    await page.locator("#videoUrl").fill(`https://example.com/browser-queue-${index}`);
    await page.waitForFunction((suffix) => document.querySelector("#linkPreviewUrl").textContent.endsWith(suffix), `browser-queue-${index}`);
    const response = page.waitForResponse((item) => item.url() === `${base}/analyze` && item.request().method() === "POST");
    await page.locator("#linkSubmit").click();
    const result = await (await response).json();
    ids.push(result.task_id);
    if (new URL(page.url()).pathname !== "/") throw new Error("Submission unexpectedly left the queue");
    await page.locator(`.queue-task-link[data-task-id="${result.task_id}"]`).waitFor();
  }
  await page.locator(`.queue-task-link[data-task-id="${ids[0]}"]`).click();
  await page.waitForFunction((id) => document.querySelector("#taskId").textContent === id &&
    document.querySelector("#taskStatusBadge").textContent === "已完成", ids[0]);
  if (new URL(page.url()).pathname !== `/view/${ids[0]}`) throw new Error("No independent detail URL");
  await page.reload();
  await page.waitForFunction((id) => document.querySelector("#taskId").textContent === id, ids[0]);
  await page.locator("#backToQueue").click();
  await page.locator(`.queue-task-link[data-task-id="${ids[1]}"]`).click();
  await page.waitForFunction((id) => document.querySelector("#taskId").textContent === id, ids[1]);
  await page.goBack();
  await page.waitForFunction(() => document.body.dataset.view === "queue");

  const headers = await page.evaluate(() => ({
    "X-Client-Id": localStorage.getItem("videoinsight_client_id"),
    "Authorization": `Bearer ${localStorage.getItem("videoinsight_auth_token") || ""}`,
  }));
  for (const id of ids) {
    const response = await page.request.delete(`${base}/tasks/${id}`, { headers });
    if (!response.ok()) throw new Error("Isolated smoke task cleanup failed");
  }

  const titles = ["长视频转写与产品需求分析", "用户访谈中的核心痛点", "内容创作的完整工作流", "平台链接权限校验"];
  const urls = ["https://www.bilibili.com/video/BV1example", "https://www.douyin.com/video/12345", "https://www.kuaishou.com/short-video/abc", "https://www.xiaohongshu.com/explore/123"];
  const fixtures = Array.from({ length: 20 }, (_, index) => ({
    task_id: (index + 1).toString(16).padStart(32, "0"),
    status: index < 2 ? "running" : index < 7 ? "queued" : index < 13 ? "succeeded" : "failed",
    request: { url: urls[index % 4], mode: "summary", label: `${titles[index % 4]} ${index + 1}` },
    title: `${titles[index % 4]} ${index + 1}`,
    created_at: "2026-11-06T14:30:00",
    updated_at: "2026-11-06T14:31:00",
    progress: index < 2 ? 42 + index : index >= 7 && index < 13 ? 100 : null,
    stage: index < 2 ? "transcribing" : null,
    queue_position: index >= 2 && index < 7 ? index - 1 : null,
    error: index >= 13 ? "链接需要有效的平台登录状态，请更新 Cookies 后重试。" : null,
  }));
  const pool = { core_workers: 2, max_workers: 4, queue_capacity: 32, active_workers: 2, queued: 5 };
  await page.route("**/queue?*", (route) => route.fulfill({
    json: { pool, tasks: fixtures, counts: { running: 2, queued: 5, succeeded: 6, failed: 7 }, engine: { asr_global_workers: 8 } },
  }));
  await page.route("**/tasks", (route) => route.fulfill({ json: [] }));
  await page.route("**/tasks?*", (route) => route.fulfill({ json: [] }));
  await page.route("**/tasks/*", (route) => {
    const task = fixtures.find((item) => route.request().url().endsWith(item.task_id));
    return route.fulfill({ status: task ? 200 : 404, json: task ? {
      ...task, status: "succeeded", result: {
        mode: "summary", transcript: `Transcript ${task.task_id}`, segments: [],
        output: `Summary ${task.task_id}`, title: task.title,
        media: { source_url: task.request.url, title: task.title }, diagnostics: {}, comments: [],
      },
    } : { detail: "Task not found" } });
  });
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector("#queueTaskList article"));
  await page.locator('[data-queue-filter="queued"]').click();
  if (await page.locator(".queue-task-row").count() !== 5) throw new Error("Status filtering failed");
  if (!(await page.locator("#queueTaskList").textContent()).includes("排队第 1 位")) throw new Error("Queue positions missing");
  await page.locator('[data-queue-filter="all"]').click();
  await page.locator("#queueSearch").fill("完整工作流 11");
  if (await page.locator(".queue-task-row").count() !== 1) throw new Error("Search failed");
  await page.locator("#queueSearch").fill("does not exist");
  if (!(await page.locator("#queueTaskList").textContent()).includes("没有匹配")) throw new Error("Empty search state missing");
  await page.locator("#queueSearch").fill("");
  await page.locator("#queueNext").click();
  if (!(await page.locator("#queuePageNumber").textContent()).startsWith("2 /")) throw new Error("Pagination failed");
  await page.locator("#queuePrevious").click();
  await page.locator('[data-queue-filter="failed"]').click();

  const layouts = [];
  for (const theme of ["dark", "light"]) {
    if (await page.evaluate(() => document.documentElement.dataset.theme) !== theme) await page.locator("#themeToggle").click();
    for (const width of [1440, 1024, 768, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      if (overflow) throw new Error(`Queue overflow at ${width}, ${theme}`);
      if ([1440, 390].includes(width)) {
        await page.screenshot({ path: `output/playwright/queue-fixture-${theme}-${width}.png`, fullPage: true });
      }
      layouts.push({ width, theme });
    }
  }

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('[data-queue-filter="all"]').click();
  await page.screenshot({ path: "output/playwright/queue-fixture-overview.png" });
  const first = fixtures[7], second = fixtures[8];
  await page.locator('[data-queue-filter="succeeded"]').click();
  await page.locator(`.queue-task-link[data-task-id="${first.task_id}"]`).click();
  await page.waitForFunction((id) => document.querySelector("#summaryOutput").textContent.includes(id), first.task_id);
  await page.evaluate(() => {
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble user";
    bubble.textContent = "FIRST VIDEO CHAT";
    document.querySelector("#chatMessages").append(bubble);
  });
  await page.locator("#backToQueue").click();
  await page.locator(`.queue-task-link[data-task-id="${second.task_id}"]`).click();
  await page.waitForFunction((id) => document.querySelector("#summaryOutput").textContent.includes(id), second.task_id);
  if ((await page.locator("#chatMessages").textContent()).includes("FIRST VIDEO CHAT")) throw new Error("Chat leaked across tasks");
  await page.locator("#backToQueue").click();
  await page.locator(`.queue-task-link[data-task-id="${first.task_id}"]`).click();
  await page.waitForFunction(() => document.querySelector("#chatMessages").textContent.includes("FIRST VIDEO CHAT"));
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error("Detail page overflow");
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: "output/playwright/queue-detail-desktop.png" });
  await page.goto(`${base}/view/missing-task`);
  await page.waitForFunction(() => document.querySelector("#taskDetailTitle").textContent === "无法打开任务");
  if (await page.locator("[data-task-detail]").first().isVisible()) throw new Error("Missing detail showed another task's data");
  let authReady = false;
  let authRace = false;
  await page.evaluate(() => localStorage.setItem("videoinsight_auth_token", "queue-browser-test-token"));
  await page.route("**/health", (route) => route.fulfill({ json: {
    status: "ok", mock_mode: true, auth_required: true, login_required: false, cookies_file: false,
  } }));
  await page.route("**/auth/session", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 100));
    authReady = true;
    await route.fulfill({ json: { authenticated: true } });
  });
  await page.route(`**/tasks/${first.task_id}`, (route) => {
    if (!authReady) authRace = true;
    return route.fulfill({ json: {
      ...first, status: "succeeded", result: {
        mode: "summary", transcript: "Authorized transcript", segments: [], output: "Authorized summary",
        media: { source_url: first.request.url }, diagnostics: {}, comments: [],
      },
    } });
  });
  await page.goto(`${base}/view/${first.task_id}`);
  await page.waitForFunction(() => document.querySelector("#summaryOutput").textContent.includes("Authorized summary"));
  if (authRace || !authReady) throw new Error("Detail loaded before the media auth session was synchronized");
  if (errors.length) throw new Error(errors.join("\n"));
  return { passed: true, realMockSubmissions: ids.length, layouts, details: ["deep link", "reload", "back", "chat isolation", "missing task", "auth startup ordering"], pageErrors: errors };
}
