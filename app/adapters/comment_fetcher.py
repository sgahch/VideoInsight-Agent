"""观众评论抓取适配层。

两条数据来源：
1. **抖音内置直连**（默认，零依赖）：`www.iesdouyin.com/web/api/v2/comment/list/`
   实测仍开放——不需要 cookies、不需要 a_bogus 签名，返回热门评论
   （固定约 10 条，count/cursor 参数均无效；二级回复接口在 v2 下已下线）。
   注意 web 端 `www.douyin.com/aweme/v1/web/comment/list/` 与 detail 接口一样
   被签名墙挡住（返回空 body），不能用。
2. **MediaCrawler 对接**（其他平台/更多评论）：MediaCrawler 作为**独立服务/进程**
   运行（Playwright + 本机 Chrome 登录态），把评论落盘到它自己的 data/ 目录；
   本模块只负责「按平台 + 视频 ID 把评论读回来并归一化」。
   - MediaCrawlerFileFetcher —— 读 MediaCrawler 输出的 CSV/JSON/JSONL/SQLite 文件。
   - MediaCrawlerApiFetcher  —— 尝试通过 MediaCrawler 的 api(:8080) 拉评论（端点待核实，失败即降级）。
3. MockCommentFetcher        —— 开发/测试用，返回样例评论。

任何后端异常都**优雅降级为返回空列表**，绝不阻断主分析链路。
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.adapters.downloader import normalize_url
from app.config import Settings
from app.schemas import CommentItem


# VideoInsight 平台名(detect_platform) -> MediaCrawler 平台简码
_PLATFORM_TO_MC = {
    "抖音": "dy",
    "快手": "ks",
    "B站": "bili",
    "小红书": "xhs",
    "微博": "wb",
    "百度贴吧": "tieba",
    "知乎": "zhihu",
}

# 各平台从 URL 抽取原生视频 ID 的正则（仅抽取确定性高的；短链/无法解析时返回 None）。
_ID_PATTERNS = {
    "B站": [r"bilibili\.com/video/(BV[0-9A-Za-z]+)"],
    "抖音": [r"douyin\.com/video/(\d+)"],
    "快手": [r"kuaishou\.com/short-video/(\w+)", r"gifshow\.com/[^\?]*?/photo/(\w+)"],
    "小红书": [r"xiaohongshu\.com/(?:explore|discovery/item)/(\w+)"],
    "知乎": [r"zhihu\.com/(?:question|answer)/(\d+)"],
    "微博": [r"weibo\.com/(\d{10,})"],
    "百度贴吧": [r"tieba\.baidu\.com/p/(\d+)"],
}


def detect_platform_cn(url: str) -> str:
    """与 workflow.detect_platform 保持一致的平台识别（避免循环导入，本地实现）。"""
    value = url.strip().lower()
    if value.startswith("upload://"):
        return "上传文件"
    if any(host in value for host in ("kuaishou.com", "chenzhongtech.com", "gifshow.com")):
        return "快手"
    if "douyin.com" in value:
        return "抖音"
    if "bilibili.com" in value or "b23.tv" in value:
        return "B站"
    if "xiaohongshu.com" in value or "xhslink" in value:
        return "小红书"
    if "weibo.com" in value or "weibo.cn" in value:
        return "微博"
    if "tieba.baidu.com" in value:
        return "百度贴吧"
    if "zhihu.com" in value:
        return "知乎"
    return "未知平台"


def extract_platform_video_id(url: str) -> tuple[str | None, str | None]:
    """从 URL 抽取 (MediaCrawler 平台简码, 原生视频 ID)。抽不到返回 (None, None)。"""
    platform_cn = detect_platform_cn(url)
    mc_platform = _PLATFORM_TO_MC.get(platform_cn)
    if not mc_platform:
        return None, None
    patterns = _ID_PATTERNS.get(platform_cn, [])
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return mc_platform, match.group(1)
    return mc_platform, None


@runtime_checkable
class CommentBackend(Protocol):
    def fetch(self, mc_platform: str, video_id: str) -> list[CommentItem]:
        ...


def _row_to_comment(row: dict[str, str]) -> CommentItem | None:
    """把一行原始评论字段映射为归一化 CommentItem；缺 comment_id 则丢弃。"""
    norm = {str(k).strip().lower(): v for k, v in row.items()}
    comment_id = (norm.get("comment_id") or "").strip()
    if not comment_id:
        return None
    content = (norm.get("content") or norm.get("message") or norm.get("comment") or "").strip()
    try:
        like_count = int(str(norm.get("like_count") or norm.get("like") or 0).strip() or 0)
    except ValueError:
        like_count = 0
    return CommentItem(
        comment_id=comment_id,
        parent_comment_id=(norm.get("parent_comment_id") or norm.get("parent") or "").strip() or None,
        content=content,
        author_nickname=(
            norm.get("nickname") or norm.get("user_name") or norm.get("uname") or norm.get("name") or ""
        ).strip() or None,
        like_count=like_count,
        create_time=(norm.get("create_time") or norm.get("ctime") or norm.get("add_ts") or "").strip() or None,
    )


def _dedupe(comments: list[CommentItem]) -> list[CommentItem]:
    seen: set[str] = set()
    out: list[CommentItem] = []
    for c in comments:
        if c.comment_id in seen:
            continue
        seen.add(c.comment_id)
        out.append(c)
    return out


def _build_comment_tree(comments: list[CommentItem]) -> list[CommentItem]:
    """把扁平评论按 parent_comment_id 还原成树，填充每条约评论的 sub_comments。

    - 一级评论（无父或父不在列表中）进入 roots；
    - 二级/楼中楼评论挂到对应父评论的 sub_comments 下（前端 renderComments 递归渲染）。
    - 同一评论对象被复用，不会重复计入 roots。
    """
    by_id: dict[str, CommentItem] = {c.comment_id: c for c in comments}
    roots: list[CommentItem] = []
    for comment in comments:
        parent_id = comment.parent_comment_id
        parent = by_id.get(parent_id) if parent_id else None
        if parent is not None and parent is not comment:
            parent.sub_comments.append(comment)
        else:
            roots.append(comment)
    return roots


class MediaCrawlerFileFetcher:
    """读 MediaCrawler 落盘的评论文件（默认 data/ 目录）。

    MediaCrawler 按 `data/<平台>/<类型>_comments/<id>_comments.<csv|json|jsonl>` 之类命名；
    这里做成宽松匹配：在 output_dir 下递归查找「文件名含 video_id」且扩展名受支持的文件，
    解析后归一化。多个匹配文件会合并（按 comment_id 去重）。
    """

    SUPPORTED_EXT = {".csv", ".json", ".jsonl", ".sqlite", ".db"}

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)

    def fetch(self, mc_platform: str, video_id: str) -> list[CommentItem]:
        if not self.output_dir.exists():
            return []
        matches: list[Path] = []
        for path in self.output_dir.rglob("*"):
            if not path.is_file():
                continue
            if video_id not in path.name:
                continue
            if path.suffix.lower() not in self.SUPPORTED_EXT:
                continue
            matches.append(path)
        comments: list[CommentItem] = []
        for path in matches:
            comments.extend(self._parse_file(path))
        return _dedupe(comments)

    def _parse_file(self, path: Path) -> list[CommentItem]:
        ext = path.suffix.lower()
        if ext == ".csv":
            return self._parse_csv(path)
        if ext in {".json", ".jsonl"}:
            return self._parse_json(path)
        if ext in {".sqlite", ".db"}:
            return self._parse_sqlite(path)
        return []

    def _parse_csv(self, path: Path) -> list[CommentItem]:
        rows: list[CommentItem] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                comment = _row_to_comment(row or {})
                if comment:
                    rows.append(comment)
        return rows

    def _parse_json(self, path: Path) -> list[CommentItem]:
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 可能是 JSONL（每行一个对象）
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(data, dict):
            data = data.get("comments") or data.get("data") or []
        if not isinstance(data, list):
            return []
        rows: list[CommentItem] = []
        for item in data:
            if isinstance(item, dict):
                comment = _row_to_comment(item)
                if comment:
                    rows.append(comment)
        return rows

    def _parse_sqlite(self, path: Path) -> list[CommentItem]:
        # SQLite 表结构随 MediaCrawler 版本变化，这里做尽力而为的通用读取。
        try:
            conn = sqlite3.connect(str(path))
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%comment%' LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return []
            table = row[0]
            cur.execute(f"SELECT * FROM {table}")  # noqa: S608 - 表名来自 sqlite_master，非外部输入
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.close()
        except (sqlite3.Error, OSError):
            return []
        return [_row_to_comment(r) for r in rows if _row_to_comment(r)]


class MediaCrawlerApiFetcher:
    """通过 MediaCrawler 的 api 服务(:8080) 拉评论。

    注意：MediaCrawler 的 api 主要暴露「控制爬取 + 数据文件列表」，并未在 main.py 中提供
    干净的「按 platform+id 返回评论」端点（端点待核实）。这里尝试一个合理猜测路径，
    **任何失败都返回空列表**，不阻断主链路。生产接入前请在 MediaCrawler api/routers 中确认实际端点。
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def fetch(self, mc_platform: str, video_id: str) -> list[CommentItem]:
        import httpx

        try:
            resp = httpx.get(
                f"{self.base_url}/api/data/comments",
                params={"platform": mc_platform, "id": video_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("comments") or data.get("data") or []
        if not isinstance(data, list):
            return []
        return _dedupe([c for c in (_row_to_comment(i) for i in data if isinstance(i, dict)) if c])


class DouyinCommentFetcher:
    """直连抖音评论接口，无需 MediaCrawler、浏览器或登录态。

    实测结论（2026-09）：
    - `www.iesdouyin.com/web/api/v2/comment/list/?aweme_id=<id>` 直接返回热门评论，
      不需要 cookies，也不需要 a_bogus 签名。
    - 该接口固定返回约 10 条：count 参数（5/20/50/100）与 cursor（0/10/20/30）均无效，
      响应里没有分页游标，因此不做翻页，拿到多少算多少。
    - 二级回复（楼中楼）接口在 v2 下已下线（返回 "Url doesn't match"），
      所以只产出一级评论；`reply_comment_total` 目前无处展示，未纳入。
    """

    COMMENT_API = "https://www.iesdouyin.com/web/api/v2/comment/list/?aweme_id={video_id}&count=20&cursor=0"
    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )

    def fetch(self, mc_platform: str, video_id: str) -> list[CommentItem]:
        if mc_platform != "dy":
            return []
        import httpx

        try:
            response = httpx.get(
                self.COMMENT_API.format(video_id=video_id),
                headers={"User-Agent": self.MOBILE_UA},
                timeout=15,
                verify=False,
                trust_env=False,
            )
            response.raise_for_status()
            data = response.json()
        except Exception:  # 网络/解析异常一律降级为空，不阻断主链路
            return []

        raw = data.get("comments") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []

        comments: list[CommentItem] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("cid") or "").strip()
            if not cid:
                continue
            author = item.get("user") if isinstance(item.get("user"), dict) else {}
            try:
                like_count = int(item.get("digg_count") or 0)
            except (TypeError, ValueError):
                like_count = 0
            comments.append(
                CommentItem(
                    comment_id=cid,
                    parent_comment_id=None,
                    content=str(item.get("text") or "").strip(),
                    author_nickname=(str(author.get("nickname") or "").strip() or None),
                    like_count=like_count,
                    create_time=_format_timestamp(item.get("createTime")),
                )
            )
        return _dedupe(comments)


