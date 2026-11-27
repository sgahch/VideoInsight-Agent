const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");

const load = () => require("../web/resumable-upload.js");
const digest = (data) => createHash("sha256").update(data).digest("hex");
const storage = () => {
  const records = new Map();
  return { getItem: (key) => records.get(key) || null, setItem: (key, value) => records.set(key, value) };
};

function server({ uploaded = [], failOnce = false, delay = 0 } = {}) {
  const bytes = Buffer.from("abcdefghij");
  const chunks = new Map(uploaded.map((index) => [index, bytes.subarray(index * 4, Math.min(index * 4 + 4, bytes.length))]));
  const state = { puts: [], creates: 0, completes: 0, active: 0, peak: 0, cancelled: false, completed: false };
  const session = () => ({ upload_id: "a".repeat(32), filename: "meeting.mp4", size: 10,
    sha256: digest(bytes), chunk_size: 4, total_chunks: 3, uploaded_chunks: [...chunks.keys()],
    uploaded_bytes: [...chunks.values()].reduce((sum, value) => sum + value.length, 0),
    state: state.completed ? "completed" : "uploading", expires_at: Date.now() / 1000 + 86400,
  });
  const request = async (path, options = {}) => {
    if (path === "/uploads/config") return { max_file_size: 1024, chunk_size: 4 };
    if (path === "/uploads") { state.creates++; return session(); }
    if (options.method === "DELETE") { state.cancelled = true; return {}; }
    if (path.endsWith("/complete")) {
      assert.equal(chunks.size, 3, "completion must wait for all chunks");
      assert.equal(Buffer.concat([chunks.get(0), chunks.get(1), chunks.get(2)]).toString(), "abcdefghij");
      state.completes++; state.completed = true;
      return { task_id: "a".repeat(32), status: "queued" };
    }
    if (options.method === "PUT") {
      const index = Number(path.split("/").at(-1));
      const data = Buffer.from(await options.body.arrayBuffer());
      assert.equal(options.headers["X-Chunk-SHA256"], digest(data));
      state.puts.push(index); state.active++; state.peak = Math.max(state.peak, state.active);
      try {
        if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
        if (options.signal?.aborted) throw new DOMException("aborted", "AbortError");
        if (failOnce && index === 1) { failOnce = false; throw new TypeError("network interrupted"); }
        chunks.set(index, data);
        return session();
      } finally { state.active--; }
    }
    return session();
  };
  return { state, request, session };
}

test("SHA-256 matches the standard digest across block and padding boundaries", async () => {
  const { Sha256, hashFile } = load();
  for (const length of [0, 1, 55, 56, 63, 64, 65, 1000, 4194317]) {
    const data = Buffer.alloc(length, 0x61);
    const hash = new Sha256();
    for (let offset = 0; offset < data.length; offset += 97) hash.update(data.subarray(offset, offset + 97));
    assert.equal(hash.hex(), digest(data));
    assert.equal(await hashFile(new File([data], "test.mp4")), digest(data));
  }
});

test("resuming uploads only missing chunks and retries a transient failure", async () => {
  const { ResumableUploader } = load();
  const remote = server({ uploaded: [0], failOnce: true });
  const local = storage();
  const uploader = new ResumableUploader({ request: remote.request, storage: local, delay: async () => {} });
  const task = await uploader.upload(new File(["abcdefghij"], "meeting.mp4"), { use_mock: true, llm_api_key: "private" });
  assert.equal(task.task_id, "a".repeat(32));
  assert.equal(remote.state.puts.includes(0), false);
  assert.equal(remote.state.puts.filter((index) => index === 1).length, 2);
  assert.equal(remote.state.completes, 1);
  assert.equal(uploader.pending().length, 0);
  assert.equal(local.getItem("videoinsight_uploads_v1").includes("private"), false);
});

test("pause settles requests, reload retains the session, and resume respects concurrency", async () => {
  const { ResumableUploader } = load();
  const remote = server({ delay: 15 });
  const local = storage();
  let paused = false;
  let first;
  first = new ResumableUploader({ request: remote.request, storage: local, concurrency: 1,
    onProgress: (progress) => {
      if (!paused && progress.stage === "uploading" && progress.uploadedBytes >= 4) { paused = true; first.pause(); }
    },
  });
  assert.equal(await first.upload(new File(["abcdefghij"], "meeting.mp4"), {}), null);
  assert.equal(remote.state.completes, 0);
  assert.equal(first.pending().length, 1);
  const second = new ResumableUploader({ request: remote.request, storage: local, concurrency: 2 });
  await second.upload(new File(["abcdefghij"], "meeting.mp4"), {});
  assert.equal(remote.state.creates, 1);
  assert.equal(remote.state.puts.filter((index) => index === 0).length, 1);
  assert.ok(remote.state.peak <= 2);
});

