const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function harness(storage = new Map()) {
  const source = fs.readFileSync("web/app.js", "utf8");
  const context = vm.createContext({
    state: { chatSessions: new Map() },
    CHAT_CACHE_KEY: "chats",
    getClientId: () => "owner",
    localStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
    },
  });
  vm.runInContext(source.slice(source.indexOf("  function chatStorageKey()"),
    source.indexOf("  async function fetchEvents(")), context);
  return context;
}

test("chat survives reload, remains task-scoped and excludes interrupted answers", () => {
  const storage = new Map();
  const first = harness(storage);
  first.saveChatMessages("video-a", [
    { role: "user", content: "first", complete: true },
    { role: "assistant", content: "answer", complete: true },
    { role: "user", content: "second", complete: true },
    { role: "assistant", content: "partial", complete: false },
  ]);
  const reloaded = harness(storage);
  assert.equal(reloaded.loadChatMessages("video-a").length, 4);
  assert.equal(reloaded.loadChatMessages("video-b").length, 0);
  assert.equal(JSON.stringify(reloaded.chatHistory(reloaded.loadChatMessages("video-a"))),
    JSON.stringify([{ role: "user", content: "first" }, { role: "assistant", content: "answer" }]));
});

test("bad cache and disabled storage do not break chat", () => {
  const context = harness(new Map([["chats:owner", "not json"]]));
  assert.equal(context.loadChatMessages("a").length, 0);
  context.localStorage.setItem = () => { throw Error("disabled"); };
  context.saveChatMessages("a", [{ role: "user", content: "hello" }]);
  assert.equal(context.loadChatMessages("a")[0].content, "hello");
});

test("cache and model context are bounded", () => {
  const context = harness();
  const messages = Array.from({ length: 50 }, (_, i) => ({
    role: i % 2 ? "assistant" : "user", content: "x".repeat(10000), complete: true,
  }));
  context.saveChatMessages("a", messages);
  assert.equal(context.loadChatMessages("a").length, 40);
  assert.equal(context.chatHistory(context.loadChatMessages("a")).length, 6);
});
