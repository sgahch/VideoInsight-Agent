"""Collect Bilibili comments in a logged-in local Chrome; no cookie export."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import urlopen

if __package__:
    from .browser_capture import audit, export_capture, ingest_main, integer, load_capture, new_capture, now, put, video_url, write_json
else:
    from browser_capture import audit, export_capture, ingest_main, integer, load_capture, new_capture, now, put, video_url, write_json


ROOT = "document.querySelector('bili-comments')?.shadowRoot"
SORT = ROOT + "?.querySelector('bili-comments-header-renderer')?.shadowRoot"


class PlatformBlocked(RuntimeError):
    pass


def cdp_endpoint(url: str) -> str:
    """Chrome's remote-debugging UI may expose WS without /json/version."""
    try:
        with urlopen(url.rstrip('/') + '/json/version', timeout=5) as response:
            response.read()
        return url
    except HTTPError as exc:
        if exc.code != 404:
            raise
        parsed = urlsplit(url)
        profiles = [Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Google/Chrome/User Data',
                    Path.home()/'Library/Application Support/Google/Chrome', Path.home()/'.config/google-chrome']
        for profile in profiles:
            try:
                lines = (profile/'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
                if int(lines[0]) == (parsed.port or 80) and lines[1].startswith('/devtools/browser/'):
                    return 'ws://' + parsed.netloc + lines[1]
            except (OSError, ValueError, IndexError):
                continue
        return 'ws://' + urlsplit(url).netloc + '/devtools/browser'


class BrowserCollector:
    def __init__(self, state: dict, directory: Path, interval: float = 2):
        self.state, self.directory, self.interval = state, Path(directory), interval
        self.pending = set()
        self.blocked = False
        self.oid = None

    def checkpoint(self):
        write_json(self.directory / "snapshot.json", self.state)

    async def on_response(self, response):
        url = urlsplit(response.url)
        if url.hostname != "api.bilibili.com" or url.path != "/x/v2/reply/wbi/main":
            return
        try:
            if response.status in {412, 429}:
                raise PlatformBlocked("平台限制访问，请在浏览器处理后再续抓。")
            payload = await response.json()
            if payload.get("code") != 0:
                raise PlatformBlocked("评论接口未成功，请在浏览器确认登录及验证状态。")
            from urllib.parse import parse_qs
            self.oid = parse_qs(url.query).get("oid", [self.oid])[0]
            ingest_main(self.state, payload["data"])
            self.checkpoint()
        except Exception as exc:
            self.state["errors"].append({"stage": "main-response", "message": str(exc)[:300]})
            self.blocked = True

    def listen(self, response):
        task = asyncio.create_task(self.on_response(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def until(self, predicate, timeout=35):
        end = time.monotonic()+timeout
        while not predicate():
            if self.blocked:
                raise PlatformBlocked("评论接口需要处理，已保存进度。")
            if time.monotonic() >= end:
                raise TimeoutError("等待评论分页超时，已保存进度。")
            await asyncio.sleep(.2)

    async def main_pages(self, page, mode: int, max_pages: int | None):
        await page.evaluate("""mode => {
          const h = document.querySelector('bili-comments')?.shadowRoot?.querySelector('bili-comments-header-renderer')?.shadowRoot;
          const b = [...(h?.querySelectorAll('#sort-actions bili-text-button') || [])];
          const target = b.find(x=>x.textContent.includes(mode===2?'最新':'最热'));
          if(!target) throw Error('未找到评论排序按钮'); target.click();
        }""", mode)
        await self.until(lambda: any(r.get("mode") == mode for r in self.state["main_runs"]))
        while True:
            runs = [r for r in self.state["main_runs"] if r.get("mode") == mode]
            if runs[-1]["is_end"]:
                return
            if max_pages and len(runs) >= max_pages:
                raise RuntimeError("已达到测试页数上限；本次为部分数据。")
            before = len(runs)
            await page.evaluate("""() => {
              const t=document.querySelector('bili-comments')?.shadowRoot?.querySelector('#continuations .trigger');
              if(!t) throw Error('未找到下一页触发器');
              window.scrollBy(0,-1000); t.scrollIntoView({block:'center'});
            }""")
            try:
                await self.until(lambda: len([r for r in self.state["main_runs"] if r.get("mode") == mode]) > before, timeout=12)
            except TimeoutError:
                # One re-entry handles a trigger that remained in the viewport.
                await page.evaluate("window.scrollTo(0,0)")
                await asyncio.sleep(self.interval)
                await page.evaluate("document.querySelector('bili-comments').shadowRoot.querySelector('#continuations .trigger').scrollIntoView()")
                await self.until(lambda: len([r for r in self.state["main_runs"] if r.get("mode") == mode]) > before)
            await asyncio.sleep(self.interval)
            print(json.dumps({"stage": "main", "mode": mode, "roots": len(self.state["threads"]), "comments": len(self.state["comments"])}, ensure_ascii=False), flush=True)

    async def reply_page(self, page, root: str, pn: int):
        for attempt in range(3):
            await asyncio.sleep(self.interval if not attempt else 2**(attempt+1))
            result = await page.evaluate("""async ({oid,root,pn}) => {
              const u='https://api.bilibili.com/x/v2/reply/reply?'+new URLSearchParams({oid,type:'1',root,ps:'10',pn:String(pn),web_location:'333.788'});
              const r=await fetch(u,{credentials:'include'});
              if(!r.ok)return {http:r.status};
              return {http:r.status,body:await r.json()};
            }""", {"oid": self.oid, "root": root, "pn": pn})
            if result["http"] in {412, 429} or (result.get("body") or {}).get("code") in {-352, -412, -509}:
                raise PlatformBlocked("平台限制访问；已保存数据，请处理后续抓。")
            if result["http"] == 200 and result.get("body", {}).get("code") == 0:
                return result["body"]["data"]
        raise RuntimeError("回复接口请求未成功；已保存进度。")

    async def replies(self, page):
        if not self.oid:
            raise RuntimeError("未观察到视频评论 oid。")
        for root, item in list(self.state["threads"].items()):
            expected = integer(item.get("rcount"))
            actual = sum(c["root"] == root for c in self.state["comments"].values())
            previous = self.state["reply_runs"].get(root) or {}
            if previous.get("complete") and actual == expected == previous.get("total"):
                continue
            run = {"expected": expected, "total": expected, "count": 0, "pages": [], "complete": False}
            self.state["reply_runs"][root] = run
            ids, pn, page_count = set(), 1, 1
            if not expected:
                run["complete"] = True
                continue
            while pn <= page_count:
                d = await self.reply_page(page, root, pn)
                info = d.get("page") or {}
                total, size = integer(info.get("count")), integer(info.get("size"))
                if size <= 0 or integer(info.get("num")) != pn:
                    raise ValueError("回复接口返回的页码或页大小无效。")
                rows = d.get("replies") or []
                if not isinstance(rows, list):
                    raise ValueError("回复列表格式无效。")
                before = len(ids)
                for raw in rows:
                    put(self.state, raw)
                    ids.add(str(raw.get("rpid_str") or raw.get("rpid")))
                if d.get("root"):
                    put(self.state, d["root"], main=True)
                run.update(total=total, count=len(ids))
                run["pages"].append({"pn": pn, "size": size, "count": len(rows), "declared_count": total, "at": now()})
                page_count = (total+size-1)//size
                self.checkpoint()
                if pn < page_count and len(ids) == before:
                    raise ValueError("回复接口重复页面或提前返回空页。")
                pn += 1
            run["complete"] = len(ids) == run["total"]
            if not run["complete"]:
                raise ValueError("回复唯一 ID 数量与接口总数不一致。")
            self.checkpoint()
            print(json.dumps({"stage": "replies", "verified_roots": sum(r.get("complete", False) for r in self.state["reply_runs"].values()), "roots": len(self.state["threads"]), "comments": len(self.state["comments"])}, ensure_ascii=False), flush=True)

    async def collect(self, page, *, cross_check=False, max_main_pages=None, skip_replies=False):
        page.on("response", self.listen)
        try:
            await page.goto(self.state["video_url"], wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector("bili-comments", state="attached", timeout=30000)
            await page.locator("bili-comments").scroll_into_view_if_needed()
            await page.wait_for_function(SORT+"?.querySelector('#sort-actions')", timeout=30000)
            self.state["title"] = await page.title()
            await self.main_pages(page, 2, max_main_pages)
            if cross_check:
                # Discard the initial hot page evidence before traversing that mode.
                self.state["main_runs"] = [r for r in self.state["main_runs"] if r.get("mode") != 3]
                await self.main_pages(page, 3, max_main_pages)
            if skip_replies:
                raise RuntimeError("已跳过楼中楼，本次仅为部分采集。")
            await self.replies(page)
        except Exception as exc:
            self.state["errors"].append({"stage": "collector", "message": str(exc)[:300]})
        finally:
            page.remove_listener("response", self.listen)
            if self.pending:
                await asyncio.gather(*list(self.pending), return_exceptions=True)
            self.state["finished_at"] = now()
            self.state["status"] = "complete" if audit(self.state)["accessible_scope_verified"] else "partial"
            self.checkpoint()


async def run(args):
    if args.resume:
        state = load_capture(json.loads(Path(args.resume).read_text(encoding="utf-8")))
        if state["video_url"] != video_url(args.url):
            raise ValueError("续抓文件与本次视频不一致。")
        # Re-observe fresh signed main pagination; keep verified reply streams.
        state.update(main_runs=[], errors=[], status="running", finished_at=None)
    else:
        state = new_capture(args.url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("本机采集需要 Playwright：python -m pip install playwright；连接现有 Chrome 不需要下载浏览器。") from exc
    collector = BrowserCollector(state, Path(args.out_dir), args.interval)
    async with async_playwright() as pw:
        endpoint = await asyncio.to_thread(cdp_endpoint, args.cdp_url)
        browser = await pw.chromium.connect_over_cdp(endpoint, timeout=20000)
        if not browser.contexts:
            raise RuntimeError("浏览器没有可用上下文。")
        page = await browser.contexts[0].new_page()
        try:
            await collector.collect(page, cross_check=args.cross_check, max_main_pages=args.max_main_pages, skip_replies=args.skip_replies)
        finally:
            await page.close()  # Only the tab created by this command.
        # Do not close the connected user's browser.
    report = export_capture(state, Path(args.out_dir))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["accessible_scope_verified"] else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("url", nargs="?", help="B站 /video/BV… 链接")
    p.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--resume", help="snapshot.json；重新遍历主评论并复用已核对的回复")
    p.add_argument("--export-only", metavar="SNAPSHOT", help="离线校验、导出已有快照，不联网")
    p.add_argument("--cross-check", action="store_true", help="额外遍历最热排序并合并")
    p.add_argument("--interval", type=float, default=2)
    p.add_argument("--max-main-pages", type=int, help="仅用于限页测试，会保留部分完成状态")
    p.add_argument("--skip-replies", action="store_true", help="仅用于主评论试采")
    a = p.parse_args()
    if a.interval < .5 or (a.max_main_pages is not None and a.max_main_pages < 1):
        p.error("请求间隔至少 0.5 秒，测试页数至少 1；默认间隔 2 秒。")
    if a.export_only:
        report = export_capture(load_capture(json.loads(Path(a.export_only).read_text(encoding="utf-8"))), Path(a.out_dir))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["accessible_scope_verified"] else 2
    if not a.url and a.resume:
        try:
            a.url = load_capture(json.loads(Path(a.resume).read_text(encoding="utf-8")))["video_url"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            p.error(f"无法读取续抓文件：{exc}")
    if not a.url:
        p.error("请提供视频链接、--resume 或 --export-only。")
    parsed = urlsplit(a.cdp_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        p.error("CDP 仅连接本机 http://localhost:端口，不接受远程地址。")
    try:
        return asyncio.run(run(a))
    except KeyboardInterrupt:
        print("已中断；使用已保存的 snapshot.json 续抓。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"采集未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
