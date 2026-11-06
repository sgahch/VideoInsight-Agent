from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import pytest

from crawler_service.bilibili_browser import BrowserCollector, PlatformBlocked
from crawler_service.browser_capture import audit, export_capture, ingest_main, load_capture, new_capture, normalize, put, video_url
from crawler_service.browser_import import import_capture
from crawler_service.store import Store


URL = "https://www.bilibili.com/video/BV12gJz6qEKo/"


def raw(cid, *, root=0, replies=0):
    return {"rpid": int(cid), "rpid_str": str(cid), "root": root, "parent": root,
            "rcount": replies, "content": {"message": "=测试公式", "pictures": [{"img_src": "https://i0.hdslb.com/example.png"}]},
            "member": {"uname": "@观众"}, "like": 2, "ctime": 1}


def complete_capture():
    s = new_capture(URL)
    ingest_main(s, {"replies": [raw(1, replies=2)], "cursor": {"mode": 2, "is_begin": True, "is_end": True, "all_count": 5}})
    for cid in (2, 3):
        put(s, raw(cid, root=1))
    s["reply_runs"]["1"] = {"expected": 2, "total": 2, "count": 2, "complete": True,
        "pages": [{"pn": 1, "size": 10, "count": 2, "declared_count": 2}]}
    s["status"] = "complete"
    return s


def test_pinned_ids_pictures_and_large_string_ids():
    s = new_capture(URL)
    item = raw(9007199254740993)
    item["rpid"] = 9007199254740992
    assert normalize(item)["rpid"] == "9007199254740993"
    ingest_main(s, {"replies": [item], "top": {"upper": item}, "top_replies": [item],
                    "cursor": {"mode": 2, "is_begin": True, "is_end": True, "all_count": 1}})
    assert len(s["comments"]) == len(s["threads"]) == 1
    assert s["comments"]["9007199254740993"]["pictures"]


@pytest.mark.parametrize("kind", ["missing_end", "missing_reply", "fake_complete", "skipped_page", "missing_rcount", "orphan", "error"])
def test_partial_or_invalid_evidence_never_verifies_complete(kind):
    s = complete_capture()
    if kind == "missing_end": s["main_runs"][-1]["is_end"] = False
    elif kind == "missing_reply": del s["comments"]["3"]
    elif kind == "fake_complete": s["reply_runs"]["1"]["total"] = 3
    elif kind == "skipped_page": s["reply_runs"]["1"]["pages"][0]["pn"] = 2
    elif kind == "missing_rcount": s["threads"]["1"].pop("rcount")
    elif kind == "orphan": s["comments"]["3"]["root"] = "999"
    else: s["errors"].append({"stage": "main", "message": "blocked"})
    assert not audit(s)["accessible_scope_verified"]


