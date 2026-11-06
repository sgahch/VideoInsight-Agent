from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
from crawler_service.pagination import Pager
from crawler_service.runtime import browser_options, runtime_status, state_dir as runtime_state_dir, upstream_dir
from crawler_service.store import Store


async def run(job_id: str):
    state_dir = runtime_state_dir()
    store = Store(state_dir / "comments.db")
    job = store.job(job_id)
    if not job:
        return
    platform = job["platform"]
    cookie_path = state_dir / "cookies" / f"{platform}.txt"
    if not cookie_path.exists() or not cookie_path.read_text(encoding="utf-8").strip():
        store.status(job_id, "needs_login", "请先在评论中心导入该平台的登录 Cookies，再点击继续。")
        return
    cookies = cookie_path.read_text(encoding="utf-8").strip()
    pager = Pager(store, job_id, platform)
    crawler = None
    original_dir = os.getcwd()
    try:
        status = runtime_status()
        if not all(status[key] for key in ("source_installed", "playwright_installed", "node_installed")):
            store.status(job_id, "blocked", "MediaCrawler 运行环境尚未安装完整，请运行 python -m crawler_service.manage install。")
            return
        os.chdir(upstream_dir())
        sys.path.insert(0, os.getcwd())
        import config
        config.PLATFORM = platform
        config.HEADLESS = True
        config.ENABLE_CDP_MODE = False
        config.SAVE_LOGIN_STATE = False
        config.LOGIN_TYPE = "cookie"
        config.COOKIES = cookies
        config.CRAWLER_TYPE = "detail"
        config.SAVE_DATA_OPTION = "jsonl"
        config.SAVE_DATA_PATH = str(state_dir / "upstream")
        config.ENABLE_GET_COMMENTS = True
        config.ENABLE_GET_SUB_COMMENTS = True
        config.ENABLE_GET_MEDIA = False
        config.ENABLE_GET_WORDCLOUD = False
        config.MAX_CONCURRENCY_NUM = 1
        config.CRAWLER_MAX_SLEEP_SEC = 2
        config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = sys.maxsize
        config.CRAWLER_MAX_NOTES_COUNT = 1
        config.USER_DATA_DIR = str(state_dir / "profiles" / "%s")
        setattr(config, {"dy": "DY_SPECIFIED_ID_LIST", "ks": "KS_SPECIFIED_ID_LIST", "bili": "BILI_SPECIFIED_ID_LIST", "xhs": "XHS_SPECIFIED_NOTE_URL_LIST"}[platform], [job["url"]])
        # No full comment text or credentials in container logs.
        logging.getLogger("MediaCrawler").setLevel(logging.ERROR)
        module_name, class_name, client_name, method_name = {
            "dy": ("douyin", "DouYinCrawler", "DouYinClient", "get_aweme_all_comments"),
            "ks": ("kuaishou", "KuaishouCrawler", "KuaiShouClient", "get_video_all_comments"),
            "bili": ("bilibili", "BilibiliCrawler", "BilibiliClient", "get_video_all_comments"),
            "xhs": ("xhs", "XiaoHongShuCrawler", "XiaoHongShuClient", "get_note_all_comments"),
        }[platform]
        core = importlib.import_module(f"media_platform.{module_name}.core")
        logging.getLogger("MediaCrawler").setLevel(logging.ERROR)
        client_module = importlib.import_module(f"media_platform.{module_name}.client")
        client_class = getattr(client_module, client_name)

        async def collect(client, *args, **kwargs):
            video_id = args[0] if args else kwargs.get("aweme_id") or kwargs.get("photo_id") or kwargs.get("video_id") or kwargs.get("note_id")
            token = (args[1] if len(args) > 1 else kwargs.get("xsec_token", "")) if platform == "xhs" else ""
            try:
                return await pager.run(client, str(video_id), token)
            except Exception as exc:
                # The upstream core may catch errors; retain the fact independently.
                pager.issues.append(f"评论分页未完成（{type(exc).__name__}），请检查平台登录或验证状态后继续。")
                return []

        setattr(client_class, method_name, collect)
        crawler = getattr(core, class_name)()
        original_launch = crawler.launch_browser

        class ConfiguredChromium:
            """Use bundled Chromium or an explicitly selected local browser."""
            def __init__(self, browser_type):
                self.browser_type = browser_type

            def __getattr__(self, name):
                return getattr(self.browser_type, name)

            async def launch(self, **options):
                options.pop("channel", None)
                options.update(browser_options())
                return await self.browser_type.launch(**options)

        async def launch(*args, **kwargs):
            if args:
                args = (ConfiguredChromium(args[0]), *args[1:])
            else:
                kwargs["chromium"] = ConfiguredChromium(kwargs["chromium"])
            context = await original_launch(*args, **kwargs)
            domain = {"dy": ".douyin.com", "ks": ".kuaishou.com", "bili": ".bilibili.com", "xhs": ".xiaohongshu.com"}[platform]
            entries = []
            for part in cookies.split(";"):
                name, sep, value = part.strip().partition("=")
                if name and sep:
                    entries.append({"name": name, "value": value, "domain": domain, "path": "/"})
            await context.add_cookies(entries)
            if platform == "bili":
                from urllib.parse import urlsplit
                if urlsplit(job["url"]).hostname == "b23.tv":
                    page = await context.new_page()
                    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
                    from crawler_service.store import platform_for_url
                    if platform_for_url(page.url) != "bili":
                        raise ValueError("B站短链接未跳转到有效的视频页面。")
                    config.BILI_SPECIFIED_ID_LIST = [page.url]
                    await page.close()
            return context

        crawler.launch_browser = launch
        await asyncio.wait_for(crawler.start(), timeout=24 * 60 * 60)
        if pager.exhausted and not pager.issues:
            store.status(job_id, "completed", "一级评论及子评论接口均已遍历到末页；范围为当前账号可见的评论。")
        else:
            current = store.job(job_id)
            store.status(job_id, "partial" if current["total"] else "blocked", pager.issues[0] if pager.issues else "平台未开始返回评论，请更新登录 Cookies、完成平台验证，并检查服务器 IP 是否被平台限制。")
    except BaseException as exc:
        current = store.job(job_id)
        store.status(job_id, "partial" if current["total"] else "blocked", f"抓取未完成（{type(exc).__name__}）。请检查登录状态、平台验证或服务器 IP 限制，已抓到的数据已保存。")
    finally:
        if crawler and getattr(crawler, "browser_context", None):
            try:
                await crawler.browser_context.close()
            except Exception:
                pass
        os.chdir(original_dir)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
