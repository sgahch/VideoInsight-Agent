from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.link_parser import parse_share_text
from app.main import app


# ---- 纯函数单元测试 ----

def test_douyin_share_text() -> None:
    text = (
        '7.94 复制打开抖音，看看【A1Lun项润平的作品】《不断"项"上》播客特辑《自律》 '
        '"自律不是把自己... https://v.douyin.com/Qi8xo72jFqo/ lcN:/ o@q.eO 04/14 :3pm'
    )
    parsed = parse_share_text(text)
    assert parsed.url == "https://v.douyin.com/Qi8xo72jFqo"
    assert parsed.platform == "抖音"
    assert parsed.author == "A1Lun项润平"
    assert "《自律》" in (parsed.title or "")
    assert parsed.is_share_text is True
    assert parsed.label == parsed.title


def test_clean_douyin_url() -> None:
    url = "https://v.douyin.com/Qi8xo72jFqo/"
    parsed = parse_share_text(url)
    assert parsed.url == "https://v.douyin.com/Qi8xo72jFqo"
    assert parsed.platform == "抖音"
    assert parsed.is_share_text is False


def test_markdown_link_extracts_inner_url() -> None:
    parsed = parse_share_text("[https://v.douyin.com/G7m8cLTAtnY](https://v.douyin.com/G7m8cLTAtnY)")
    assert parsed.url == "https://v.douyin.com/G7m8cLTAtnY"
    assert parsed.title is None
    assert parsed.platform == "抖音"
    assert "干净链接" in parsed.note


def test_bilibili_share_text() -> None:
    text = "【老高与小茉】为什么人类会做梦？ https://www.bilibili.com/video/BV1xx411c7mD/ 复制打开看看"
    parsed = parse_share_text(text)
    assert parsed.platform == "B站"
    assert parsed.url is not None and "bilibili.com" in parsed.url


def test_kuaishou_share_text() -> None:
    text = "复制打开快手，看看【某作者的作品】带你云游西藏 https://v.kuaishou.com/abc123 口令123"
    parsed = parse_share_text(text)
    assert parsed.platform == "快手"
    assert parsed.url is not None and "kuaishou.com" in parsed.url


def test_xiaohongshu_share_text() -> None:
    text = "复制打开小红书，看看《周末探店》这家咖啡店绝了 http://xhslink.com/a/xyz789"
    parsed = parse_share_text(text)
    assert parsed.platform == "小红书"
    assert parsed.url is not None and "xhslink.com" in parsed.url


def test_title_from_book_quotes() -> None:
    text = "《不断项上》播客特辑《自律》 https://v.douyin.com/Qi8xo72jFqo/"
    parsed = parse_share_text(text)
    assert parsed.title == "《不断项上》·《自律》"


def test_no_url() -> None:
    parsed = parse_share_text("这只是一个普通的句子，没有链接")
    assert parsed.url is None
    assert parsed.note  # 应有提示


def test_empty_input() -> None:
    parsed = parse_share_text("")
    assert parsed.url is None


def test_bare_short_link_without_scheme() -> None:
    text = "v.douyin.com/Qi8xo72jFqo 复制打开抖音看看"
    parsed = parse_share_text(text)
    assert parsed.url == "https://v.douyin.com/Qi8xo72jFqo"
    assert parsed.platform == "抖音"


# ---- 接口测试 ----

@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_parse_link_endpoint(client: TestClient) -> None:
    text = (
        '7.94 复制打开抖音，看看【A1Lun项润平的作品】《自律》 '
        "https://v.douyin.com/Qi8xo72jFqo/ lcN:/ o@q.eO"
    )
    resp = client.post("/parse-link", json={"text": text})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://v.douyin.com/Qi8xo72jFqo"
    assert data["platform"] == "抖音"
    assert data["is_share_text"] is True


def test_analyze_normalizes_share_text(client: TestClient) -> None:
    text = "复制打开抖音，看看《自律》 https://v.douyin.com/Qi8xo72jFqo/ lcN:/ o@q.eO"
    resp = client.post(
        "/analyze",
        json={"url": text, "mode": "transcript", "use_mock": True},
    )
    assert resp.status_code == 202
    body = resp.json()
    task_id = body["task_id"]
    # 任务记录中的 url 应被归一化为干净链接，并自动带上标签
    detail = client.get(f"/tasks/{task_id}").json()
    assert detail["request"]["url"] == "https://v.douyin.com/Qi8xo72jFqo"
    assert detail["request"]["label"] == "《自律》"