def _format_timestamp(value: object) -> str | None:
    """抖音 createTime 是 Unix 秒；转成本地可读时间，失败返回 None。"""
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds))
    except (OverflowError, OSError, ValueError):
        return None


class MockCommentFetcher:
    """开发/测试用：返回样例评论，便于在没有真实 MediaCrawler 时验证整条链路。"""

    def fetch(self, mc_platform: str, video_id: str) -> list[CommentItem]:
        return [
            CommentItem(
                comment_id="mock-1",
                parent_comment_id=None,
                content="这期讲得真好，Transformer 的那段一下就懂了！",
                author_nickname="观众A",
                like_count=128,
                create_time="2026-09-10 20:11",
            ),
            CommentItem(
                comment_id="mock-2",
                parent_comment_id=None,
                content="求问 KV Cache 是啥？没太跟上。",
                author_nickname="观众B",
                like_count=42,
                create_time="2026-09-10 20:30",
            ),
            CommentItem(
                comment_id="mock-2-1",
                parent_comment_id="mock-2",
                content="KV Cache 就是缓存历史 Key/Value，避免每步重复算，省显存还快。",
                author_nickname="观众C",
                like_count=15,
                create_time="2026-09-10 20:35",
            ),
        ]


def get_comment_fetcher(settings: Settings) -> CommentBackend | None:
    """按配置返回可用的评论后端；两者都未配置时返回 None（不抓评论）。"""
    if settings.media_crawler_api_url:
        return MediaCrawlerApiFetcher(settings.media_crawler_api_url)
    if settings.media_crawler_output_dir:
        return MediaCrawlerFileFetcher(settings.media_crawler_output_dir)
    return None


