"""从平台“分享文案”中解析出标准链接与标题标签。

典型输入（抖音示例）：
    7.94 复制打开抖音，看看【A1Lun项润平的作品】《不断"项"上》播客特辑《自律》 "自律不是把自己...
    https://v.douyin.com/Qi8xo72jFqo/ lcN:/ o@q.eO 04/14 :3pm

目标：无需用户手动截取，自动提取出干净的 URL、平台、作者、标题，
并生成一个标准化的展示标签，供提交与历史记录使用。

同样适用于 B站 / 快手 / 小红书 等分享文案（只要包含真实链接即可）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.workflow import detect_platform

# 完整 http(s) 链接：遇到空白或常见中文/引号闭合符即停止。
_FULL_URL_RE = re.compile(r"https?://[^\s\"'）)】〉]+", re.IGNORECASE)
# Markdown 链接中的真实地址，如 [标题](https://example.com/video)。
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)", re.IGNORECASE)
# 兜底：无 scheme 的裸域名短链，如 v.douyin.com/xxxx（需要真实域名后缀，避免误吃口令噪声）。
_BARE_URL_RE = re.compile(
    r"(?:[a-zA-Z0-9-]+\.)+(?:com|cn|net|org|tv|app|live|gov|edu|co)(?:/[^\s\"'）)】〉]+)?",
    re.IGNORECASE,
)
_BOILERPLATE_RE = re.compile(
    r"(?:复制打开[\u4e00-\u9fa5A-Za-z]*，?看看|复制打开|打开看看|长按复制|长按|点击链接|"
    r"该视频|更多精彩|打开抖音|打开快手|打开哔哩哔哩|打开小红书|打开B站|打开西瓜视频|看看)"
)
_LEADING_NOISE_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*")
_TRAILING_PUNCT_RE = re.compile(r"[\s，。、,.…:：;；\"'\"'“”‘’\-—]+$")
_BRACKET_RE = re.compile(r"【([^】]+)】")
_BOOKTITLE_RE = re.compile(r"《([^》]+)》")
# 抖音口令类噪声片段（如 lcN:/ o@q.eO 04/14 :3pm），不参与任何解析，仅用于判断“这是分享文案”。
_SHARE_HINT_RE = re.compile(r"(复制打开|长按复制|口令|lcN:|o@q\.eO|复制此链接|打开看看)", re.IGNORECASE)


@dataclass
class ParsedLink:
    raw: str
    url: str | None = None
    platform: str = "未知平台"
    author: str | None = None
    title: str | None = None
    label: str | None = None
    is_share_text: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "raw": self.raw,
            "url": self.url,
            "platform": self.platform,
            "author": self.author,
            "title": self.title,
            "label": self.label,
            "is_share_text": self.is_share_text,
            "note": self.note,
        }


def _extract_url(text: str) -> str | None:
    markdown_match = _MARKDOWN_LINK_RE.search(text)
    if markdown_match:
        candidate = markdown_match.group(1)
        return candidate.rstrip("/") or candidate
    match = _FULL_URL_RE.search(text)
    if match:
        return match.group(0).rstrip("/") or match.group(0)
    match = _BARE_URL_RE.search(text)
    if match:
        candidate = match.group(0)
        if candidate.lower().startswith("http"):
            return candidate.rstrip("/")
        return "https://" + candidate.rstrip("/")
    return None


def _extract_author(text: str) -> str | None:
    match = _BRACKET_RE.search(text)
    if not match:
        return None
    author = match.group(1).strip()
    # 分享文案常见写法：【作者名的作品】/【作者名的直播】等，去掉后缀保留作者名。
    author = re.sub(r"(的作品|的直播|的主页|的视频)$", "", author).strip()
    return author or None


def _extract_title(text: str, url: str | None) -> str | None:
    # 纯 Markdown 链接没有标题；若前后还有分享文案，只移除链接包装后继续提取。
    markdown_match = _MARKDOWN_LINK_RE.search(text)
    if markdown_match and url:
        markdown_url = markdown_match.group(1).rstrip("/")
        if markdown_url == url:
            before = text[: markdown_match.start()]
            after = text[markdown_match.end() :]
            if not before.strip() and not after.strip():
                return None
            text = f"{before} {after}"

    # 优先取《书名号》包裹的标题（可多个，常见“播客特辑《自律》”这种连续结构）。
    books = [b.strip() for b in _BOOKTITLE_RE.findall(text) if b.strip()]
    if books:
        return "《" + "》·《".join(books) + "》"

    # 否则取链接前面的主文案，去掉引导词与噪声。
    head = text
    if url:
        idx = head.find(url)
        if idx != -1:
            head = head[:idx]
    head = _BOILERPLATE_RE.sub("", head)
    head = _LEADING_NOISE_RE.sub("", head)
    head = _TRAILING_PUNCT_RE.sub("", head).strip()
    head = re.sub(r"\s+", " ", head)
    if not head:
        return None
    return head[:40]


def parse_share_text(text: str) -> ParsedLink:
    """解析分享文案或纯链接，返回标准化结果。"""
    raw = (text or "").strip()
    result = ParsedLink(raw=raw)
    if not raw:
        result.note = "内容为空，请提供链接或平台分享文案。"
        return result

    url = _extract_url(raw)
    result.url = url
    if not url:
        result.note = "未识别到有效链接，请确认粘贴内容包含以 http(s):// 开头的地址。"
        return result

    result.platform = detect_platform(url)
    result.author = _extract_author(raw)
    result.title = _extract_title(raw, url)

    if result.title:
        result.label = result.title
    else:
        result.label = f"{result.platform}分享链接"

    # 是否为分享文案（而非干净的纯链接）。忽略结尾斜杠差异，避免把带斜杠的干净链接误判为文案。
    result.is_share_text = (raw.rstrip("/") != url) or bool(_SHARE_HINT_RE.search(raw))
    if result.is_share_text:
        extracted_parts = []
        if result.author:
            extracted_parts.append("作者")
        if result.title:
            extracted_parts.append("标题")
        extracted_summary = "与".join(extracted_parts) if extracted_parts else "干净链接"
        result.note = (
            f"已从分享文案中解析出{result.platform}链接，已自动提取{extracted_summary}。"
        )
    else:
        result.note = "已识别为干净链接，无需裁剪。"
    return result
