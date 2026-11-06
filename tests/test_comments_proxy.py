from dataclasses import replace
import ssl

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.comments_api as proxy
from app.config import Settings


@pytest.fixture
def client(monkeypatch):
    app=FastAPI();app.include_router(proxy.router)
    calls=[]
    def upstream(method,path,**kwargs):
        calls.append((method,path,kwargs))
        return {"id":"a"*32,"ok":True}
    monkeypatch.setattr(proxy,"service_request",upstream)
    return TestClient(app),calls


def test_share_text_and_browser_owner_are_forwarded(client):
    browser,calls=client
    response=browser.post("/comment-api/jobs",headers={"X-Client-Id":"browser-a"},json={"url":"复制打开看看 https://www.bilibili.com/video/BV1zcJf6LEPe/?share_source=copy_web"})
    assert response.status_code==202
    assert calls[-1][2]["json"]["owner"]=="browser-a"
    assert calls[-1][2]["json"]["url"].endswith("?share_source=copy_web")
    assert browser.post("/comment-api/jobs",json={"url":"missing link"}).status_code==400


def test_health_is_proxied_without_exposing_service_credentials(client):
    browser,calls=client
    assert browser.get("/comment-api/health").status_code==200
    assert calls==[("GET","/health",{})]


def test_main_health_identifies_configured_background_service(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main,"settings",replace(main.settings,
        comment_crawler_url="http://127.0.0.1:8090",comment_crawler_token="private"))
    assert main.health()["comment_source"]=="mediacrawler-service"
    assert "private" not in str(main.health())


def test_proxy_reuses_tls_context_without_disabling_verification():
    first=proxy.service_tls_context()
    assert first is proxy.service_tls_context()
    assert first.verify_mode==ssl.CERT_REQUIRED
    assert first.check_hostname is True


def test_proxy_checks_platform_ids_actions_and_paginates(client):
    browser,calls=client
    assert browser.put("/comment-api/platforms/unknown/cookies",json={"content":"a=b"}).status_code==404
    assert browser.get("/comment-api/jobs/not-an-id").status_code==404
    assert browser.post("/comment-api/jobs/"+"a"*32+"/bad-action").status_code==404
    browser.cookies.set("videoinsight_client_id","cookie-owner")
    assert browser.get("/comment-api/jobs/"+"a"*32+"/comments?offset=-3").status_code==200
    assert calls[-1][2]["params"]=={"owner":"cookie-owner","offset":0,"limit":100,"sort":"collected","scope":"all"}


def test_proxy_forwards_ranking_scope_thread_and_top_limit(client):
    browser,calls=client
    response=browser.get("/comment-api/jobs/"+"a"*32+"/comments",params={
        "sort":"likes","scope":"replies","root_id":"9007199254740993","top":10,"limit":999})
    assert response.status_code==200
    assert calls[-1][2]["params"]=={
        "owner":"","offset":0,"limit":200,"sort":"likes","scope":"replies",
        "root_id":"9007199254740993","top":10}


@pytest.mark.parametrize("params",[
    {"sort":"likes DESC; DROP TABLE comments"}, {"scope":"unknown"}, {"top":0}, {"top":1001}, {"root_id":""},
])
def test_proxy_rejects_invalid_filters_without_contacting_upstream(client,params):
    browser,calls=client
    assert browser.get("/comment-api/jobs/"+"a"*32+"/comments",params=params).status_code==422
    assert calls==[]


def test_browser_snapshot_import_uses_current_browser_owner(client):
    browser,calls=client
    response=browser.post("/comment-api/import-browser",headers={"X-Client-Id":"browser-a"},json={"snapshot":{"video_url":"bili"},"owner":"someone-else"})
    assert response.status_code==201
    assert calls[-1]==("POST","/jobs/import-browser",{"json":{"snapshot":{"video_url":"bili"},"owner":"browser-a"}})


def test_service_secret_is_only_sent_to_configured_server(monkeypatch):
    config=Settings(comment_crawler_url="http://crawler:8090",comment_crawler_token="server-private")
    monkeypatch.setattr(proxy,"settings",config)
    calls=[]
    def request(method,url,**kwargs):
        calls.append((method,url,kwargs))
        return httpx.Response(200,json={"status":"ok"})
    monkeypatch.setattr(proxy.httpx,"request",request)
    assert proxy.service_request("GET","/platforms")=={"status":"ok"}
    assert calls[0][1]=="http://crawler:8090/platforms"
    assert calls[0][2]["headers"]["Authorization"]=="Bearer server-private"
    assert calls[0][2]["trust_env"] is False
    monkeypatch.setattr(proxy,"settings",replace(config,comment_crawler_url=None))
    with pytest.raises(HTTPException) as exc:proxy.service_request("GET","/platforms")
    assert exc.value.status_code==503


def test_filtered_export_forwards_owner_and_ranking_without_buffering(client,monkeypatch):
    browser,_=client
    captured=[]
    original_client=httpx.Client
    def respond(request):
        captured.append(request)
        return httpx.Response(200,content=b'{"comment_id":"top"}\n')
    monkeypatch.setattr(proxy,"settings",Settings(
        comment_crawler_url="http://crawler:8090",comment_crawler_token="private"))
    monkeypatch.setattr(proxy.httpx,"Client",lambda **kwargs: original_client(
        transport=httpx.MockTransport(respond),**kwargs))
    response=browser.get("/comment-api/jobs/"+"a"*32+"/export",headers={"X-Client-Id":"mine"},
        params={"sort":"likes","scope":"roots","top":10})
    assert response.status_code==200 and response.text=='{"comment_id":"top"}\n'
    assert dict(captured[0].url.params)=={"owner":"mine","sort":"likes","scope":"roots","top":"10"}
    assert captured[0].headers["authorization"]=="Bearer private"