def fetch_comments_for_url(
    url: str,
    settings: Settings,
    *,
    backend: CommentBackend | None = None,
    force: bool = False,
) -> tuple[list[CommentItem], str | None]:
    """对一条视频链接尝试抓取观众评论。

    参数：
        url: 视频链接（支持分享文案，调用方应先归一化为干净 URL）。
        settings: 运行时配置（决定后端）。
        backend: 测试可注入自定义后端；否则按配置取。
        force: 抽不到 video_id 时也允许调用（一般用于 Mock 测试）。
    返回：
        (comments, source_label) —— 失败时 comments 为空列表，source_label 描述原因或来源。
    """
    fetcher = backend if backend is not None else get_comment_fetcher(settings)

    # 抖音短链（v.douyin.com/xxx）必须先归一化成 /video/<id>，否则抽不到原生 ID。
    normalized = normalize_url(url)
    mc_platform, video_id = extract_platform_video_id(normalized)
    if video_id is None:
        if not force:
            return [], f"无法从链接抽取视频 ID（平台={mc_platform or '未知'}），跳过评论抓取"
        # force 模式（如 Mock 测试）：用占位 id 调用
        mc_platform = mc_platform or "bili"
        video_id = video_id or "mock-id"

    # 抖音走内置直连接口（零依赖）；其余平台只能靠 MediaCrawler。
    candidates: list[tuple[str, CommentBackend]] = []
    if mc_platform == "dy":
        candidates.append(
            (f"抖音评论接口::{video_id}", DouyinCommentFetcher())  # type: ignore[arg-type]
        )
    if fetcher is not None:
        candidates.append((f"MediaCrawler::{mc_platform}/{video_id}", fetcher))
    if not candidates:
        return [], (
            "未配置评论数据源：抖音走内置接口，其他平台需设置 "
            "media_crawler_output_dir / media_crawler_api_url"
        )

    errors: list[str] = []
    for label, candidate in candidates:
        try:
            comments = candidate.fetch(mc_platform, video_id)
        except Exception as exc:  # 任何异常都降级，不影响主分析
            errors.append(f"{label} 异常: {exc}")
            continue
        if comments:
            # 还原二级评论树（爬虫落盘是扁平的 parent_comment_id），供前端递归渲染。
            return _build_comment_tree(_dedupe(comments)), label
    detail = errors[0] if errors else f"未找到该视频({mc_platform}/{video_id})的评论数据"
    return [], detail
