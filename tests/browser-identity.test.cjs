const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { createHash } = require("node:crypto");
const { ResumableUploader } = require("../web/resumable-upload.js");

const KEY = "videoinsight_llm_config";
const CLIENT = "videoinsight_client_id";
const ACCOUNT_A = { authenticated: true, is_admin: false, account_id: "account:a", username: "a@example.test" };
const ACCOUNT_B = { authenticated: true, is_admin: false, account_id: "account:b", username: "b@example.test" };
const config = (apiKey) => JSON.stringify({ provider: "custom", baseUrl: "https://example.test/v1",
  protocol: "chat_completions", model: "test-model", apiKey });
const source = (file) => fs.readFileSync("web/" + file, "utf8");
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const sha = (text) => createHash("sha256").update(text).digest("hex");

function page({ records = new Map(), account, storageFailure, handler = () => null, comments = false } = {}) {
  const requests = [];
  const storageReads = [];
  const navigations = [];
  const nodes = new Map();
  const listeners = {};
  const timers = new Map();
  let sequence = 0;

  function element() {
    return {
      listeners: {}, value: "", innerHTML: "", textContent: "", hidden: true, isConnected: true,
      style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
      addEventListener(event, fn) { this.listeners[event] = fn; },
      querySelectorAll: () => [], setAttribute() {}, focus() {}, scrollIntoView() {},
      appendChild(child) { child.parentElement = this; },
      append(...children) { for (const child of children) this.appendChild(child); },
      replaceChildren() {},
    };
  }
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  }
  const document = {
    getElementById: node,
    querySelector: (selector) => selector.startsWith("#") ? node(selector.slice(1)) : null,
    querySelectorAll: () => [],
    createElement: element, addEventListener() {},
    documentElement: element(), body: element(), cookie: "",
  };
  let randomCalls = 0;
  const math = Object.create(Math);
  math.random = () => (++randomCalls) / 1000;

  const context = vm.createContext({
    document, console, Response, Headers, AbortController, URLSearchParams, TextDecoder, Math: math,
    CommentUtils: {}, setInterval(fn) { this.commentPoll = fn; },
    localStorage: {
      getItem(key) {
        storageReads.push(key);
        if (storageFailure === "getItem") throw Error("storage read disabled");
        return records.get(key) ?? null;
      },
      setItem(key, value) {
        if (storageFailure === "setItem") throw Error("storage write disabled");
        records.set(key, value);
      },
      removeItem(key) { records.delete(key); },
    },
    location: {
      pathname: "/", search: "", replace: (url) => navigations.push(url),
      reload: () => navigations.push("reload"),
    },
    alert() {},
    addEventListener(event, fn) { (listeners[event] ||= []).push(fn); },
    setTimeout(fn) { timers.set(++sequence, fn); return sequence; },
    clearTimeout(id) { timers.delete(id); },
    fetch: async (path, options = {}) => {
      const headers = new Headers(options.headers || {});
      requests.push({ path, headers, body: options.body });
      const custom = handler(path, options);
      if (custom) return custom;
      if (path === "/health") return json({ login_required: Boolean(account), mock_mode: false });
      if (path === "/auth/me") return json(account);
      if (path === "/config") return json({ llm: { configured: true, provider: "deepseek", model: "server-model" } });
      if (path === "/auth/logout" || path === "/cookies/status" || path === "/llm/test") return json({});
      if (path === "/analyze") return json({ task_id: "submitted" });
      if (path.startsWith("/comment-api/")) return json([]);
      if (path.endsWith("/chat/stream")) return new Response(
        'event: delta\ndata: {"text":"answer"}\n\nevent: done\ndata: {}\n\n', { headers: { "Content-Type": "text/event-stream" } },
      );
      throw Error("Unexpected request " + path);
    },
    VideoQueue: { create: () => ({ prepare() {}, start() {}, onSubmitted() {}, notify() {} }) },
  });
  context.window = context;
  // Keep the actual uploader, with the VM's storage at its browser boundary.
  context.ResumableUpload = { ResumableUploader: class extends ResumableUploader {
    constructor(options) { super({ ...options, storage: context.localStorage }); }
  } };
  const identityScript = "client-identity.js";
  if (fs.existsSync("web/" + identityScript)) vm.runInContext(source(identityScript), context);
  let ready;
  if (comments) {
    vm.runInContext(source("comments.js"), context);
  } else {
    const entry = "  boot();";
    const app = source("app.js");
    assert.ok(app.includes(entry));
    vm.runInContext(app.replace(entry, [
      "  window.testApi = { startLinkAnalysis, sendChat, submitUpload, startPolling, state, retryAsrChoice, retryPendingCookieTask };",
      "  window.ready = boot();",
    ].join("\n")), context);
    ready = context.ready;
  }
  return { node, requests, storageReads, navigations, listeners, context, ready };
}

