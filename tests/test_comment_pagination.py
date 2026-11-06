from __future__ import annotations

import asyncio
import sys
import types

import pytest

from crawler_service.pagination import Pager
from crawler_service.store import Store, platform_for_url

URLS = {"dy":"https://www.douyin.com/video/1", "ks":"https://www.kuaishou.com/short-video/a", "bili":"https://www.bilibili.com/video/BV1abc", "xhs":"https://www.xiaohongshu.com/explore/a?xsec_token=keep-me"}


def raw(platform, cid, replies=0):
    if platform == "dy":
        return {"cid":str(cid),"text":f"评论 {cid}","user":{"nickname":"观众"},"reply_comment_total":replies}
    if platform == "ks":
        return {"comment_id":cid,"content":f"评论 {cid}","author_name":"观众","hasSubComments":replies>0,"commentCount":replies}
    if platform == "bili":
        return {"rpid":cid,"content":{"message":f"评论 {cid}"},"member":{"uname":"观众"},"rcount":replies}
    return {"id":str(cid),"content":f"评论 {cid}","user_info":{"nickname":"观众"},"sub_comment_count":replies,"sub_comment_has_more":replies>0,"sub_comment_cursor":"preview-end","sub_comments":[raw(platform,cid*1000)] if replies else []}


class Client:
    def __init__(self, platform):
        self.platform = platform
        self.root_calls = []
        self.reply_calls = []
        self.break_root = None

    async def roots(self, cursor):
        self.root_calls.append(cursor)
        second = cursor not in (0, "")
        items = [raw(self.platform,i,21) for i in range(13,25) if second] or [raw(self.platform,i,21) for i in range(1,13)]
        if self.platform == "bili":
            return {"replies":items,"top":{"upper":items[0]},"cursor":{"is_end":second,"next":2}}
        if self.platform == "ks":
            return {"rootCommentsV2":items,"pcursorV2":"no_more" if second else "next"}
        return {"comments":items,"has_more":not second,"cursor":2}

    async def subs(self, root, cursor):
        self.reply_calls.append((str(root),cursor))
        if str(root) == self.break_root:
            return {"blocked":True}
        root = int(root)
        if self.platform == "bili":
            return {"page":{"count":21},"replies":[raw("bili",root*1000+i) for i in (range(20) if cursor==1 else [20])]}
        second = cursor in ("next",2)
        start = 1 if self.platform=="xhs" else 0
        items = [raw(self.platform,root*1000+i) for i in (range(start+10,21) if second else range(start,start+10))]
        if self.platform == "ks":
            return {"subCommentsV2":items,"pcursorV2":"no_more" if second else "next"}
        return {"comments":items,"has_more":not second,"cursor":2 if self.platform=="dy" else "next"}

    async def get_aweme_comments(self, video, cursor): return await self.roots(cursor)
    async def get_sub_comments(self, video, root, cursor): return await self.subs(root,cursor)
    async def get_video_comments(self, video, pcursor="", *, next=None, order_mode=None): return await self.roots(next if next is not None else pcursor)
    async def get_video_sub_comments(self, video, root, cursor): return await self.subs(root,cursor)
    async def get_note_comments(self, video, token, cursor):
        assert token == "keep-me"
        return await self.roots(cursor)
    async def get_note_sub_comments(self, video, root, token, *, cursor):
        assert token == "keep-me"
        return await self.subs(root,cursor)
    async def get_video_level_two_comments(self, video, root, pn, ps, mode):
        assert ps == 20
        return await self.subs(root,pn)


@pytest.fixture(autouse=True)
def bili_enum(monkeypatch):
    module = types.ModuleType("media_platform.bilibili.field")
    module.CommentOrderType = types.SimpleNamespace(DEFAULT=0,TIME=2)
    monkeypatch.setitem(sys.modules,"media_platform.bilibili.field",module)


@pytest.mark.parametrize("platform", URLS)
def test_exhausts_all_root_and_reply_pages_without_count_cap(tmp_path, platform):
    store=Store(tmp_path/"comments.db")
    job=store.create("browser",URLS[platform]); client=Client(platform)
    pager=Pager(store,job["id"],platform,interval=0)
    asyncio.run(pager.run(client,"video","keep-me"))
    result=store.job(job["id"])
    assert pager.exhausted and not pager.issues
    assert (result["roots"],result["replies"],result["total"]) == (24,504,528)
    assert len(client.root_calls)==2 and len(client.reply_calls)==48
    assert len(store.comments(job["id"],100,100)) == 100
    calls=len(client.reply_calls)
    asyncio.run(Pager(store,job["id"],platform,interval=0).run(client,"video","keep-me"))
    assert len(client.reply_calls)==calls
    assert store.job(job["id"])["total"]==528


@pytest.mark.parametrize("platform", URLS)
def test_failed_reply_page_keeps_checkpoint_and_resume_deduplicates(tmp_path, platform):
    store=Store(tmp_path/"comments.db"); job=store.create("browser",URLS[platform])
    client=Client(platform); client.break_root="2"
    pager=Pager(store,job["id"],platform,interval=0)
    asyncio.run(pager.run(client,"video","keep-me"))
    assert not pager.exhausted and pager.issues
    assert not store.checkpoint(job["id"],"roots")["done"]
    assert store.checkpoint(job["id"],"roots")["cursor"] is None
    assert store.checkpoint(job["id"],"1")["done"]
    client.break_root=None
    resumed=Pager(store,job["id"],platform,interval=0)
    asyncio.run(resumed.run(client,"video","keep-me"))
    assert resumed.exhausted and not resumed.issues
    assert store.job(job["id"])["total"]==528
    assert sum(root=="1" for root,cursor in client.reply_calls)==2


@pytest.mark.parametrize("payload", [
    {"comments":[],"has_more":True,"cursor":1},
    {"comments":[raw("dy",1)],"has_more":True,"cursor":0},
    {"comments":[],"has_more":False},
    {"comments":[{"text":"no id"}],"has_more":False,"cursor":0},
])
def test_malformed_or_stalled_page_never_marks_complete(tmp_path,payload):
    store=Store(tmp_path/"comments.db");job=store.create("owner",URLS["dy"])
    async def page(*_):return payload
    pager=Pager(store,job["id"],"dy",interval=0)
    with pytest.raises(ValueError):asyncio.run(pager.run(types.SimpleNamespace(get_aweme_comments=page),"video"))
    assert not pager.exhausted
    assert store.checkpoint(job["id"],"roots")=={"cursor":None,"done":False}


@pytest.mark.parametrize("url",["http://localhost/", "https://www.douyin.com.evil.com/", "https://www.douyin.com:8080/", "https://user:secret@www.douyin.com/", "file:///etc/passwd"])
def test_only_accepts_exact_supported_platform_hosts(url):
    with pytest.raises(ValueError):platform_for_url(url)


def test_store_recovery_and_owner_isolation(tmp_path):
    path=tmp_path/"comments.db";store=Store(path);job=store.create("a",URLS["dy"])
    store.status(job["id"],"running");store.save_checkpoint(job["id"],"roots",123,False)
    recovered=Store(path);recovered.recover()
    assert recovered.job(job["id"],"b") is None
    assert recovered.list_jobs("b")==[]
    assert recovered.job(job["id"],"a")["status"]=="interrupted"
    assert recovered.checkpoint(job["id"],"roots")["cursor"]==123