def test_exports_preserve_gap_and_protect_csv_formulas(tmp_path):
    s = complete_capture()
    report = export_capture(s, tmp_path)
    assert report["accessible_scope_verified"] and report["displayed_gap"] == 2
    assert report["total"] == 3 and report["replies"] == 2
    assert len((tmp_path/"comments.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    text = (tmp_path/"comments.csv").read_text(encoding="utf-8-sig")
    assert "'=测试公式" in text and "'@观众" in text
    assert load_capture(json.loads((tmp_path/"snapshot.json").read_text(encoding="utf-8")))["comments"] == s["comments"]


def test_import_uses_own_job_and_preserves_reply_picture_and_scope(tmp_path):
    store = Store(tmp_path/"comments.db")
    active = store.create("a", URL)
    imported = import_capture(store, "a", complete_capture())
    assert imported["id"] != active["id"]
    assert store.job(active["id"])["status"] == "queued"
    assert imported["status"] == "completed" and imported["total"] == 3
    assert imported["capture_report"]["displayed_gap"] == 2
    rows = store.comments(imported["id"])
    assert rows[1]["root_comment_id"] == rows[1]["parent_comment_id"] == "1"
    assert rows[1]["pictures"]
    assert store.job(imported["id"], "b") is None


def test_partial_import_is_not_completed_and_invalid_import_creates_no_job(tmp_path):
    store = Store(tmp_path/"comments.db")
    s = complete_capture()
    s["reply_runs"].clear()
    imported = import_capture(store, "a", s)
    assert imported["status"] == "partial"
    s["comments"]["1"]["rpid"] = "2"
    with pytest.raises(ValueError): import_capture(store, "a", s)
    assert len(store.list_jobs("a")) == 1


def test_existing_database_is_migrated_without_losing_comments(tmp_path):
    dbpath = tmp_path/"old.db"
    with sqlite3.connect(dbpath) as db:
        db.execute("CREATE TABLE comments(job_id TEXT NOT NULL,id TEXT NOT NULL,parent TEXT,root TEXT,content TEXT NOT NULL,nickname TEXT,likes INTEGER NOT NULL DEFAULT 0,created TEXT,PRIMARY KEY(job_id,id))")
        db.execute("INSERT INTO comments VALUES('job','1',NULL,NULL,'原文','观众',1,'1')")
    rows = Store(dbpath).comments("job")
    assert rows[0]["content"] == "原文" and rows[0]["pictures"] == []


@pytest.mark.parametrize("url", ["https://www.bilibili.com.evil.com/video/BV1x/", "http://localhost/video/BV1x/", "https://user:pass@www.bilibili.com/video/BV1x/", "https://www.bilibili.com:8080/video/BV1x/", "https://b23.tv/test"])
def test_capture_url_only_accepts_supported_video_pages(url):
    with pytest.raises(ValueError): video_url(url)


def test_browser_reply_pages_use_response_size_and_resume_verified_threads(tmp_path):
    s = new_capture(URL)
    put(s, raw(1, replies=21), main=True)
    calls = []
    class Page:
        async def evaluate(self, expression, args):
            calls.append(args["pn"])
            pn = args["pn"]
            ids = range((pn-1)*10+2, min(pn*10+2, 23))
            return {"http": 200, "body": {"code": 0, "data": {"page": {"num": pn, "size": 10, "count": 21},
                "root": raw(1, replies=21), "replies": [raw(i, root=1) for i in ids]}}}
    collector = BrowserCollector(s, tmp_path, interval=0)
    collector.oid = "123"
    asyncio.run(collector.replies(Page()))
    assert calls == [1, 2, 3] and len(s["comments"]) == 22
    assert s["reply_runs"]["1"]["complete"]
    asyncio.run(collector.replies(Page()))
    assert calls == [1, 2, 3]


def test_browser_platform_block_stops_without_retrying(tmp_path):
    class Page:
        calls = 0
        async def evaluate(self, expression, args):
            self.calls += 1
            return {"http": 412}
    page = Page()
    c = BrowserCollector(new_capture(URL), tmp_path, interval=0)
    c.oid = "123"
    with pytest.raises(PlatformBlocked): asyncio.run(c.reply_page(page, "1", 1))
    assert page.calls == 1


def test_resume_command_reads_video_url_from_snapshot(tmp_path, monkeypatch):
    import sys
    from crawler_service import bilibili_browser
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(complete_capture()), encoding="utf-8")
    async def fake_run(args):
        assert args.url == URL
        assert args.resume == str(snapshot)
        return 2
    monkeypatch.setattr(bilibili_browser, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["capture", "--resume", str(snapshot), "--out-dir", str(tmp_path)])
    assert bilibili_browser.main() == 2


def test_chrome_without_discovery_endpoint_uses_local_browser_websocket(monkeypatch, tmp_path):
    from urllib.error import HTTPError
    from crawler_service import bilibili_browser
    def missing(url, timeout):
        raise HTTPError(url, 404, "Not Found", {}, None)
    monkeypatch.setattr(bilibili_browser, "urlopen", missing)
    profile = tmp_path / 'Google/Chrome/User Data'
    profile.mkdir(parents=True)
    (profile/'DevToolsActivePort').write_text('9222\n/devtools/browser/test-session\n', encoding='utf-8')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert bilibili_browser.cdp_endpoint("http://127.0.0.1:9222") == "ws://127.0.0.1:9222/devtools/browser/test-session"
