(function (root) {
  "use strict";
  const HELP = {
    siliconflow: "固定使用免费模型 FunAudioLLM/SenseVoiceSmall，只输出纯文本。需要付费 Qwen 时，请选择独立的 Qwen3-ASR 服务并授权。",
    siliconflow_qwen: "Qwen3-ASR 按音频片段转写，离线 Whisper 提供真实片段边界。授权兜底后，首个失败会停止付费请求，已成功文本保留，其余使用离线结果。不会转发到其他云服务。Key 仅用于本账号；费用及代金券适用范围以服务商账单和券详情为准。",
    cloudflare: "使用 whisper-large-v3-turbo，提供时间戳。需要 Account ID 和具有 Workers AI 调用及账号订阅读取权限的 API Token；每次转写前验证免费计划，付费或无法确认时拒绝上传。",
    groq: "使用 Whisper 时间戳模型。个人 Key 直接连接 Groq，不依赖服务器代理。Groq API 无法自动核验账号计费状态，请先在控制台确认免费计划。",
    faster_whisper: "使用服务器已部署的离线 Whisper，无需 API Key，音频留在服务器。模型路径由服务器管理员维护。",
  };
  function create({ document, request, onChange }) {
    const ids = ["asrSettings", "asrSettingsForm", "asrSettingsProvider", "asrSettingsHelp", "asrSettingsSource", "asrAccountGroup", "asrAccountId", "asrSecretGroup", "asrSecretLabel", "asrApiKey", "asrFreeGroup", "asrFreeConfirmed", "asrPaidGroup", "asrPaidAuthorized", "asrFallbackAuthorized", "asrTimeout", "asrMakeDefault", "asrSave", "asrCheck", "asrClear", "asrSettingsStatus"];
    const el = Object.fromEntries(ids.map(id => [id, document.getElementById(id)]));
    let config = null, epoch = 0, busy = false;
    const provider = () => el.asrSettingsProvider.value;
    function message(text, ok) {
      el.asrSettingsStatus.textContent = text;
      el.asrSettingsStatus.style.color = ok === false ? "var(--danger, #dc2626)" : "";
    }
    function lock(value) {
      busy = value;
      for (const id of ["asrSettingsProvider", "asrAccountId", "asrApiKey", "asrFreeConfirmed", "asrPaidAuthorized", "asrFallbackAuthorized", "asrTimeout", "asrMakeDefault", "asrSave", "asrCheck", "asrClear"]) el[id].disabled = value || !config;
    }
    function render() {
      const p = provider(), fields = config?.fields?.[p] || {};
      el.asrSettingsHelp.textContent = HELP[p] || "";
      el.asrAccountGroup.hidden = p !== "cloudflare";
      el.asrSecretGroup.hidden = p === "faster_whisper";
      el.asrFreeGroup.hidden = p !== "groq";
      el.asrPaidGroup.hidden = p !== "siliconflow_qwen";
      el.asrPaidAuthorized.checked = fields.paid_authorized === true;
      el.asrFallbackAuthorized.checked = fields.fallback_authorized === true;
      el.asrSecretLabel.textContent = p === "cloudflare" ? "API Token" : "API Key";
      el.asrApiKey.value = "";
      el.asrApiKey.placeholder = fields.credential_saved ? "已保存，留空保留原密钥" : "填写服务商密钥";
      el.asrAccountId.value = fields.account_id || "";
      el.asrFreeConfirmed.checked = fields.free_tier_confirmed === true;
      el.asrTimeout.value = String(config?.timeout_seconds || 180);
      const item = config?.providers?.find(item => item.id === p);
      el.asrSettingsSource.textContent = fields.credential_saved ? "本账号已保存密钥。" : (item?.configured ? "当前使用服务器配置。" : "尚未配置，请填写并保存。付费服务需要单独授权。");
    }
    function load(value) {
      epoch++;
      config = value;
      render();
      lock(false);
      message("");
    }
    function reset() {
      epoch++;
      config = null;
      el.asrSettings.open = false;
      render();
      lock(false);
      message("");
    }
    async function action(kind) {
      if (busy || !config) return;
      const p = provider(), version = epoch;
      const options = { method: kind === "save" ? "PUT" : kind === "clear" ? "DELETE" : "POST" };
      if (kind === "check" && el.asrApiKey.value.trim()) {
        message("请先保存新密钥，再检查已保存配置。", false);
        return;
      }
      if (kind === "save") {
        const body = { timeout_seconds: Number(el.asrTimeout.value), make_default: el.asrMakeDefault.checked };
        if (p !== "faster_whisper" && el.asrApiKey.value.trim()) body[p === "cloudflare" ? "api_token" : "api_key"] = el.asrApiKey.value.trim();
        if (p === "cloudflare") body.account_id = el.asrAccountId.value.trim();
        if (p === "groq") body.free_tier_confirmed = el.asrFreeConfirmed.checked;
        if (p === "siliconflow_qwen") { body.paid_authorized = el.asrPaidAuthorized.checked; body.fallback_authorized = el.asrFallbackAuthorized.checked; }
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(body);
      }
      lock(true);
      message(kind === "check" ? "正在检查配置，不上传音频…" : "正在保存…");
      try {
        const result = await request("/asr/settings/" + p + (kind === "check" ? "/check" : ""), options);
        if (version !== epoch) return;
        if (kind === "check") message(result.message, result.ok);
        else {
          config = result;
          render();
          onChange(result, kind === "save" ? p : null);
          message(kind === "save" ? "已保存。新任务使用当前选择的服务；未上传音频。" : "已清除本账号的此服务配置，服务器配置保留。", true);
        }
      } catch (error) {
        if (version === epoch) message(error.message || "操作失败，请重试。", false);
      } finally {
        if (version === epoch) lock(false);
      }
    }
    el.asrSettingsForm.addEventListener("submit", event => { event.preventDefault(); return action("save"); });
    el.asrSettingsProvider.addEventListener("change", () => { render(); message(""); });
    el.asrCheck.addEventListener("click", () => action("check"));
    el.asrClear.addEventListener("click", () => action("clear"));
    reset();
    return { load, reset };
  }
  root.AsrSettings = { create };
  if (typeof module !== "undefined" && module.exports) module.exports = { create };
})(typeof window === "undefined" ? globalThis : window);
