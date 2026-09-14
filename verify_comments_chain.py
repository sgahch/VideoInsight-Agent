"""验证「观众评论」链路：mock 转写 + 真实抖音评论接口。

用法：
    .venv/Scripts/python.exe verify_comments_chain.py [视频短链]

下载/ASR 走 mock（瞬时完成），评论走真实 DouyinCommentFetcher，
用于快速确认「观众评论」面板是否有数据，而不必等 25 分钟 ASR。
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
DEFAULT_URL = "https://v.douyin.com/HopBInq9Gq4"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    health = httpx.get(f"{BASE}/health", timeout=10).json()
    print("health.comment_source =", health.get("comment_source"))

    resp = httpx.post(
        f"{BASE}/analyze",
        json={"url": url, "mode": "transcript", "with_comments": True, "use_mock": True},
        timeout=30,
    )
    resp.raise_for_status()
    task_id = resp.json()["task_id"]
    print("task_id =", task_id)

    task: dict = {}
    for _ in range(60):
        time.sleep(1)
        task = httpx.get(f"{BASE}/tasks/{task_id}", timeout=10).json()
        if task.get("status") in ("succeeded", "failed"):
            break
    print("status =", task.get("status"), task.get("error") or "")

    result = task.get("result") or {}
    diag = result.get("diagnostics") or {}
    print("comments_source =", diag.get("comments_source"))
    print("comment_count   =", diag.get("comment_count"))
    for comment in (result.get("comments") or []):
        print(
            f"  [{comment.get('like_count')}赞] {comment.get('author_nickname')} "
            f"({comment.get('create_time')}): {(comment.get('content') or '')[:50]}"
        )
    return 0 if diag.get("comment_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
