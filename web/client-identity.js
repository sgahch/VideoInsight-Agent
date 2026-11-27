/* 浏览器客户端身份标识（主应用与评论页共用）。
 *
 * 存储可用时，身份持久化到 localStorage，并镜像到 cookie——媒体预览、下载等由
 * 浏览器原生发起的请求带不上 X-Client-Id 头，只能靠 cookie 带上身份。
 *
 * 存储不可用时（隐私模式 / 禁用站点数据），退化为「页面内固定值」：同一页面的
 * 所有请求复用同一个身份，刷新后匿名身份无法恢复，并提示用户。
 */
(() => {
  "use strict";
  const KEY = "videoinsight_client_id";
  let clientId = null;

  function generate() {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  }

  function warn() {
    const el = document.getElementById("storageNotice");
    if (!el) return;
    el.hidden = false;
    el.textContent = "浏览器本地存储不可用。当前页面可继续使用，刷新后可能无法恢复匿名任务和上传进度。";
  }

  function writeCookie(value) {
    try {
      document.cookie = `${KEY}=${encodeURIComponent(value)}; Path=/; SameSite=Lax; Max-Age=31536000`;
    } catch (error) { /* cookie 不可写时仅依赖请求头身份 */ }
  }

  window.getClientId = function getClientId() {
    if (clientId) return clientId;
    try {
      clientId = localStorage.getItem(KEY);
      if (!clientId) {
        clientId = generate();
        localStorage.setItem(KEY, clientId);
      }
    } catch (error) {
      clientId ||= generate();
      warn();
    }
    writeCookie(clientId);
    return clientId;
  };
})();