const link = { url: "https://video.example.test/1", mode: "summary", useMock: false };

test('ordinary and cookie-login retries retain the original ASR destination', async () => {
  const browser = page({ account: ACCOUNT_A });
  await browser.ready;
  browser.node('asrProvider').value = 'siliconflow';
  const previous = { request: { asr_provider: 'faster_whisper', asr_require_timestamps: true } };
  const choice = browser.context.testApi.retryAsrChoice(previous);
  assert.equal(choice.asrProvider, 'faster_whisper');
  await browser.context.testApi.startLinkAnalysis({ ...link, mode: 'transcript', ...choice });
  browser.context.testApi.state.pendingCookieRetry = { ...link, mode: 'transcript', ...choice };
  await browser.context.testApi.retryPendingCookieTask();
  const bodies = browser.requests.filter(r => r.path === '/analyze').map(r => JSON.parse(r.body));
  assert.equal(bodies.length, 2);
  assert.ok(bodies.every(b => b.asr_provider === 'faster_whisper' && b.asr_require_timestamps === true));
});
const analyzeBody = (browser) => JSON.parse(browser.requests.find(({ path }) => path === "/analyze").body);
const chatBody = (browser) => JSON.parse(browser.requests.find(({ path }) => path.endsWith("/chat/stream")).body);
const chooseTask = (browser) => {
  browser.context.testApi.state.currentTask = { task_id: "test-task", status: "succeeded" };
};

test("model settings, analysis and chat wait for the authenticated identity", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const browser = page({ account: ACCOUNT_A, records: new Map([[KEY, config("old-unknown-key")]]),
    handler: (path) => path === "/auth/me" ? pending : null });
  await new Promise(setImmediate);
  await browser.context.testApi.startLinkAnalysis(link);
  await browser.context.testApi.sendChat("before identity");
  await browser.node("testModelConfig").listeners.click();
  assert.equal(browser.storageReads.some((key) => key.startsWith(KEY)), false);
  assert.equal(browser.node("llmApiKey").value, "");
  assert.equal(browser.node("saveModelConfig").disabled, true);
  assert.deepEqual(browser.requests.map(({ path }) => path), ["/health", "/auth/me"]);
  release(json(ACCOUNT_A));
  await browser.ready;
  assert.equal(browser.node("saveModelConfig").disabled, false);
});

test("logout removes A's model configuration and B's form and requests use only B's configuration", async () => {
  const records = new Map([[KEY, config("old-unknown-key")]]);
  const first = page({ account: ACCOUNT_A, records });
  await first.ready;
  assert.equal(first.node("llmApiKey").value, "");
  assert.equal(records.has(KEY), false);
  assert.match(first.node("modelMessage").textContent, /重新配置/);

  first.node("llmApiKey").value = "synthetic-key-A";
  first.node("saveModelConfig").listeners.click();
  assert.ok(records.get(KEY + ":account:a").includes("synthetic-key-A"));
  const reloaded = page({ account: ACCOUNT_A, records });
  await reloaded.ready;
  assert.equal(reloaded.node("llmApiKey").value, "synthetic-key-A");
  assert.doesNotMatch(reloaded.node("modelMessage").textContent, /重新配置/);
  chooseTask(reloaded);
  await reloaded.context.testApi.startLinkAnalysis(link);
  await reloaded.context.testApi.sendChat("A");
  assert.equal(analyzeBody(reloaded).llm_api_key, "synthetic-key-A");
  assert.equal(chatBody(reloaded).llm_api_key, "synthetic-key-A");

  await first.node("accountLogout").listeners.click();
  assert.equal(first.node("llmApiKey").value, "");
  assert.equal(records.has(KEY + ":account:a"), false);
  assert.deepEqual(first.navigations, ["/login"]);
  // A separate browser tab may have retained A's scope before a logout or session change.
  records.set(KEY + ":account:a", config("synthetic-key-A"));
  records.set(KEY + ":account:b", config("synthetic-key-B"));
  const next = page({ account: ACCOUNT_B, records });
  await next.ready;
  chooseTask(next);
  assert.equal(next.node("llmApiKey").value, "synthetic-key-B");
  await next.context.testApi.startLinkAnalysis(link);
  await next.context.testApi.sendChat("B");
  assert.equal(analyzeBody(next).llm_api_key, "synthetic-key-B");
  assert.equal(chatBody(next).llm_api_key, "synthetic-key-B");
  assert.equal(decodeURIComponent(next.requests.find(({ path }) => path === "/analyze").headers.get("X-VideoInsight-Account-Id")), "account:b");
  assert.equal(decodeURIComponent(next.requests.find(({ path }) => path.endsWith("/chat/stream")).headers.get("X-VideoInsight-Account-Id")), "account:b");
});

