(() => {
  "use strict";
  function timestamp(value) {
    if (value === null || value === undefined || value === "" || value === 0 || value === "0") return null;
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return numeric > 0 ? (numeric >= 100000000000 ? numeric : numeric * 1000) : null;
    }
    const text = String(value).trim();
    const zoned = /(?:Z|[+-]\d{2}:\d{2})$/i.test(text);
    const parsed = Date.parse(zoned ? text : `${text.replace(" ", "T")}Z`);
    return Number.isFinite(parsed) ? parsed : null;
  }
  function formatTime(value) {
    const time = timestamp(value);
    return time === null ? "时间未知" : new Date(time).toLocaleString("zh-CN", {hour12: false});
  }
  function flatten(comments) {
    const rows = [], seen = new Set();
    function visit(items, root = null) {
      for (const item of items) {
        if (seen.has(item.comment_id)) continue;
        seen.add(item.comment_id);
        rows.push({...item, root_comment_id: root});
        visit(item.sub_comments || [], root || item.comment_id);
      }
    }
    visit(comments);
    return rows;
  }
  function select(comments, sort, scope) {
    const rows = scope === "roots" ? [...comments] :
      flatten(comments).filter(item => scope !== "replies" || item.root_comment_id !== null);
    const newest = (a, b) => (timestamp(b.create_time) ?? -Infinity) - (timestamp(a.create_time) ?? -Infinity);
    if (sort === "likes" || sort === "top10") {
      rows.sort((a, b) => (Number(b.like_count) || 0) - (Number(a.like_count) || 0) || newest(a, b) || String(a.comment_id).localeCompare(String(b.comment_id)));
    } else if (sort === "newest") {
      rows.sort((a, b) => newest(a, b) || String(a.comment_id).localeCompare(String(b.comment_id)));
    }
    return sort === "top10" ? rows.slice(0, 10) : rows;
  }
  globalThis.CommentUtils = {timestamp, formatTime, flatten, select};
})();
