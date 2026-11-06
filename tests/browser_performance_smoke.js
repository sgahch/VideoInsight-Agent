// Run with playwright-cli run-code --filename tests/browser_performance_smoke.js.
async (page) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const nativeFetch = window.fetch.bind(window);
    const initialOutput = sessionStorage.getItem("perf_output") || "";
    const result = (output) => ({
      transcript: "[00:00-00:05] First segment is ready before the summary.",
      output, title: "Performance check", segments: [], comments: [],
      media: { source_url: "https://example.com/video", title: "Performance check" },
      diagnostics: {},
    });
    const fixture = window.__perf = {
      mode: "stream", polls: 0, chatCalls: 0, chatFail: false, summaryRequests: 0,
      task: {
        task_id: "perf-stream", status: sessionStorage.getItem("perf_done") ? "succeeded" : "running",
        request: { url: "https://example.com/video", mode: "summary", use_mock: true },
        created_at: "2026-11-05T10:00:00", updated_at: "2026-11-05T10:00:00",
        progress: 80, stage: "analyzing", result: result(initialOutput),
      },
    };
    if (!sessionStorage.getItem("perf_initialized")) {
      localStorage.setItem("videoinsight_client_id", "perf-owner");
      localStorage.setItem("videoinsight_auth_token", "perf-token");
      localStorage.setItem("videoinsight_active_task", JSON.stringify({ clientId: "perf-owner", taskId: "perf-stream" }));
      sessionStorage.setItem("perf_initialized", "1");
    }
    const json = (value) => Promise.resolve(new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } }));
    const sse = (entries) => {
      const timers = [];
      const body = new ReadableStream({
        start(controller) {
          entries.forEach(([delay, event, data]) => {
            timers.push(setTimeout(() => {
              const payload = typeof data === "function" ? data() : data;
              controller.enqueue(new TextEncoder().encode(`event: ${event}\r\ndata: ${JSON.stringify(payload)}\r\n\r\n`));
              if (["complete", "done", "error"].includes(event)) controller.close();
            }, delay));
          });
        },
        cancel() { timers.forEach(clearTimeout); },
      });
      return Promise.resolve(new Response(body, { headers: { "content-type": "text/event-stream" } }));
    };
    window.fetch = async (input, options = {}) => {
      const url = new URL(typeof input === "string" ? input : input.url, location.href);
      if (url.pathname === "/config") {
        return json({ llm: { configured: true, provider: "mock", model: "test-model", protocol: "chat_completions" } });
      }
      if (!url.pathname.startsWith("/tasks")) return nativeFetch(input, options);
      const headers = new Headers(options.headers);
      if (headers.get("X-Client-Id") !== "perf-owner" || headers.get("Authorization") !== "Bearer perf-token") {
        throw new Error("Stream/list request lost its identity or authorization header");
      }
      if (url.pathname === "/tasks") {
        if (url.searchParams.get("view") !== "summary") throw new Error("History fetched full results");
        fixture.summaryRequests += 1;
        const { result: ignored, ...summary } = fixture.task;
        return json([{ ...summary, title: "Performance check" }]);
      }
      if (url.pathname.endsWith("/chat/stream")) {
        fixture.chatCalls += 1;
        return sse([
          [0, "delta", { text: "First answer" }],
          [700, fixture.chatFail ? "error" : "delta", fixture.chatFail ? { detail: "temporary loss" } : { text: " is complete." }],
          ...fixture.chatFail ? [] : [[1000, "done", {}]],
        ]);
      }
      if (url.pathname.endsWith("/events")) {
        if (fixture.mode === "poll") throw new TypeError("Simulated stream outage");
        if (fixture.task.status === "succeeded") return sse([[0, "complete", fixture.task]]);
        return sse([
          [0, "snapshot", () => fixture.task],
          [200, "delta", () => {
            fixture.task.result.output += "Partial summary. ";
            sessionStorage.setItem("perf_output", fixture.task.result.output);
            return { text: "Partial summary. " };
          }],
          [1800, "complete", () => {
            fixture.task.status = "succeeded";
            fixture.task.result.output += "Final summary.";
            sessionStorage.setItem("perf_done", "1");
            sessionStorage.setItem("perf_output", fixture.task.result.output);
            return fixture.task;
          }],
        ]);
      }
      if (fixture.mode === "poll") {
        fixture.polls += 1;
        if (fixture.polls === 2) throw new TypeError("Simulated transient network loss");
        if (fixture.polls >= 4) {
          fixture.task.status = "succeeded";
          fixture.task.result = result("Recovered final summary.");
        }
      }
      return json(fixture.task);
    };
  });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto("http://127.0.0.1:8001/");
  await page.waitForFunction(() => document.querySelector("#summaryOutput").textContent.includes("Partial summary."));
  if ((await page.locator("#taskStatusBadge").textContent()).includes("已完成")) throw new Error("Partial result arrived too late");
  if (!(await page.locator("#transcriptOutput").textContent()).includes("First segment")) throw new Error("Transcript was not shown early");
  await page.screenshot({ path: "output/playwright/partial-desktop.png", fullPage: true });

  await page.reload();
  await page.waitForFunction(() => document.querySelector("#summaryOutput").textContent.includes("Final summary."));
  if (!(await page.locator("#taskStatusBadge").textContent()).includes("已完成")) throw new Error("Refresh did not resume the task");

  await page.evaluate(() => {
    window.__perf.mode = "poll";
    window.__perf.task.status = "running";
    window.__perf.task.result.output = "";
  });
  await page.locator(".history-mini-item").first().click();
  await page.waitForFunction(() => document.querySelector("#taskMessage").textContent.includes("连接暂时中断"));
  if ((await page.locator("#taskStatusBadge").textContent()).includes("失败")) throw new Error("Transient network loss was shown as task failure");
  await page.waitForFunction(() => document.querySelector("#summaryOutput").textContent.includes("Recovered final summary."), null, { timeout: 16000 });

  await page.locator("#chatInput").fill("Summarize this");
  await page.locator("#sendChatBtn").click();
  await page.waitForFunction(() => document.querySelector("#chatMessages").textContent.includes("First answer"));
  if (!(await page.locator("#sendChatBtn").isDisabled())) throw new Error("Chat did not stream before completion");
  await page.locator("#chatInput").press("Enter");
  await page.waitForFunction(() => !document.querySelector("#sendChatBtn").disabled);
  if (await page.evaluate(() => window.__perf.chatCalls) !== 1) throw new Error("Duplicate chat request");
  if (!(await page.locator("#chatMessages").textContent()).includes("First answer is complete.")) throw new Error("Chat lost streamed content");

  await page.evaluate(() => { window.__perf.chatFail = true; });
  await page.locator("#chatInput").fill("Fail after a partial answer");
  await page.locator("#sendChatBtn").click();
  await page.waitForFunction(() => !document.querySelector("#sendChatBtn").disabled);
  const failedChat = await page.locator(".chat-bubble.agent.is-error").last().textContent();
  if (!failedChat.includes("First answer") || !failedChat.includes("temporary loss")) throw new Error("Partial chat answer was discarded");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "output/playwright/mobile.png", fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  if (overflow) throw new Error("Mobile horizontal overflow");
  if (errors.length) throw new Error(errors.join("\n"));
  console.log(JSON.stringify({ passed: ["early transcript", "incremental summary", "refresh resume", "network recovery", "streamed chat", "partial chat failure", "scoped headers", "lightweight history", "mobile layout"], pageErrors: errors }));
}