test("an invalid health response leaves identity and model settings unresolved", async () => {
  for (const response of [json({}), json({ login_required: null }), new Response("<html>proxy response</html>")]) {
    const browser = page({ records: new Map([[KEY, config("unknown-key")]]),
      handler: (path) => path === "/health" ? response : null });
    await browser.ready;
    await browser.context.testApi.startLinkAnalysis(link);
    assert.equal(browser.storageReads.some((key) => key.startsWith(KEY)), false);
    assert.equal(browser.node("llmApiKey").value, "");
    assert.equal(browser.node("saveModelConfig").disabled, true);
    assert.deepEqual(browser.requests.map(({ path }) => path), ["/health"]);
  }
});

test("a failed or incomplete identity never unlocks legacy model settings", async () => {
  for (const response of [json(null, 503), json({ authenticated: true, username: "missing-id" })]) {
    const browser = page({ account: ACCOUNT_A, records: new Map([[KEY, config("unknown-key")]]),
      handler: (path) => path === "/auth/me" ? response : null });
    await browser.ready;
    await browser.context.testApi.startLinkAnalysis(link);
    await browser.context.testApi.sendChat("identity did not resolve");
    assert.equal(browser.storageReads.some((key) => key.startsWith(KEY)), false);
    assert.equal(browser.node("llmApiKey").value, "");
    assert.equal(browser.node("saveModelConfig").disabled, true);
    assert.deepEqual(browser.requests.map(({ path }) => path), ["/health", "/auth/me"]);
  }
});

test("a cached page clears the previous account before the browser restores it", async () => {
  const browser = page({ account: ACCOUNT_A, records: new Map([[KEY + ":account:a", config("synthetic-key-A")]]) });
  await browser.ready;
  for (const listener of browser.listeners.pageshow) listener({ persisted: true });
  assert.equal(browser.node("llmApiKey").value, "");
  assert.equal(browser.node("saveModelConfig").disabled, true);
  assert.deepEqual(browser.navigations, ["reload"]);
});

test("an expired local auth token cannot interrupt account logout", async () => {
  const records = new Map([[KEY + ":account:a", config("synthetic-key-A")]]);
  const browser = page({ account: ACCOUNT_A, records });
  await browser.ready;
  browser.context.localStorage.removeItem = (key) => {
    if (key === "videoinsight_auth_token") throw Error("token removal disabled");
    records.delete(key);
  };
  await browser.node("accountLogout").listeners.click();
  assert.equal(records.has(KEY + ":account:a"), false);
  assert.equal(browser.node("llmApiKey").value, "");
  assert.deepEqual(browser.navigations, ["/login"]);
});

test("the page keeps its initial fallback client ID when storage becomes available", async () => {
  const browser = page({ storageFailure: "getItem" });
  await browser.ready;
  browser.context.localStorage.getItem = () => "different-persisted-id";
  await browser.context.testApi.startLinkAnalysis(link);
  assert.equal(new Set(browser.requests.map(({ headers }) => headers.get("X-Client-Id"))).size, 1);
});

