"""Trusted single-video inputs for accounts, separate from local admin tooling."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.url_safety import PublicOnlyTransport, UnsafeURL, validate_public_url


PLATFORM_HOSTS = {
    "douyin": {
        "douyin.com", "www.douyin.com", "m.douyin.com", "v.douyin.com",
        "iesdouyin.com", "www.iesdouyin.com",
    },
    "kuaishou": {
        "kuaishou.com", "www.kuaishou.com", "v.kuaishou.com",
        "m.kuaishou.com", "v.m.chenzhongtech.com", "m.gifshow.com",
    },
    "bilibili": {
        "bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv",
        "www.b23.tv", "bili2233.cn", "www.bili2233.cn",
    },
    "xiaohongshu": {
        "xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com",
        "xhslink.cn", "www.xhslink.cn",
    },
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"},
}
SHORT_HOSTS = {
    "v.douyin.com", "v.kuaishou.com", "b23.tv", "www.b23.tv",
    "bili2233.cn", "www.bili2233.cn", "xhslink.com", "www.xhslink.com",
    "xhslink.cn", "www.xhslink.cn",
}
_INPUT_ERROR = "普通账号仅支持抖音、快手、B站、小红书和 YouTube 的官方单视频链接。"


def media_hostname(raw: str) -> str | None:
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username is not None:
            return None
        return parsed.hostname
    except ValueError:
        return None


def media_platform(raw: str) -> str | None:
    host = media_hostname(raw)
    return next((name for name, hosts in PLATFORM_HOSTS.items() if host in hosts), None)


def _parameter(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise UnsafeURL(_INPUT_ERROR)
    return values[0]


def _canonical_shape(raw: str) -> tuple[str, bool]:
    if (
        not isinstance(raw, str) or not raw or len(raw) > 8192
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw)
        or "\\" in raw or "#" in raw
    ):
        raise UnsafeURL(_INPUT_ERROR)
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname or "@" in parsed.netloc
            or parsed.port not in (None, 443 if parsed.scheme.lower() == "https" else 80)
        ):
            raise UnsafeURL(_INPUT_ERROR)
    except ValueError as exc:
        raise UnsafeURL(_INPUT_ERROR) from exc
    platform = media_platform(raw)
    host, path = parsed.hostname, parsed.path
    if not platform:
        raise UnsafeURL(_INPUT_ERROR)
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
    except ValueError as exc:
        raise UnsafeURL(_INPUT_ERROR) from exc
    if host in SHORT_HOSTS:
        pattern = r"/(?:[a-z]/)?[A-Za-z0-9_-]{1,128}/?" if platform == "xiaohongshu" else r"/[A-Za-z0-9_-]{1,128}/?"
        if not re.fullmatch(pattern, path):
            raise UnsafeURL(_INPUT_ERROR)
        # Short links identify a video by path, never by a user-supplied next URL.
        return f"https://{host}{path.rstrip('/')}", True
    if platform == "douyin":
        match = re.fullmatch(r"/(?:share/)?(?:video|note)/([0-9]{1,30})/?", path)
        video_id = match[1] if match else (
            _parameter(query, "modal_id") if path in {"", "/", "/discover", "/recommend"} else None
        )
        if video_id and re.fullmatch(r"[0-9]{1,30}", video_id):
            return f"https://www.douyin.com/video/{video_id}", False
    elif platform == "kuaishou":
        match = re.fullmatch(r"/(?:short-video|fw/photo)/([A-Za-z0-9]{1,64})/?", path)
        if match:
            return f"https://www.kuaishou.com/short-video/{match[1]}", False
    elif platform == "bilibili":
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av[0-9]{1,20})/?", path)
        if match:
            page = _parameter(query, "p")
            if page is not None and not re.fullmatch(r"[1-9][0-9]{0,4}", page):
                raise UnsafeURL("B 站分 P 参数无效。")
            suffix = "?" + urlencode({"p": page}) if page else ""
            return f"https://www.bilibili.com/video/{match[1]}{suffix}", False
    elif platform == "xiaohongshu":
        match = re.fullmatch(r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?", path)
        if match:
            allowed = {}
            for name, limit in (("xsec_token", 2048), ("xsec_source", 64)):
                value = _parameter(query, name)
                if value is not None:
                    if not re.fullmatch(r"[A-Za-z0-9_+=/-]{1," + str(limit) + "}", value):
                        raise UnsafeURL(_INPUT_ERROR)
                    allowed[name] = value
            suffix = "?" + urlencode(allowed) if allowed else ""
            return f"https://www.xiaohongshu.com/explore/{match[1].lower()}{suffix}", False
    elif platform == "youtube":
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = path.strip("/")
        elif path == "/watch":
            video_id = _parameter(query, "v")
        else:
            match = re.fullmatch(r"/(?:shorts|embed)/([A-Za-z0-9_-]{11})/?", path)
            video_id = match[1] if match else None
        if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return f"https://www.youtube.com/watch?v={video_id}", False
    raise UnsafeURL(_INPUT_ERROR)


def canonical_media_url(raw: str, *, resolve_short: bool = False) -> str:
    """Whitelist shapes and parameters before any network request or CLI call.

    CLI extractors trust official platforms/CDNs, not arbitrary public websites.
    Native HTTP paths additionally pin public DNS answers for every request.
    """
    canonical, is_short = _canonical_shape(raw)
    validate_public_url(raw, allow_query=True)
    if not is_short or not resolve_short:
        return canonical
    platform = media_platform(canonical)
    with httpx.Client(
        transport=PublicOnlyTransport(), trust_env=False, timeout=10, follow_redirects=False,
    ) as client:
        for _ in range(5):
            response = client.get(canonical)
            if not response.is_redirect or "location" not in response.headers:
                response.raise_for_status()
                raise UnsafeURL("短链接未指向单个视频，请粘贴作品的完整官方链接。")
            target = str(response.url.join(response.headers["location"]))
            next_url, is_short = _canonical_shape(target)
            if media_platform(next_url) != platform:
                raise UnsafeURL("短链接跳转到了其他平台，已停止访问。")
            validate_public_url(target, allow_query=True)
            canonical = next_url
            if not is_short:
                return canonical
    raise UnsafeURL("短链接跳转次数过多，请粘贴作品的完整官方链接。")
