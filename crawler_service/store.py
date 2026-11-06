from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

PLATFORMS = {"dy": "抖音", "ks": "快手", "bili": "B站", "xhs": "小红书"}
DOMAINS = {"dy": {"douyin.com", "www.douyin.com", "v.douyin.com", "m.douyin.com"}, "ks": {"kuaishou.com", "www.kuaishou.com", "v.kuaishou.com", "v.gifshow.com"}, "bili": {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}, "xhs": {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}}
CommentSort = Literal["collected", "likes", "newest"]
CommentScope = Literal["all", "roots", "replies"]


def comment_timestamp(value) -> float | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        stamp = float(value)
        if stamp >= 100_000_000_000:
            stamp /= 1000
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, TypeError):
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, OverflowError, OSError):
            return None


def platform_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("请填写四个平台的有效视频链接。")
    for platform, domains in DOMAINS.items():
        if parsed.hostname in domains:
            return platform
    raise ValueError("目前支持抖音、快手、B站和小红书。")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL,
                    platform TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL, pages INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS comments(job_id TEXT NOT NULL, id TEXT NOT NULL, parent TEXT,
                    root TEXT, content TEXT NOT NULL, nickname TEXT, likes INTEGER NOT NULL DEFAULT 0,
                    created TEXT, PRIMARY KEY(job_id,id));
                CREATE INDEX IF NOT EXISTS comments_job_root ON comments(job_id,root);
                CREATE TABLE IF NOT EXISTS checkpoints(job_id TEXT NOT NULL, stream TEXT NOT NULL,
                    cursor TEXT NOT NULL, done INTEGER NOT NULL, PRIMARY KEY(job_id,stream));
                CREATE TABLE IF NOT EXISTS capture_reports(job_id TEXT PRIMARY KEY, report TEXT NOT NULL);
            """)
            if "pictures" not in {r["name"] for r in db.execute("PRAGMA table_info(comments)")}:
                db.execute("ALTER TABLE comments ADD COLUMN pictures TEXT NOT NULL DEFAULT '[]'")
            if "created_ts" not in {r["name"] for r in db.execute("PRAGMA table_info(comments)")}:
                db.execute("ALTER TABLE comments ADD COLUMN created_ts REAL")
                for row in db.execute("SELECT rowid,created FROM comments"):
                    db.execute("UPDATE comments SET created_ts=? WHERE rowid=?", (comment_timestamp(row["created"]), row["rowid"]))
            db.execute("CREATE INDEX IF NOT EXISTS comments_job_likes ON comments(job_id,likes DESC,created_ts DESC,id)")
            db.execute("CREATE INDEX IF NOT EXISTS comments_job_time ON comments(job_id,created_ts DESC,id)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner: str, url: str, *, reuse_active: bool = True) -> dict:
        platform = platform_for_url(url)
        with self.connect() as db:
            active = db.execute("SELECT id FROM jobs WHERE owner=? AND url=? AND status IN ('queued','running')", (owner, url)).fetchone()
            if active and reuse_active:
                return self.job(active["id"], owner)
            job_id = uuid4().hex
            now = time.time()
            db.execute("INSERT INTO jobs(id,owner,url,platform,status,created,updated) VALUES(?,?,?,?,?,?,?)", (job_id, owner, url, platform, "queued", now, now))
        return self.job(job_id, owner)

    def job(self, job_id: str, owner: str | None = None) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?" + (" AND owner=?" if owner is not None else ""), (job_id, owner) if owner is not None else (job_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            counts = db.execute("SELECT COUNT(*) total,SUM(CASE WHEN root IS NULL THEN 1 ELSE 0 END) roots FROM comments WHERE job_id=?", (job_id,)).fetchone()
            result.update(total=counts["total"], roots=counts["roots"] or 0, replies=counts["total"] - (counts["roots"] or 0))
            capture = db.execute("SELECT report FROM capture_reports WHERE job_id=?", (job_id,)).fetchone()
            if capture:
                result["capture_report"] = json.loads(capture["report"])
                result["source"] = "local-browser"
            return result

    def list_jobs(self, owner: str) -> list[dict]:
        with self.connect() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 50", (owner,))]
        return [self.job(job_id, owner) for job_id in ids]

    def status(self, job_id: str, status: str, message: str = ""):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,message=?,updated=? WHERE id=?", (status, message[:1000], time.time(), job_id))

    def checkpoint(self, job_id: str, stream: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT cursor,done FROM checkpoints WHERE job_id=? AND stream=?", (job_id, stream)).fetchone()
        return {"cursor": json.loads(row["cursor"]), "done": bool(row["done"])} if row else {"cursor": None, "done": False}

    def save_checkpoint(self, job_id: str, stream: str, cursor, done: bool):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)", (job_id, stream, json.dumps(cursor), int(done)))
            db.execute("UPDATE jobs SET pages=pages+1,updated=? WHERE id=?", (time.time(), job_id))

    def save(self, job_id: str, rows: list[dict]):
        with self.connect() as db:
            db.executemany("INSERT INTO comments(job_id,id,parent,root,content,nickname,likes,created,pictures,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id,id) DO UPDATE SET parent=excluded.parent,root=excluded.root,content=excluded.content,nickname=excluded.nickname,likes=excluded.likes,created=excluded.created,pictures=excluded.pictures,created_ts=excluded.created_ts", [(job_id, r["comment_id"], r.get("parent_comment_id"), r.get("root_comment_id"), r.get("content") or "", r.get("author_nickname"), r.get("like_count") or 0, str(r.get("create_time") or ""), json.dumps(r.get("pictures") or [], ensure_ascii=False), comment_timestamp(r.get("create_time"))) for r in rows])

    def save_capture_report(self, job_id: str, report: dict):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO capture_reports VALUES(?,?)", (job_id, json.dumps(report, ensure_ascii=False)))

    @staticmethod
    def comment_filter(job_id: str, scope: CommentScope, root_id: str | None):
        if scope not in {"all", "roots", "replies"}:
            raise ValueError("无效的评论范围。")
        where, params = "c.job_id=?", [job_id]
        if scope != "all":
            where += " AND c.root IS " + ("NULL" if scope == "roots" else "NOT NULL")
        if root_id is not None:
            where += " AND c.root=?"
            params.append(root_id)
        return where, params

    def comment_count(self, job_id: str, *, scope: CommentScope = "all", root_id: str | None = None) -> int:
        where, params = self.comment_filter(job_id, scope, root_id)
        with self.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM comments c WHERE {where}", params).fetchone()[0]

    def comments(self, job_id: str, offset: int = 0, limit: int = 100, *,
                 sort: CommentSort = "collected", scope: CommentScope = "all",
                 root_id: str | None = None, top: int | None = None) -> list[dict]:
        orders = {"collected": "c.rowid", "likes": "c.likes DESC,c.created_ts DESC,c.id",
                  "newest": "c.created_ts DESC,c.id"}
        if sort not in orders:
            raise ValueError("无效的评论排序。")
        offset = max(0, offset)
        limit = max(0, min(limit, top - offset)) if top is not None else max(0, limit)
        where, params = self.comment_filter(job_id, scope, root_id)
        with self.connect() as db:
            rows = db.execute(f"""SELECT c.*,
                CASE WHEN c.root IS NULL THEN
                    (SELECT COUNT(*) FROM comments r WHERE r.job_id=c.job_id AND r.root=c.id)
                ELSE 0 END reply_count
                FROM comments c WHERE {where} ORDER BY {orders[sort]} LIMIT ? OFFSET ?""",
                [*params, limit, offset]).fetchall()
        return [{"comment_id": r["id"], "parent_comment_id": r["parent"], "root_comment_id": r["root"], "content": r["content"], "author_nickname": r["nickname"], "like_count": r["likes"], "create_time": r["created"], "pictures": json.loads(r["pictures"]), "reply_count": r["reply_count"]} for r in rows]

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted',message='服务已重启，已保存评论和翻页位置，点击继续抓取。' WHERE status IN ('queued','running')")
