from __future__ import annotations

from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urlparse

import httpx

from app.errors import WorkflowError


_VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_SHORT_HOSTS = {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


class _RiskControlError(WorkflowError):
    pass


def is_bilibili_video_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in _SHORT_HOSTS or (
        parsed.hostname in _VIDEO_HOSTS and parsed.path.startswith("/video/")
    )


def _cookie_jar(cookie_args: list[str]) -> CookieJar:
    if not cookie_args:
        return CookieJar()
    # 与下载命令共用 yt-dlp 的 cookies 参数解析，兼容 Netscape 文件和浏览器配置。
    # 不进入 YoutubeDL 上下文，不回写共享 cookies；传入的文件也是任务临时副本。
    from yt_dlp import YoutubeDL, parse_options

    try:
        options = parse_options(["--ignore-config", *cookie_args]).ydl_opts
        return YoutubeDL({**options, "quiet": True}).cookiejar
    except Exception as exc:
        raise WorkflowError(
            "读取 B 站 Cookies 失败，请在「平台 Cookies」上传有效的 cookies.txt 后重试。"
        ) from exc


def _api_get(client: httpx.Client, path: str, params: dict) -> dict | list:
    response = client.get(f"https://api.bilibili.com{path}", params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise WorkflowError("B 站接口返回了无效数据，请稍后重试。")
    code = payload.get("code")
    if code in {-352, -412, -401}:
        raise _RiskControlError(
            "B 站触发了访问风控，请稍后重试，或在「平台 Cookies」上传 B 站登录态；"
            "也可改用「上传文件分析」。"
        )
    if code in {-101, -403, -10403}:
        raise WorkflowError("该 B 站视频需要登录或访问权限，请上传有权限的 B 站 Cookies 后重试。")
    if code in {-404, 62002, 62012}:
        raise WorkflowError("该 B 站视频不存在、已下架或不可见，请确认链接。")
    if code != 0 or not isinstance(payload.get("data"), (dict, list)):
        raise WorkflowError(f"B 站解析失败：{payload.get('message') or '接口未返回视频数据'}（{code}）")
    return payload["data"]


def _video_detail(client: httpx.Client, query: dict) -> dict:
    try:
        detail = _api_get(client, "/x/web-interface/view", query)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 412:
            raise
    except _RiskControlError:
        pass
    else:
        if not isinstance(detail, dict):
            raise WorkflowError("B 站详情接口返回了无效数据，请稍后重试。")
        return detail
    # 部分服务器仅详情接口被风控；分 P 接口仍提供合法的 cid、标题和时长。
    # 不重复请求已被拒绝的接口，也不臆造无法取得的 UP 主信息。
    pages = _api_get(client, "/x/player/pagelist", query)
    if not isinstance(pages, list) or not pages:
        raise WorkflowError("B 站未返回视频分 P 信息，请确认链接或上传 B 站 Cookies 后重试。")
    return {"bvid": query.get("bvid"), "pages": pages}


def _media_urls(stream: dict, primary_keys: tuple[str, ...]) -> list[str]:
    primary = next((stream.get(key) for key in primary_keys if stream.get(key)), None)
    backups = stream.get("backupUrl") or stream.get("backup_url") or []
    if not isinstance(backups, list):
        backups = []
    return list(dict.fromkeys(
        url for url in [primary, *backups]
        if isinstance(url, str) and urlparse(url).scheme in {"http", "https"}
    ))


def _formats(play_info: dict) -> list[dict]:
    formats = []
    dash = play_info.get("dash") or {}
    for kind in ("audio", "video"):
        for stream in dash.get(kind) or []:
            for index, url in enumerate(_media_urls(stream, ("baseUrl", "base_url"))):
                formats.append({
                    "format_id": f"{kind}-{stream.get('id')}-{stream.get('codecid', 0)}-cdn{index}",
                    "source_preference": -index,
                    "url": url,
                    "ext": "m4a" if kind == "audio" else "mp4",
                    "acodec": stream.get("codecs") if kind == "audio" else "none",
                    "vcodec": "none" if kind == "audio" else stream.get("codecs"),
                    "tbr": (stream.get("bandwidth") or 0) / 1000,
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                })
    # 老视频可能仅返回分段的音视频合流，必须保留全部分段，避免静默截短。
    parts = [(part, _media_urls(part, ("url",))) for part in play_info.get("durl") or []]
    if parts and all(urls for _, urls in parts):
        for index in range(max(len(urls) for _, urls in parts)):
            fragments = [
                {"url": urls[index] if index < len(urls) else urls[0],
                 "duration": (part.get("length") or 0) / 1000}
                for part, urls in parts
            ]
            formats.append({
                "format_id": f"http-cdn{index}",
                "source_preference": -index,
                "url": fragments[0]["url"],
                "ext": "mp4",
                **({"fragments": fragments, "protocol": "http_dash_segments"} if len(fragments) > 1 else {}),
            })
    return formats


def resolve_bilibili_info(url: str, cookie_args: list[str]) -> dict:
    """通过详情和播放接口生成 yt-dlp 输入，避免请求易被 412 拦截的视频 HTML。"""
    try:
        with httpx.Client(
            headers=_HEADERS, cookies=_cookie_jar(cookie_args), timeout=20, trust_env=False,
        ) as client:
            # 短链只取 Location，不跟随到视频 HTML；限制重定向次数及目标平台。
            for _ in range(5):
                if urlparse(url).hostname not in _SHORT_HOSTS:
                    break
                response = client.get(url, follow_redirects=False)
                if not response.is_redirect or "location" not in response.headers:
                    response.raise_for_status()
                    raise WorkflowError("无法展开 B 站短链接，请粘贴包含 BV/av 号的视频链接。")
                url = str(response.url.join(response.headers["location"]))
            parsed = urlparse(url)
            if parsed.hostname not in _VIDEO_HOSTS or not parsed.path.startswith("/video/"):
                raise WorkflowError("该 B 站链接未指向单个视频，请粘贴包含 BV/av 号的视频链接。")
            video_id = parsed.path.split("/")[2]
            if video_id.startswith("BV") and video_id[2:].isalnum():
                query = {"bvid": video_id}
            elif video_id.startswith("av") and video_id[2:].isdigit():
                query = {"aid": video_id[2:]}
            else:
                raise WorkflowError("无法识别 B 站视频编号，请检查链接中的 BV/av 号。")
            try:
                page_number = int(parse_qs(parsed.query).get("p", ["1"])[0])
            except ValueError as exc:
                raise WorkflowError("B 站分 P 参数无效，请检查链接中的 p 参数。") from exc
            detail = _video_detail(client, query)
            pages = detail.get("pages") or [{"page": 1, "cid": detail.get("cid")}]
            page = next((p for p in pages if p.get("page") == page_number), None)
            if page is None or not page.get("cid"):
                raise WorkflowError("找不到链接指定的 B 站分 P，请检查链接中的 p 参数。")
            play_info = _api_get(client, "/x/player/playurl", {
                **query, "cid": page["cid"], "fnval": 16, "qn": 64, "fourk": 1,
            })
            if not isinstance(play_info, dict):
                raise WorkflowError("B 站播放接口返回了无效数据，请稍后重试。")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {403, 412, 429}:
            raise WorkflowError(
                "B 站拒绝了当前请求（访问风控），请稍后重试或上传 B 站 Cookies；"
                "也可改用「上传文件分析」。"
            ) from exc
        raise WorkflowError(f"B 站接口请求失败（HTTP {exc.response.status_code}），请稍后重试。") from exc
    except httpx.HTTPError as exc:
        raise WorkflowError("无法连接 B 站接口，请检查网络后重试。") from exc
    except ValueError as exc:
        raise WorkflowError("B 站接口未返回有效 JSON，请稍后重试。") from exc

    formats = _formats(play_info)
    if not formats:
        raise WorkflowError("B 站未返回可用音视频流，请确认视频可播放，或上传 B 站 Cookies 后重试。")
    bvid = detail.get("bvid") or video_id
    return {
        "id": f"{bvid}_{page['cid']}",
        "title": (page.get("part") if len(pages) > 1 else detail.get("title")) or page.get("part") or bvid,
        "uploader": (detail.get("owner") or {}).get("name"),
        "duration": page.get("duration") or play_info.get("timelength", 0) / 1000 or detail.get("duration"),
        "webpage_url": f"https://www.bilibili.com/video/{bvid}?p={page_number}",
        "extractor": "bilibili_api",
        "http_headers": _HEADERS.copy(),
        "formats": formats,
    }
