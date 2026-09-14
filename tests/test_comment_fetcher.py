"""评论抓取适配层测试：后端（file/api/mock）、优雅降级、与 workflow 集成。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.adapters import downloader as downloader_module
from app.adapters.comment_fetcher import (
    DouyinCommentFetcher,
    MediaCrawlerApiFetcher,
    MediaCrawlerFileFetcher,
    MockCommentFetcher,
    _build_comment_tree,
    _format_timestamp,
    extract_platform_video_id,
    fetch_comments_for_url,
    get_comment_fetcher,
)
from app.config import Settings
from app.prompts import build_prompt, render_comments_block
from app.schemas import AnalysisMode, AnalyzeRequest, CommentItem


# ---------------- 平台/ID 抽取 ----------------

def test_extract_bilibili_id():
    mc, vid = extract_platform_video_id("https://www.bilibili.com/video/BV1xx123")
    assert mc == "bili" and vid == "BV1xx123"


def test_extract_douyin_id():
    mc, vid = extract_platform_video_id("https://www.douyin.com/video/7311223344556")
    assert mc == "dy" and vid == "7311223344556"


def test_extract_short_link_returns_no_id():
    # 短链无原生 ID，应优雅返回 None（不阻断主链路）
    mc, vid = extract_platform_video_id("https://v.douyin.com/AbC12Xy/")
    assert mc == "dy" and vid is None


def test_extract_unknown_platform():
    mc, vid = extract_platform_video_id("https://example.com/foo")
    assert mc is None and vid is None


# ---------------- Mock 后端 ----------------

def test_mock_fetcher_returns_sample():
    comments = MockCommentFetcher().fetch("bili", "BV1")
    assert len(comments) == 3
    assert comments[0].comment_id == "mock-1"
    assert comments[2].parent_comment_id == "mock-2"


# ---------------- 二级评论树重建（核心修复） ----------------

def test_build_comment_tree_groups_replies():
    flat = [
        CommentItem(comment_id="a", content="顶层A"),
        CommentItem(comment_id="a1", content="回复A", parent_comment_id="a"),
        CommentItem(comment_id="a1-1", content="楼中楼", parent_comment_id="a1"),
        CommentItem(comment_id="b", content="顶层B"),
    ]
    tree = _build_comment_tree(flat)
    assert [c.comment_id for c in tree] == ["a", "b"]
    a = next(c for c in tree if c.comment_id == "a")
    assert [c.comment_id for c in a.sub_comments] == ["a1"]
    a1 = a.sub_comments[0]
    assert [c.comment_id for c in a1.sub_comments] == ["a1-1"]


def test_fetch_comments_for_url_builds_tree():
    # 经顶层入口拿到的评论必须是树（前端靠 sub_comments 递归渲染楼中楼）
    comments, _ = fetch_comments_for_url(
        "https://www.bilibili.com/video/BVtree",
        Settings(),
        backend=MockCommentFetcher(),
        force=True,
    )
    parent = next(c for c in comments if c.comment_id == "mock-2")
    assert [c.comment_id for c in parent.sub_comments] == ["mock-2-1"]


def test_analyze_injects_comment_tree(tmp_path: Path):
    mc_dir = tmp_path / "mc_data"
    _write_csv(
        mc_dir / "bili",
        "BVtree2",
        [
            {"comment_id": "p", "content": "父评", "nickname": "pa", "like_count": "5"},
            {"comment_id": "q", "content": "子评", "parent_comment_id": "p", "nickname": "ch", "like_count": "2"},
        ],
    )
    settings = Settings(
        mock_mode=True,
        media_crawler_output_dir=mc_dir,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "db.sqlite",
    )
    request = AnalyzeRequest(
        url="https://www.bilibili.com/video/BVtree2",
        mode=AnalysisMode.summary,
        use_mock=True,
        with_comments=True,
    )
    from app.workflow import analyze

    result = analyze(request, "task-comment-tree", settings)
    assert result.comments is not None
    assert len(result.comments) == 1  # 只有父评在顶层
    parent = result.comments[0]
    assert parent.comment_id == "p"
    assert [c.comment_id for c in parent.sub_comments] == ["q"]  # 子评挂到 sub_comments


# ---------------- 文件后端 ----------------

def _write_csv(directory: Path, video_id: str, rows: list[dict]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{video_id}_comments.csv"
    # 取所有行的字段并集，避免某行多/少字段时报错
    fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def test_file_fetcher_reads_csv(tmp_path: Path):
    rows = [
        {"comment_id": "c1", "content": "好视频", "nickname": "甲", "like_count": "10", "parent_comment_id": ""},
        {"comment_id": "c2", "content": "求教", "nickname": "乙", "like_count": "3", "parent_comment_id": ""},
    ]
    _write_csv(tmp_path / "bili", "BVtest", rows)
    comments = MediaCrawlerFileFetcher(tmp_path).fetch("bili", "BVtest")
    assert len(comments) == 2
    assert comments[0].author_nickname == "甲"
    assert comments[0].like_count == 10


def test_file_fetcher_reads_json(tmp_path: Path):
    data = [
        {"comment_id": "c1", "message": "第一条", "uname": "丙", "like": 7},
        {"comment_id": "c2", "message": "第二条", "uname": "丁", "like": 1},
    ]
    (tmp_path / "dy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dy" / "7311_comments.json").write_text(json.dumps(data), encoding="utf-8")
    comments = MediaCrawlerFileFetcher(tmp_path).fetch("dy", "7311")
    assert len(comments) == 2
    assert comments[1].content == "第二条"


def test_file_fetcher_no_match_returns_empty(tmp_path: Path):
    comments = MediaCrawlerFileFetcher(tmp_path).fetch("bili", "does-not-exist")
    assert comments == []


def test_file_fetcher_missing_dir_returns_empty(tmp_path: Path):
    comments = MediaCrawlerFileFetcher(tmp_path / "nope").fetch("bili", "BVx")
    assert comments == []


# ---------------- API 后端（端点待核实，失败降级） ----------------

def test_api_fetcher_parses_list(monkeypatch: pytest.MonkeyPatch):
    import httpx

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"comments": [{"comment_id": "a1", "content": "api评论", "nickname": "E"}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    comments = MediaCrawlerApiFetcher("http://127.0.0.1:8080").fetch("bili", "BVx")
    assert len(comments) == 1
    assert comments[0].content == "api评论"


def test_api_fetcher_error_degrades_to_empty(monkeypatch: pytest.MonkeyPatch):
    import httpx

    def _boom(*a, **k):
        raise httpx.HTTPError("conn refused")

    monkeypatch.setattr(httpx, "get", _boom)
    comments = MediaCrawlerApiFetcher("http://127.0.0.1:8080").fetch("bili", "BVx")
    assert comments == []


# ---------------- 抖音内置直连评论接口 ----------------

_DOUYIN_API_PAYLOAD = {
    "comments": [
        {
            "cid": "7001",
            "text": "第一条评论",
            "digg_count": 19,
            "createTime": 1754000000,
            "user": {"nickname": "甲"},
        },
        {"cid": "7002", "text": "第二条评论", "digg_count": 0, "createTime": 0, "user": {}},
        {"cid": "7001", "text": "重复 cid 应被去重", "digg_count": 1, "createTime": 0, "user": {}},
        {"text": "没有 cid 应被丢弃", "digg_count": 5, "createTime": 0, "user": {}},
    ]
}


def _fake_douyin_api(monkeypatch: pytest.MonkeyPatch, payload: object) -> list[str]:
    """替换 httpx.get，记录被调用的 URL，返回给定 payload。"""
    import httpx

    called: list[str] = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def _get(url, *args, **kwargs):
        called.append(url)
        return _Resp()

    monkeypatch.setattr(httpx, "get", _get)
    return called


def test_douyin_fetcher_parses_comments(monkeypatch: pytest.MonkeyPatch):
    called = _fake_douyin_api(monkeypatch, _DOUYIN_API_PAYLOAD)
    comments = DouyinCommentFetcher().fetch("dy", "7679416235511106843")
    assert [c.comment_id for c in comments] == ["7001", "7002"]  # 无 cid 与重复项被剔除
    assert comments[0].content == "第一条评论"
    assert comments[0].author_nickname == "甲"
    assert comments[0].like_count == 19
    assert comments[0].create_time == _format_timestamp(1754000000)
    assert comments[1].author_nickname is None
    assert comments[1].create_time is None  # createTime=0 不生成假时间
    assert "aweme_id=7679416235511106843" in called[0]
    assert "iesdouyin.com" in called[0]


def test_douyin_fetcher_skips_other_platforms(monkeypatch: pytest.MonkeyPatch):
    called = _fake_douyin_api(monkeypatch, _DOUYIN_API_PAYLOAD)
    assert DouyinCommentFetcher().fetch("bili", "BV1") == []
    assert called == []  # 非抖音平台不应发起任何请求


def test_douyin_fetcher_network_error_degrades(monkeypatch: pytest.MonkeyPatch):
    import httpx

    def _boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "get", _boom)
    assert DouyinCommentFetcher().fetch("dy", "123") == []


def test_douyin_fetcher_non_dict_payload_degrades(monkeypatch: pytest.MonkeyPatch):
    _fake_douyin_api(monkeypatch, ["not", "a", "dict"])
    assert DouyinCommentFetcher().fetch("dy", "123") == []


def test_douyin_fetcher_empty_comments_degrades(monkeypatch: pytest.MonkeyPatch):
    _fake_douyin_api(monkeypatch, {"comments": []})
    assert DouyinCommentFetcher().fetch("dy", "123") == []


def test_format_timestamp_handles_garbage():
    assert _format_timestamp(None) is None
    assert _format_timestamp("abc") is None
    assert _format_timestamp(0) is None
    assert _format_timestamp(-1) is None
    assert _format_timestamp(1754000000) is not None


def test_fetch_douyin_works_without_media_crawler(monkeypatch: pytest.MonkeyPatch):
    """未配置 MediaCrawler 时，抖音短链也应走内置接口拿到评论。"""
    _fake_douyin_api(monkeypatch, _DOUYIN_API_PAYLOAD)
    monkeypatch.setattr(
        downloader_module,
        "resolve_douyin_short_url",
        lambda url: "https://www.douyin.com/video/7679416235511106843",
    )
    comments, source = fetch_comments_for_url("https://v.douyin.com/AbC12Xy/", Settings())
    assert len(comments) == 2
    assert source == "抖音评论接口::7679416235511106843"


def test_fetch_douyin_falls_back_to_media_crawler(monkeypatch: pytest.MonkeyPatch):
    """抖音接口返回空时，降级到已配置的 MediaCrawler 后端。"""

    class _Backend:
        def fetch(self, mc_platform, video_id):
            return [CommentItem(comment_id="mc1", content="来自 MediaCrawler")]

    _fake_douyin_api(monkeypatch, {"comments": []})
    comments, source = fetch_comments_for_url(
        "https://www.douyin.com/video/7311223344556", Settings(), backend=_Backend()
    )
    assert [c.content for c in comments] == ["来自 MediaCrawler"]
    assert source.startswith("MediaCrawler::")


# ---------------- 顶层入口与优雅降级 ----------------

def test_fetch_no_backend_configured():
    settings = Settings(media_crawler_api_url=None, media_crawler_output_dir=None)
    comments, source = fetch_comments_for_url("https://www.bilibili.com/video/BVx", settings)
    assert comments == []
    assert "未配置" in source


def test_fetch_unknown_platform_degrades():
    settings = Settings(media_crawler_output_dir=Path("/tmp/nope"))
    comments, source = fetch_comments_for_url("https://example.com/foo", settings)
    assert comments == []
    assert "平台" in source


def test_fetch_backend_exception_degrades():
    class _Boom:
        def fetch(self, mc_platform, video_id):
            raise RuntimeError("boom")

    comments, source = fetch_comments_for_url(
        "https://www.bilibili.com/video/BVx", Settings(), backend=_Boom(), force=True
    )
    assert comments == []
    assert "异常" in source


def test_get_comment_fetcher_prefers_api():
    settings = Settings(media_crawler_api_url="http://x", media_crawler_output_dir=Path("/y"))
    assert isinstance(get_comment_fetcher(settings), MediaCrawlerApiFetcher)


def test_get_comment_fetcher_file_fallback():
    settings = Settings(media_crawler_output_dir=Path("/y"))
    assert isinstance(get_comment_fetcher(settings), MediaCrawlerFileFetcher)


# ---------------- 提示词注入 ----------------

def test_prompt_includes_comments_block():
    comments = [CommentItem(comment_id="1", content="评论A", author_nickname="X", like_count=5)]
    block = render_comments_block(comments)
    prompt = build_prompt(
        AnalysisMode.summary, "转写文本示例", metadata={"video_title": "t"}, comments_text=block
    )
    assert "# 观众评论" in prompt
    assert "评论A" in prompt
    assert "{comments_block}" not in prompt  # 占位符必须被替换掉


def test_prompt_without_comments_has_no_block():
    prompt = build_prompt(AnalysisMode.summary, "转写文本", metadata={"video_title": "t"})
    assert "# 观众评论" not in prompt
    assert "{comments_block}" not in prompt


# ---------------- 与 analyze() 端到端（mock 模式，离线） ----------------

def test_analyze_injects_comments(tmp_path: Path):
    # 准备 MediaCrawler 输出目录，放一条带 bili 视频 ID 的评论 csv
    mc_dir = tmp_path / "mc_data"
    _write_csv(
        mc_dir / "bili",
        "BVend2end",
        [{"comment_id": "c1", "content": "端到端评论", "nickname": "路人", "like_count": "9"}],
    )
    settings = Settings(
        mock_mode=True,
        media_crawler_output_dir=mc_dir,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "db.sqlite",
    )
    request = AnalyzeRequest(
        url="https://www.bilibili.com/video/BVend2end",
        mode=AnalysisMode.summary,
        use_mock=True,
        with_comments=True,
    )
    from app.workflow import analyze

    result = analyze(request, "task-comment-e2e", settings)
    assert result.comments is not None
    assert len(result.comments) == 1
    assert result.comments[0].content == "端到端评论"
    assert result.diagnostics.get("comment_count") == 1
    assert "MediaCrawler" in (result.diagnostics.get("comments_source") or "")


def test_analyze_without_comments_skips(tmp_path: Path):
    settings = Settings(
        mock_mode=True,
        media_crawler_output_dir=None,
        tmp_dir=tmp_path / "tmp",
        results_dir=tmp_path / "results",
        media_dir=tmp_path / "media",
        db_path=tmp_path / "db.sqlite",
    )
    request = AnalyzeRequest(
        url="https://www.bilibili.com/video/BVskip",
        mode=AnalysisMode.summary,
        use_mock=True,
        with_comments=True,
    )
    from app.workflow import analyze

    result = analyze(request, "task-comment-skip", settings)
    assert result.comments is None
    assert "未配置" in (result.diagnostics.get("comments_source") or "")
