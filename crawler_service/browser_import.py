"""Import a browser snapshot into the existing comment center, without crawling."""
from __future__ import annotations

from crawler_service.browser_capture import audit, ident, load_capture
from crawler_service.store import Store


def import_capture(store: Store, owner: str, payload: dict) -> dict:
    state = load_capture(payload)
    if len(state["comments"]) > 100000:
        raise ValueError("单次导入最多 100000 条评论，请分任务采集。")
    report = audit(state)
    # Validate all rows before creating a job; never overwrite a live crawl.
    rows = []
    for c in state["comments"].values():
        root, parent = ident(c.get("root")), ident(c.get("parent"))
        likes = c.get("like") or 0
        if isinstance(likes, bool) or not str(likes).isdigit():
            raise ValueError("评论点赞数量无效。")
        rows.append({"comment_id": c["rpid"], "root_comment_id": None if root == "0" else root,
                     "parent_comment_id": None if root == "0" else root if parent == "0" else parent,
                     "content": c.get("message", ""), "author_nickname": str(c.get("username") or ""),
                     "like_count": int(likes), "create_time": c.get("ctime"), "pictures": c.get("pictures") or []})
    job = store.create(owner, state["video_url"], reuse_active=False)
    try:
        store.save(job["id"], rows)
        report.update(started_at=state["started_at"], finished_at=state["finished_at"], title=state["title"])
        store.save_capture_report(job["id"], report)
        verified = report["accessible_scope_verified"]
        message = "本机浏览器采集结果已导入；" + ("主评论末页及逐串回复数量已核对。" if verified else "尚未核对完整，请在本机续抓后重新导入 snapshot.json。")
        if report["displayed_gap"] is not None:
            message += f" 页面显示 {report['displayed_total']} 条，实际保存 {report['total']} 条，差额 {report['displayed_gap']} 条（原因未确认）。"
        store.status(job["id"], "completed" if verified else "partial", message)
    except Exception:
        store.status(job["id"], "failed", "本机采集文件导入未完成，请重新导入。")
        raise
    return store.job(job["id"], owner)
