// Run with playwright-cli run-code --filename tests/browser_platforms_smoke.js.
async (page) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:8000/");
  await page.locator(".platform-showcase").waitFor();
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".platform-showcase img")].every((image) => image.complete)
  );

  const results = [];
  const viewports = [
    { width: 1440, height: 1000 },
    { width: 1024, height: 900 },
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 320, height: 760 },
  ];
  for (const theme of ["dark", "light"]) {
    if (await page.evaluate(() => document.documentElement.dataset.theme) !== theme) {
      await page.locator("#themeToggle").click();
    }
    for (const viewport of viewports) {
      await page.setViewportSize(viewport);
      const state = await page.evaluate(() => {
        const showcase = document.querySelector(".platform-showcase");
        const bounds = showcase.getBoundingClientRect();
        const items = [...showcase.querySelectorAll("li")].map((item) => {
          const image = item.querySelector("img");
          const label = item.querySelector("span");
          const box = item.getBoundingClientRect();
          const text = label.getBoundingClientRect();
          return {
            name: label.textContent,
            loaded: image.complete && image.naturalWidth > 0 && image.naturalHeight > 0,
            local: new URL(image.src).origin === location.origin,
            fits: text.right <= box.right + 1 && text.bottom <= box.bottom + 1,
          };
        });
        return {
          theme: document.documentElement.dataset.theme,
          width: innerWidth,
          overflow: document.documentElement.scrollWidth > innerWidth,
          fits: bounds.left >= 0 && bounds.right <= innerWidth + 1,
          items,
        };
      });
      if (state.theme !== theme || state.overflow || !state.fits || state.items.length !== 4 ||
          state.items.some((item) => !item.loaded || !item.local || !item.fits)) {
        throw new Error(`Platform showcase regression: ${JSON.stringify(state)}`);
      }
      results.push({ theme, width: viewport.width, logos: state.items.length });
      if ([1440, 390].includes(viewport.width)) {
        await page.screenshot({
          path: `output/playwright/platforms-${theme}-${viewport.width}.png`,
          fullPage: true,
        });
      }
      if (viewport.width === 1440 && theme === "dark") {
        await page.locator(".platform-showcase").screenshot({
          path: "output/playwright/platforms-showcase.png",
        });
      }
    }
  }
  if (errors.length) throw new Error(`Page errors: ${errors.join("; ")}`);
  return { passed: true, checks: results, pageErrors: errors };
}
