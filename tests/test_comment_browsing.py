import json
import sqlite3
from datetime import datetime, timezone

import pytest

from crawler_service.store import Store, comment_timestamp
from tests.test_comment_service import service


def populate(store, job_id):
    store.save(job_id, [
        {"comment_id":str(i), "content":f"Comment {i}", "like_count":i,
         "create_time":1700000000+i} for i in range(125)
    ] + [
        {"comment_id":"reply-a","root_comment_id":"124","parent_comment_id":"124",
         "like_count":1000,"create_time":1700000200000,"content":"Reply A"},
        {"comment_id":"reply-b","root_comment_id":"124","parent_comment_id":"reply-a",
         "like_count":999,"create_time":"2025-01-01T08:00:00+08:00","content":"Reply B"},
        {"comment_id":"unknown","like_count":0,"content":"No timestamp"},
    ])


def test_global_ranking_scope_counts_and_top_ten_are_not_limited_to_first_page(service):
    client,module,_=service
    job=module.store.create("a","https://v.douyin.com/test/")
    populate(module.store,job["id"])
    url=f"/jobs/{job['id']}/comments"
    response=client.get(url,params={"owner":"a","sort":"likes","scope":"roots","top":10})
    assert response.status_code==200
    page=response.json()
    assert page["total"]==10 and page["matched_total"]==126 and page["job_total"]==128
    assert [r["comment_id"] for r in page["items"]]==[str(i) for i in range(124,114,-1)]
    assert page["items"][0]["reply_count"]==2
    second=client.get(url,params={"owner":"a","sort":"likes","scope":"roots","top":10,"offset":8,"limit":100}).json()
    assert len(second["items"])==2
    assert client.get(url,params={"owner":"a","top":10,"offset":10}).json()["items"]==[]
    assert client.get(url,params={"owner":"a","sort":"likes","scope":"all"}).json()["items"][0]["comment_id"]=="reply-a"
    assert client.get(url,params={"owner":"a","sort":"likes","offset":100,"limit":20}).json()["items"][0]["comment_id"]=="26"
    filtered=client.get(url,params={"owner":"a","scope":"replies"}).json()
    assert filtered["total"]==len(filtered["items"])==2


def test_latest_uses_numeric_time_across_seconds_milliseconds_iso_and_unknown(service):
    client,module,_=service
    job=module.store.create("a","https://v.douyin.com/test/")
    populate(module.store,job["id"])
    page=client.get(f"/jobs/{job['id']}/comments",params={"owner":"a","sort":"newest","limit":200}).json()
    assert [r["comment_id"] for r in page["items"][:3]]==["reply-b","reply-a","124"]
    assert page["items"][-1]["comment_id"]=="unknown"


def test_thread_pagination_export_and_owner_isolation(service):
    client,module,_=service
    job=module.store.create("a","https://v.douyin.com/test/")
    populate(module.store,job["id"])
    url=f"/jobs/{job['id']}"
    params={"owner":"a","scope":"replies","root_id":"124","limit":1}
    first=client.get(url+"/comments",params=params).json()
    second=client.get(url+"/comments",params={**params,"offset":1}).json()
    assert first["total"]==2
    assert first["items"][0]["comment_id"]=="reply-a"
    assert second["items"][0]["parent_comment_id"]=="reply-a"
    assert client.get(url+"/comments",params={**params,"owner":"other"}).status_code==404
    assert client.get(url+"/export",params={**params,"owner":"other"}).status_code==404
    assert client.get(url+"/comments",params={**params,"root_id":"missing"}).json()["total"]==0
    exported=client.get(url+"/export",params={"owner":"a","sort":"likes","scope":"roots","top":10})
    rows=[json.loads(line) for line in exported.text.splitlines()]
    assert len(rows)==10 and rows[0]["comment_id"]=="124" and rows[-1]["comment_id"]=="115"
    assert len(client.get(url+"/export",params={"owner":"a"}).text.splitlines())==128


@pytest.mark.parametrize("params",[
    {"sort":"bad"},{"scope":"bad"},{"top":0},{"top":1001},{"root_id":""},
])
def test_service_rejects_invalid_filters(service,params):
    client,module,_=service
    job=module.store.create("a","https://v.douyin.com/test/")
    assert client.get(f"/jobs/{job['id']}/comments",params={"owner":"a",**params}).status_code==422
    assert client.get(f"/jobs/{job['id']}/export",params={"owner":"a",**params}).status_code==422


@pytest.mark.parametrize("value,expected",[
    (None,None),("",None),("not-a-date",None),("nan",None),("inf",None),(0,None),
    ("1700000000000",1700000000),(1700000000,1700000000),
    ("2025-01-01T08:00:00+08:00",datetime(2025,1,1,tzinfo=timezone.utc).timestamp()),
    ("2025-01-01 00:00:00",datetime(2025,1,1,tzinfo=timezone.utc).timestamp()),
])
def test_timestamp_normalization(value,expected):
    assert comment_timestamp(value)==expected


def test_old_database_backfills_sort_time_and_upserts_update_ranking(tmp_path):
    path=tmp_path/"comments.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE comments(job_id TEXT NOT NULL,id TEXT NOT NULL,parent TEXT,root TEXT,content TEXT NOT NULL,nickname TEXT,likes INTEGER NOT NULL DEFAULT 0,created TEXT,PRIMARY KEY(job_id,id))")
        db.executemany("INSERT INTO comments VALUES('job',?,NULL,NULL,'text',NULL,?,?)",[
            ("old",1,"1700000000"),("new",2,"1700000001000")])
    store=Store(path)
    assert [r["comment_id"] for r in store.comments("job",sort="newest")]==["new","old"]
    store.save("job",[{"comment_id":"old","content":"updated","like_count":99,"create_time":"2025-01-01T00:00:00Z"}])
    assert [r["comment_id"] for r in store.comments("job",sort="newest")]==["old","new"]
    assert store.comments("job",sort="likes")[0]["like_count"]==99
    assert Store(path).comment_count("job")==2
