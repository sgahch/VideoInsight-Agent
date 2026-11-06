from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from crawler_service.store import Store


@pytest.fixture
def service(monkeypatch,tmp_path):
    monkeypatch.setenv("COMMENT_CRAWLER_STATE",str(tmp_path))
    monkeypatch.setenv("COMMENT_CRAWLER_TOKEN","private-service-token")
    module=importlib.import_module("crawler_service.api")
    monkeypatch.setattr(module,"STATE",tmp_path)
    monkeypatch.setattr(module,"store",Store(tmp_path/"comments.db"))
    pending=[]
    monkeypatch.setattr(module,"schedule",pending.append)
    client=TestClient(module.app,headers={"Authorization":"Bearer private-service-token"})
    return client,module,pending


def test_internal_token_and_platform_cookie_status_do_not_expose_secrets(service):
    client,module,pending=service
    assert TestClient(module.app).get("/platforms").status_code==401
    assert TestClient(module.app).get("/health").status_code==200
    assert client.put("/platforms/dy/cookies",json={"content":"sessionid=secret-value"}).json()=={"saved":True}
    assert "secret-value" not in client.get("/platforms").text
    assert client.get("/platforms").json()[0]["cookies_configured"]
    assert client.delete("/platforms/dy/cookies").status_code==200
    assert not client.get("/platforms").json()[0]["cookies_configured"]


@pytest.mark.parametrize("platform,content,expected",[
    ("dy","Cookie: sessionid=secret", "sessionid=secret"),
    ("bili",json.dumps([{"name":"SESSDATA","value":"secret","domain":".bilibili.com"},{"name":"foreign","value":"drop","domain":"evil.com"}]),"SESSDATA=secret"),
    ("xhs","# Netscape HTTP Cookie File\n#HttpOnly_.xiaohongshu.com\tTRUE\t/\tTRUE\t0\tweb_session\tsecret\n.evil.com\tTRUE\t/\tFALSE\t0\tforeign\tdrop", "web_session=secret"),
    ("ks",json.dumps({"cookies":[{"name":"kuaishou.server.web_st","value":"secret","domain":"www.kuaishou.com"}]}),"kuaishou.server.web_st=secret"),
])
def test_cookie_import_formats_and_domain_filtering(service,platform,content,expected):
    client,module,pending=service
    assert client.put(f"/platforms/{platform}/cookies",json={"content":content}).status_code==200
    assert (module.STATE/"cookies"/f"{platform}.txt").read_text()==expected


@pytest.mark.parametrize("content",["", "not a cookie", "a=b\r\nInjected: header", "{invalid}","[]"])
def test_invalid_cookies_are_rejected(service,content):
    client,module,pending=service
    assert client.put("/platforms/dy/cookies",json={"content":content}).status_code==400


def test_owner_checks_export_pagination_retry_and_cancellation(service):
    client,module,pending=service
    response=client.post("/jobs",json={"url":"https://v.douyin.com/test/","owner":"a"})
    assert response.status_code==202
    job=response.json();job_id=job["id"]
    assert pending==[job_id]
    assert client.get(f"/jobs/{job_id}",params={"owner":"b"}).status_code==404
    assert client.get(f"/jobs/{job_id}/export",params={"owner":"b"}).status_code==404
    assert client.post(f"/jobs/{job_id}/retry",params={"owner":"b"}).status_code==404
    assert client.post(f"/jobs/{job_id}/cancel",params={"owner":"b"}).status_code==404
    module.store.save(job_id,[{"comment_id":str(i),"content":"<script>评论</script>","root_comment_id":"0" if i else None} for i in range(501)])
    page=client.get(f"/jobs/{job_id}/comments",params={"owner":"a","offset":100,"limit":100}).json()
    assert page["total"]==501 and len(page["items"])==100
    assert page["items"][0]["comment_id"]=="100"
    exported=client.get(f"/jobs/{job_id}/export",params={"owner":"a"})
    assert len(exported.text.splitlines())==501
    assert json.loads(exported.text.splitlines()[-1])["comment_id"]=="500"
    assert client.post(f"/jobs/{job_id}/cancel",params={"owner":"a"}).json()["status"]=="cancelled"
    assert client.post(f"/jobs/{job_id}/retry",params={"owner":"a"}).status_code==202


def test_worker_without_cookies_returns_actionable_status(service,monkeypatch):
    import asyncio
    from crawler_service.worker import run
    client,module,pending=service
    job=module.store.create("a","https://www.bilibili.com/video/BV1test")
    asyncio.run(run(job["id"]))
    assert module.store.job(job["id"])["status"]=="needs_login"


def test_browser_import_is_owned_validated_and_not_scheduled(service):
    from tests.test_browser_capture import complete_capture
    client,module,pending=service
    response=client.post("/jobs/import-browser",json={"owner":"a","snapshot":complete_capture()})
    assert response.status_code==201
    job=response.json()
    assert job["source"]=="local-browser" and job["total"]==3
    assert pending==[]
    assert client.get(f"/jobs/{job['id']}",params={"owner":"b"}).status_code==404
    assert client.post(f"/jobs/{job['id']}/retry",params={"owner":"a"}).status_code==400
    rows=client.get(f"/jobs/{job['id']}/export",params={"owner":"a"}).text.splitlines()
    assert len(rows)==3 and json.loads(rows[1])["pictures"]
    assert client.post("/jobs/import-browser",json={"owner":"a","snapshot":{}}).status_code==400