test("resume checks the full fingerprint before trusting server progress", async () => {
  const { ResumableUploader } = load();
  const remote = server({ uploaded: [0] });
  const uploader = new ResumableUploader({ request: async (path, options) => {
    const result = await remote.request(path, options);
    return path === "/uploads" ? { ...result, sha256: "0".repeat(64) } : result;
  }, storage: storage() });
  await assert.rejects(uploader.upload(new File(["abcdefghij"], "meeting.mp4"), {}), /文件|校验|匹配/);
  assert.equal(remote.state.puts.length, 0);
});

test("permanent chunk errors stop retries and cancellation deletes the persisted upload", async () => {
  const { ResumableUploader } = load();
  const remote = server();
  let failures = 0;
  const uploader = new ResumableUploader({ storage: storage(), request: async (path, options) => {
    if (options?.method === "PUT") { failures++; throw Object.assign(new Error("forbidden"), { status: 403 }); }
    return remote.request(path, options);
  } });
  await assert.rejects(uploader.upload(new File(["abcdefghij"], "meeting.mp4"), {}), /forbidden/);
  assert.ok(failures <= 3, "each active worker may fail once but must not retry a 403");
  await uploader.cancel();
  assert.equal(remote.state.cancelled, true);
  assert.equal(uploader.pending().length, 0);
});

test("completed response lost before reload is recovered without uploading again", async () => {
  const { ResumableUploader } = load();
  const remote = server();
  const local = storage();
  const first = new ResumableUploader({ storage: local, retries: 0, request: async (path, options) => {
    const result = await remote.request(path, options);
    if (path.endsWith("/complete")) throw new TypeError("lost response");
    return result;
  } });
  await assert.rejects(first.upload(new File(["abcdefghij"], "meeting.mp4"), {}), /lost response/);
  const second = new ResumableUploader({ storage: local, request: remote.request });
  const task = await second.upload(new File(["abcdefghij"], "meeting.mp4"), {});
  assert.equal(task.task_id, "a".repeat(32));
  assert.equal(remote.state.creates, 1);
  assert.equal(remote.state.puts.length, 3);
});

test("a transient configuration failure is retried before hashing", async () => {
  const { ResumableUploader } = load();
  const remote = server();
  let attempts = 0;
  const uploader = new ResumableUploader({ storage: storage(), delay: async () => {}, request: async (path, options) => {
    if (path === "/uploads/config" && attempts++ === 0) throw new TypeError("offline");
    return remote.request(path, options);
  } });
  const result = await uploader.upload(new File(["abcdefghij"], "meeting.mp4"), {});
  assert.equal(result.task_id, "a".repeat(32));
  assert.equal(attempts, 2);
});

test("cancelling a new file during hashing does not cancel an older paused upload", async () => {
  const { ResumableUploader } = load();
  const remote = server({ delay: 10 });
  let phase = "first", uploader;
  uploader = new ResumableUploader({ storage: storage(), request: remote.request, concurrency: 1,
    onProgress: (progress) => {
      if (phase === "first" && progress.stage === "uploading" && progress.uploadedBytes >= 4) uploader.pause();
    },
  });
  await uploader.upload(new File(["abcdefghij"], "meeting.mp4"), {});
  phase = "second";
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const other = new File(["different!"], "other.mp4");
  other.slice = () => ({ arrayBuffer: async () => { await gate; return Buffer.from("different!"); } });
  const active = uploader.upload(other, {});
  // cancel waits for the hashing operation to settle, while its record is still unknown.
  await new Promise((resolve) => setImmediate(resolve));
  const cancellation = uploader.cancel();
  release();
  await cancellation;
  await active;
  assert.equal(remote.state.cancelled, false);
  assert.equal(uploader.pending().length, 1);
});

test("pending uploads follow the server retention time when it exceeds one day", () => {
  const { ResumableUploader } = load();
  const local = storage();
  local.setItem("videoinsight_uploads_v1", JSON.stringify({
    old: { filename: "meeting.mp4", upload_id: "a".repeat(32), updatedAt: Date.now() - 25 * 3600000, expiresAt: Date.now() + 3600000 },
    expired: { filename: "expired.mp4", updatedAt: Date.now(), expiresAt: Date.now() - 1 },
  }));
  const uploader = new ResumableUploader({ request: async () => {}, storage: local });
  assert.deepEqual(uploader.pending().map((record) => record.filename), ["meeting.mp4"]);
});