test("login keeps existing resumable upload records available", async () => {
  const records = new Map([["videoinsight_uploads_v1:a@example.test", JSON.stringify({
    fixture: { filename: "pending.mp4", updatedAt: Date.now(), expiresAt: Date.now() + 3600000 },
  })]]);
  const browser = page({ account: ACCOUNT_A, records });
  await browser.ready;
  assert.equal(browser.node("uploadResumeHint").hidden, false);
  assert.match(browser.node("uploadResumeHint").textContent, /pending\.mp4/);
});

test("anonymous analysis still reads the browser's original model configuration", async () => {
  const records = new Map([[KEY, config("synthetic-anonymous-key")]]);
  const browser = page({ records });
  await browser.ready;
  assert.equal(browser.node("llmApiKey").value, "synthetic-anonymous-key");
  assert.ok(records.has(KEY));
  await browser.context.testApi.startLinkAnalysis(link);
  assert.equal(analyzeBody(browser).llm_api_key, "synthetic-anonymous-key");
});

test("an account change response clears a stale page before reloading it", async () => {
  const browser = page({ account: ACCOUNT_A, records: new Map([
    [KEY, config("synthetic-key-A")], [KEY + ":account:a", config("synthetic-key-A")],
  ]), handler: (path) => path === "/analyze" ?
    json({ detail: "account changed", code: "account_changed" }, 409) : null });
  await browser.ready;
  assert.equal(browser.node("llmApiKey").value, "synthetic-key-A");
  await browser.context.testApi.startLinkAnalysis(link);
  assert.equal(browser.node("llmApiKey").value, "");
  assert.equal(browser.node("saveModelConfig").disabled, true);
  assert.deepEqual(browser.navigations, ["reload"]);
});

for (const storageFailure of ["getItem", "setItem"]) {
  test("upload and polling share one client ID when " + storageFailure + " fails", async () => {
    const uploadId = "a".repeat(32);
    let uploaded = false;
    const session = () => ({ upload_id: uploadId, filename: "clip.mp4", size: 4, sha256: sha("clip"),
      chunk_size: 4, uploaded_chunks: uploaded ? [0] : [], uploaded_bytes: uploaded ? 4 : 0,
      total_chunks: 1, state: "uploading", expires_at: Date.now() / 1000 + 3600 });
    const browser = page({ storageFailure, handler(path, options) {
      if (path === "/uploads/config") return json({ max_file_size: 1024, chunk_size: 4 });
      if (path === "/uploads") return json(session());
      if (options.method === "PUT") { uploaded = true; return json(session()); }
      if (path.endsWith("/complete")) return json({ task_id: uploadId, status: "queued" });
      if (path.startsWith("/tasks/")) return json({ detail: "test polling termination" }, 404);
      return null;
    } });
    await browser.ready;
    browser.node("mediaFile").files = [new File(["clip"], "clip.mp4")];
    browser.node("uploadMode").value = "transcript";
    await browser.context.testApi.submitUpload({ preventDefault() {} });
    browser.context.testApi.startPolling(uploadId);
    await new Promise(setImmediate);

    assert.deepEqual(browser.requests.filter(({ path }) => /^\/(uploads|tasks)/.test(path)).map(({ path }) => path), [
      "/uploads/config", "/uploads", "/uploads/" + uploadId + "/chunks/0", "/uploads/" + uploadId + "/complete",
      "/tasks/" + uploadId + "/events", "/tasks/" + uploadId,
    ]);
    const ids = new Set(browser.requests.map(({ headers }) => headers.get("X-Client-Id")));
    assert.equal(ids.size, 1);
    assert.ok([...ids][0]);
    assert.equal(browser.node("storageNotice").hidden, false);
    assert.match(browser.node("storageNotice").textContent, /刷新/);
  });

  test("the comment page keeps polling when " + storageFailure + " fails", async () => {
    const browser = page({ storageFailure, comments: true });
    await new Promise(setImmediate);
    await browser.node("refresh").onclick();
    const ids = browser.requests.map(({ headers }) => headers.get("X-Client-Id"));
    assert.ok(ids.length >= 4 && ids.every((id) => id && id === ids[0]));
    assert.ok(browser.context.document.cookie.includes(ids[0]));
    assert.equal(browser.node("storageNotice").hidden, false);
    assert.match(browser.node("storageNotice").textContent, /刷新/);
  });
}
