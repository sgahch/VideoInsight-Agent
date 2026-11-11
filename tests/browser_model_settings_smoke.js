// Run with playwright-cli run-code --filename against an isolated local server.
async (page) => {
  const base = "http://127.0.0.1:8001";
  const errors = [];
  const calls = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/config", (route) => route.fulfill({ json: {
    llm: { configured: false, provider: "mock" },
  } }));
  await page.route("**/llm/test", async (route) => {
    const data = route.request().postDataJSON();
    calls.push(data);
    await route.fulfill({ json: {
      ok: true, model: data.llm_model, protocol: data.llm_protocol,
      latency_ms: 1, endpoint: `${data.llm_base_url}/responses`,
    } });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base);
  await page.evaluate(() => localStorage.removeItem("videoinsight_llm_config"));
  await page.reload();
  await page.locator('a[href="#settings"]').click();
  await page.locator("#llmApiKey").fill("browser-test-key");
  if (await page.locator("#llmModel").inputValue() !== "deepseek-flash") {
    throw new Error("The default DeepSeek model ID is outdated");
  }
  await page.locator("#llmBaseUrl").fill("https://api.deepseek.com");
  await page.locator("#llmProtocol").selectOption("responses");
  await page.locator("#llmModel").fill("DeepSeek V4 Flash");
  await page.locator("#testModelConfig").click();
  if (!(await page.locator("#modelMessage").textContent()).includes("deepseek-v4-flash")) {
    throw new Error("The invalid display name was not explained");
  }
  if (calls.length) throw new Error("An invalid display name reached the API");
  await page.locator("#saveModelConfig").click();
  if (await page.evaluate(() => localStorage.getItem("videoinsight_llm_config"))) {
    throw new Error("An invalid display name was saved");
  }
  const invalidResponse = await page.request.post(`${base}/llm/test`, { data: {
    llm_provider: "deepseek", llm_base_url: "https://api.deepseek.com",
    llm_protocol: "responses", llm_model: "DeepSeek V4 Flash", llm_api_key: "browser-test-key",
  } });
  const invalidResult = await invalidResponse.json();
  if (invalidResult.ok !== false || !invalidResult.error.includes("deepseek-v4-flash") ||
      JSON.stringify(invalidResult).includes("browser-test-key")) {
    throw new Error("The live backend did not safely reject the display name");
  }
  const layouts = [];
  for (const theme of ["light", "dark"]) {
    if (await page.evaluate(() => document.documentElement.dataset.theme) !== theme) {
      await page.locator("#themeToggle").click();
    }
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.locator("#modelMessage").scrollIntoViewIfNeeded();
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
        throw new Error(`Model settings overflow at ${width}px`);
      }
      const overlaps = await page.evaluate(() => {
        const message = document.querySelector("#modelMessage").getBoundingClientRect();
        const button = document.querySelector("#testModelConfig").getBoundingClientRect();
        return message.left < button.right && message.right > button.left &&
          message.top < button.bottom && message.bottom > button.top;
      });
      if (overlaps) throw new Error(`Model error overlaps controls at ${width}px`);
      await page.locator("#settings").screenshot({ path: `output/playwright/model-settings-${theme}-${width}.png` });
      layouts.push({ theme, width });
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator("#llmModel").fill("deepseek-v4-flash");
  await page.locator("#testModelConfig").click();
  await page.waitForFunction(() => document.querySelector("#modelMessage").textContent.includes("1ms"));
  if (calls.length !== 1 || calls[0].llm_protocol !== "responses" || calls[0].llm_model !== "deepseek-v4-flash") {
    throw new Error("The valid model ID or selected protocol was rewritten");
  }
  await page.locator("#saveModelConfig").click();
  await page.reload();
  await page.locator('a[href="#settings"]').click();
  if (await page.locator("#llmModel").inputValue() !== "deepseek-v4-flash" ||
      await page.locator("#llmProtocol").inputValue() !== "responses") {
    throw new Error("The saved model configuration did not survive reload");
  }
  await page.locator("#llmProvider").selectOption("custom");
  await page.locator("#llmBaseUrl").fill("https://gateway.example/v1");
  await page.locator("#llmModel").fill("DeepSeek V4 Flash");
  await page.locator("#testModelConfig").click();
  await page.waitForFunction(() => document.querySelector("#modelMessage").textContent.includes("1ms"));
  if (calls.length !== 2 || calls[1].llm_model !== "DeepSeek V4 Flash") {
    throw new Error("A gateway-specific model alias was blocked");
  }
  await page.locator("#clearModelConfig").click();
  if (errors.length) throw new Error(errors.join("\n"));
  return { ok: true, liveBackendValidation: true, mockedRequests: calls.length, layouts };
}
