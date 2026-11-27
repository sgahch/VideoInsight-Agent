const { test } = require("node:test");
const assert = require("node:assert/strict");
const { create } = require("../web/asr-settings.js");
const config = (saved = false) => ({ default: "faster_whisper", timeout_seconds: 180,
  providers: [{ id: "siliconflow", configured: saved }, { id: "faster_whisper", configured: true }],
  fields: { siliconflow: { credential_saved: saved } } });
function view(request) {
  const nodes = new Map(); const changes = [];
  const document = { getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, { value: "", checked: false, style: {}, events: {}, addEventListener(k, fn) { this.events[k] = fn; } });
    return nodes.get(id);
  } };
  const app = create({ document, request, onChange: (...args) => changes.push(args) });
  const el = id => document.getElementById(id);
  el("asrSettingsProvider").value = "siliconflow"; app.load(config());
  el("asrMakeDefault").checked = true;
  return { app, el, changes, submit: () => el("asrSettingsForm").events.submit({ preventDefault() {} }) };
}
test("save writes only the chosen provider, clears secret and enables switching without reload", async () => {
  const calls = []; const v = view(async (...args) => { calls.push(args); return config(true); });
  v.el("asrApiKey").value = "private-test-key";
  await v.submit();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/asr/settings/siliconflow");
  assert.deepEqual(JSON.parse(calls[0][1].body), { api_key: "private-test-key", timeout_seconds: 180, make_default: true });
  assert.equal(v.el("asrApiKey").value, "");
  assert.equal(v.changes[0][1], "siliconflow");
  assert.match(v.el("asrApiKey").placeholder, /已保存/);
});
test("blank secret is not resubmitted; connection check is a separate explicit action", async () => {
  const calls = []; const v = view(async (...args) => { calls.push(args); return config(true); });
  v.app.load(config(true)); await v.submit();
  assert.equal(JSON.parse(calls[0][1].body).api_key, undefined);
  v.el("asrApiKey").value = "unsaved-key";
  await v.el("asrCheck").events.click();
  assert.equal(calls.length, 1);
  assert.match(v.el("asrSettingsStatus").textContent, /先保存/);
  v.el("asrApiKey").value = "";
  await v.el("asrCheck").events.click();
  assert.equal(calls[1][0], "/asr/settings/siliconflow/check");
  assert.equal(calls[1][1].body, undefined);
});
test("account reset clears secrets and ignores a late response from the former account", async () => {
  let resolve; const v = view(() => new Promise(r => { resolve = r; }));
  v.el("asrApiKey").value = "private-account-a";
  const pending = v.submit();
  v.app.reset(); v.app.load(config());
  resolve(config(true)); await pending;
  assert.equal(v.el("asrApiKey").value, "");
  assert.equal(v.changes.length, 0);
  assert.equal(v.el("asrSettingsStatus").textContent, "");
});
test("provider change discards unsaved secrets; clear touches only selected provider", async () => {
  const calls = []; const v = view(async (...args) => { calls.push(args); return config(); });
  v.el("asrApiKey").value = "unsaved-sf";
  v.el("asrSettingsProvider").value = "cloudflare";
  v.el("asrSettingsProvider").events.change();
  assert.equal(v.el("asrApiKey").value, "");
  assert.equal(v.el("asrAccountGroup").hidden, false);
  await v.el("asrClear").events.click();
  assert.equal(calls[0][0], "/asr/settings/cloudflare");
  assert.equal(calls[0][1].method, "DELETE");
});

test("paid and fallback authorization are explicit and reset when changing account", async () => {
  const calls=[];const v=view(async(...args)=>{calls.push(args);return config();});
  v.el('asrSettingsProvider').value='siliconflow_qwen';v.el('asrSettingsProvider').events.change();
  assert.equal(v.el('asrPaidGroup').hidden,false);
  assert.equal(v.el('asrPaidAuthorized').checked,false);
  v.el('asrPaidAuthorized').checked=true;v.el('asrFallbackAuthorized').checked=true;
  await v.submit();
  const body=JSON.parse(calls[0][1].body);
  assert.equal(body.paid_authorized,true);assert.equal(body.fallback_authorized,true);
  v.app.reset();assert.equal(v.el('asrPaidAuthorized').checked,false);assert.equal(v.el('asrFallbackAuthorized').checked,false);
});
