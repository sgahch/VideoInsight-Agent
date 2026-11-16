const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function fixture(fetch) {
  const elements = new Map();
  const timers = new Map();
  const storage = new Map();
  let sequence = 0;
  class Element extends EventTarget {
    constructor() {
      super();
      this.hidden = true;
      this.disabled = false;
      this.textContent = "";
      this.href = "";
    }
    removeAttribute(name) { this[name] = ""; }
  }
  const document = new EventTarget();
  document.visibilityState = "visible";
  document.getElementById = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const window = new EventTarget();
  const context = {
    window, document, URL, CustomEvent, AbortController, Date, JSON, encodeURIComponent,
    navigator: { platform: "Win32", userAgent: "Windows" },
    location: { origin: "https://site.example", assign() {} },
    sessionStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    fetch,
    setTimeout: (callback, delay) => {
      const id = ++sequence;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../web/local-login.js"), "utf8"), context);
  window.addEventListener("videoinsight:helper-launch", event => event.preventDefault());
  window.VideoInsightLocalLogin.render({
    available: true, secure: true, username: "user@example.invalid",
    download_url: "/cookies/helper/download",
  });
  return {
    window, elements, timers, storage,
    click(id) { document.getElementById(id).dispatchEvent(new Event("click")); },
    async settle() { await new Promise(resolve => setImmediate(resolve)); },
    async tick() {
      assert.equal(timers.size, 1, "Exactly one status retry must remain scheduled");
      const [id, timer] = [...timers.entries()][0];
      timers.delete(id);
      await timer.callback();
      await this.settle();
      return timer.delay;
    },
  };
}

const sessionId = "a".repeat(32);
const started = {
  session_id: sessionId,
  launch_url: `videoinsight-login://authorize?server=https%3A%2F%2Fsite.example&token=${"t".repeat(64)}`,
};
const response = (status, body) => ({
  ok: status < 400, status, json: async () => body,
});

test("status checks recover after repeated server failures without an online event", async () => {
  let attempts = 0;
  const f = fixture(async path => {
    if (path === "/cookies/helper/start") return response(200, started);
    attempts += 1;
    return attempts <= 5
      ? response(503, { detail: "temporarily unavailable" })
      : response(200, { status: "confirmed", completed: true });
  });
  let saved = 0;
  f.window.addEventListener("videoinsight:helper-saved", () => { saved += 1; });
  f.click("startLocalLogin");
  await f.settle();
  for (let i = 0; i < 6; i += 1) {
    const delay = await f.tick();
    assert.ok(delay >= 2500 && delay <= 30000);
  }
  assert.equal(attempts, 6);
  assert.equal(saved, 1);
  assert.match(f.elements.get("localHelperMessage").textContent, /登录成功/);
  assert.equal(f.timers.size, 0);
  assert.equal(f.storage.size, 0);
});

test("authorization errors stop retries instead of reconnecting with an expired website session", async () => {
  const f = fixture(async path => response(
    path === "/cookies/helper/start" ? 200 : 401,
    path === "/cookies/helper/start" ? started : { detail: "unauthorized" },
  ));
  f.click("startLocalLogin");
  await f.settle();
  await f.tick();
  assert.equal(f.timers.size, 0);
  assert.equal(f.storage.size, 0);
  assert.match(f.elements.get("localHelperMessage").textContent, /重新登录/);
});
