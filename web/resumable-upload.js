/* Bounded-memory hashing and resumable uploads, also runnable under node:test. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ResumableUpload = api;
})(globalThis, function () {
  "use strict";
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const rotate = (value, bits) => (value >>> bits) | (value << (32 - bits));

  class Sha256 {
    constructor() {
      this.h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
      this.block = new Uint8Array(64);
      this.words = new Uint32Array(64);
      this.position = 0;
      this.length = 0;
    }
    compress(bytes, offset = 0) {
      const w = this.words;
      const view = new DataView(bytes.buffer, bytes.byteOffset + offset, 64);
      for (let i = 0; i < 16; i++) w[i] = view.getUint32(i * 4);
      for (let i = 16; i < 64; i++) {
        const a = w[i - 15], b = w[i - 2];
        w[i] = (w[i - 16] + (rotate(a, 7) ^ rotate(a, 18) ^ (a >>> 3)) + w[i - 7] + (rotate(b, 17) ^ rotate(b, 19) ^ (b >>> 10))) >>> 0;
      }
      let [a, b, c, d, e, f, g, h] = this.h;
      for (let i = 0; i < 64; i++) {
        const t1 = (h + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
        const t2 = ((rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      const values = [a, b, c, d, e, f, g, h];
      for (let i = 0; i < 8; i++) this.h[i] = (this.h[i] + values[i]) >>> 0;
    }
    update(data) {
      if (this.result) throw new Error("SHA-256 has already been finalized");
      const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
      this.length += bytes.length;
      let offset = 0;
      if (this.position) {
        const count = Math.min(64 - this.position, bytes.length);
        this.block.set(bytes.subarray(0, count), this.position);
        this.position += count; offset += count;
        if (this.position === 64) { this.compress(this.block); this.position = 0; }
      }
      while (offset + 64 <= bytes.length) { this.compress(bytes, offset); offset += 64; }
      if (offset < bytes.length) {
        this.block.set(bytes.subarray(offset), this.position);
        this.position += bytes.length - offset;
      }
      return this;
    }
    hex() {
      if (this.result) return this.result;
      this.block[this.position++] = 0x80;
      this.block.fill(0, this.position);
      if (this.position > 56) { this.compress(this.block); this.block.fill(0); }
      const view = new DataView(this.block.buffer);
      view.setUint32(56, Math.floor(this.length / 0x20000000));
      view.setUint32(60, (this.length * 8) >>> 0);
      this.compress(this.block);
      this.result = Array.from(this.h, (value) => value.toString(16).padStart(8, "0")).join("");
      return this.result;
    }
  }

  function checkAbort(signal) {
    if (signal?.aborted) throw new DOMException("上传已暂停", "AbortError");
  }
  async function hashFile(file, { signal, onProgress = () => {} } = {}) {
    const hash = new Sha256();
    for (let start = 0; start < file.size; start += 4 * 1024 * 1024) {
      checkAbort(signal);
      const bytes = new Uint8Array(await file.slice(start, start + 4 * 1024 * 1024).arrayBuffer());
      checkAbort(signal);
      hash.update(bytes);
      onProgress(Math.min(start + bytes.length, file.size) / file.size * 100);
    }
    checkAbort(signal);
    return hash.hex();
  }
  function sleep(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      checkAbort(signal);
      const abort = () => { clearTimeout(timer); reject(new DOMException("上传已暂停", "AbortError")); };
      const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, milliseconds);
      signal?.addEventListener("abort", abort, { once: true });
    });
  }
  function defaultStorage() {
    try { return globalThis.localStorage; } catch (_) { return null; }
  }
  function randomKey() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll("-", "");
    const bytes = new Uint8Array(16);
    if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.random() * 256;
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  class ResumableUploader {
    constructor({ request, storage = defaultStorage(), storageKey = "videoinsight_uploads_v1", onProgress = () => {}, concurrency = 3, retries = 3, delay = sleep }) {
      this.request = request;
      this.storage = storage;
      this.storageKey = storageKey;
      this.onProgress = onProgress;
      this.concurrency = Math.max(1, Math.min(concurrency, 3));
      this.retries = retries;
      this.delay = delay;
      this.records = {};
      try {
        const stored = JSON.parse(storage?.getItem(storageKey) || "{}");
        if (stored && typeof stored === "object" && !Array.isArray(stored)) this.records = stored;
      } catch (_) { /* Storage is optional; the active page can still pause and resume. */ }
      for (const [key, record] of Object.entries(this.records)) {
        const expiresAt = record && (record.expiresAt || record.updatedAt + 24 * 3600 * 1000);
        if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) delete this.records[key];
      }
    }
    pending() { return Object.values(this.records).sort((a, b) => b.updatedAt - a.updatedAt); }
    save(key, record) {
      if (record) this.records[key] = record;
      else delete this.records[key];
      try { this.storage?.setItem(this.storageKey, JSON.stringify(this.records)); } catch (_) { /* memory fallback */ }
    }
    emit(stage, extra = {}) {
      this.progress = { ...this.progress, stage, ...extra };
      this.onProgress(this.progress);
    }
    async attempt(operation, signal) {
      for (let attempt = 0; ; attempt++) {
        checkAbort(signal);
        try { return await operation(); } catch (error) {
          checkAbort(signal);
          const transient = !error.status || error.status === 408 || error.status === 429 || error.status >= 500;
          if (!transient || attempt >= this.retries) throw error;
          this.emit(this.progress.stage, { retrying: true });
          await this.delay(Math.min(500 * 2 ** attempt, 4000), signal);
        }
      }
    }
    async upload(file, options = {}) {
      if (this.running) throw new Error("已有文件正在上传。");
      this.running = true;
      this.paused = false;
      this.controller = new AbortController();
      const signal = this.controller.signal;
      this.session = null;
      this.recordKey = null;
      this.progress = { stage: "preparing", uploadedBytes: 0, totalBytes: file.size, retrying: false };
      this.emit("preparing");
      const work = async () => {
        const config = await this.attempt(() => this.request("/uploads/config", { signal }), signal);
        if (!file.size || file.size > config.max_file_size) throw new Error(`文件不能为空，且不能超过 ${Math.floor(config.max_file_size / 1048576)} MB。`);
        this.progress = { uploadedBytes: 0, totalBytes: file.size, retrying: false };
        this.emit("hashing", { hashPercent: 0 });
        const digest = await hashFile(file, { signal, onProgress: (hashPercent) => this.emit("hashing", { hashPercent }) });
        const key = `${digest}:${file.size}`;
        this.recordKey = key;
        let record = this.records[key];
        let session;
        if (record?.upload_id) {
          try { session = await this.attempt(() => this.request(`/uploads/${record.upload_id}`, { signal }), signal); }
          catch (error) {
            if (![404, 410].includes(error.status)) throw error;
            this.save(key, null); record = null;
          }
        }
        if (!session) {
          record = record || { filename: file.name, size: file.size, sha256: digest, idempotency_key: randomKey(), updatedAt: Date.now() };
          this.save(key, record);
          session = await this.attempt(() => this.request("/uploads", {
            method: "POST", headers: { "Content-Type": "application/json" }, signal,
            body: JSON.stringify({ filename: record.filename, size: file.size, sha256: digest, idempotency_key: record.idempotency_key }),
          }), signal);
          record = { ...record, upload_id: session.upload_id, expiresAt: session.expires_at * 1000, updatedAt: Date.now() };
          this.save(key, record);
        }
        this.session = session;
        if (session.sha256 !== digest || session.size !== file.size) throw new Error("续传文件与上传会话不匹配，请取消后重新上传。");
        const completed = new Set(session.uploaded_chunks);
        const bytesFor = (index) => Math.min(session.chunk_size, file.size - index * session.chunk_size);
        let confirmed = [...completed].reduce((sum, index) => sum + bytesFor(index), 0);
        this.emit("uploading", { uploadedBytes: confirmed, totalChunks: session.total_chunks, completedChunks: completed.size, retrying: false });
        let next = 0, failure;
        const worker = async () => {
          while (next < session.total_chunks && !signal.aborted) {
            const index = next++;
            if (completed.has(index)) continue;
            const blob = file.slice(index * session.chunk_size, (index + 1) * session.chunk_size);
            const hash = new Sha256().update(await blob.arrayBuffer()).hex();
            const accepted = await this.attempt(() => this.request(`/uploads/${session.upload_id}/chunks/${index}`, {
              method: "PUT", body: blob, signal, headers: { "Content-Type": "application/octet-stream", "X-Chunk-SHA256": hash },
            }, 120000), signal);
            record = { ...record, expiresAt: accepted.expires_at * 1000, updatedAt: Date.now() };
            this.save(key, record);
            completed.add(index); confirmed += bytesFor(index);
            this.emit("uploading", { uploadedBytes: confirmed, completedChunks: completed.size, retrying: false });
          }
        };
        const workers = session.state === "completed" ? [] : Array.from({ length: this.concurrency }, () => worker().catch((error) => {
          if (!signal.aborted) { failure = error; this.controller.abort(); }
        }));
        await Promise.all(workers);
        if (failure) throw failure;
        checkAbort(signal);
        this.emit("completing", { uploadedBytes: file.size, retrying: false });
        const task = await this.attempt(() => this.request(`/uploads/${session.upload_id}/complete`, {
          method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(options),
        }, 180000), signal);
        this.save(key, null);
        this.session = null;
        this.emit("done");
        return task;
      };
      this.active = work();
      try { return await this.active; }
      catch (error) {
        if (this.paused) { this.emit("paused", { retrying: false }); return null; }
        throw error;
      } finally { this.running = false; this.active = null; }
    }
    pause() { this.paused = true; this.controller?.abort(); }
    async cancel() {
      const wasRunning = this.running;
      const currentKey = this.recordKey;
      this.pause();
      if (this.active) await this.active.catch(() => {});
      const record = this.records[currentKey] || (!wasRunning ? this.pending()[0] : null);
      if (record) {
        let uploadId = record.upload_id;
        if (!uploadId) {
          const session = await this.request("/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(record) });
          uploadId = session.upload_id;
        }
        try { await this.request(`/uploads/${uploadId}`, { method: "DELETE" }); }
        catch (error) { if (![404, 410].includes(error.status)) throw error; }
        this.save(`${record.sha256}:${record.size}`, null);
      }
      this.session = null;
      this.emit("cancelled", { uploadedBytes: 0 });
    }
  }
  return { Sha256, hashFile, ResumableUploader };
});
