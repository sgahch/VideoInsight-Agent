# 本机浏览器采集 B站评论

项目新增一个本机采集入口，复用已登录 Chrome，通过网页实际发出的主评论响应和同会话楼中楼请求采集数据。服务器采集任务仍使用原有入口。本机结果可以导入现有评论中心。

## 使用

需要 Python 3.10+、`python -m pip install playwright` 和本机 Chrome CDP。连接已有浏览器不需要下载 Playwright 浏览器。可在支持的 Chrome 中检查 `chrome://inspect/#remote-debugging` 及远程调试设置。默认端口 9222，可通过 --cdp-url 指定本机 HTTP 地址。登录和验证码在浏览器中手动完成。

```powershell
python -m crawler_service.bilibili_browser 'https://www.bilibili.com/video/BV12gJz6qEKo/' --out-dir data/browser-captures/my-video --cross-check
python -m crawler_service.bilibili_browser --resume data/browser-captures/my-video/snapshot.json --out-dir data/browser-captures/my-video --cross-check
python -m crawler_service.bilibili_browser --export-only data/browser-captures/my-video/snapshot.json --out-dir data/browser-captures/export
```

仅支持完整 BV 视频链接。脚本新建标签页，结束时关闭自己的标签页，不关闭用户浏览器。默认请求间隔 2 秒，遇到风控停止并保存部分结果。`--max-main-pages 1 --skip-replies` 可做有限试采。

输出 snapshot.json（逐页保存）、report.json、comments.jsonl 和 comments.csv。保留字符串 ID、父子关系、时间、点赞及图片链接。续抓重新扫描主评论，复用已经核对的回复进度；结果覆盖采集期间，不能视为同一瞬间的快照。

## 导入评论中心

打开 `/comments`，在“导入本机采集结果”中上传 snapshot.json（页面限制 20 MB）。后端限制 100000 条记录，重新核对快照结构和采集证据；每次导入新建任务，避免覆盖当前任务。原有数据库会自动增加图片字段和采集报告表。

代理接口 `POST /comment-api/import-browser` 接收 `{ "snapshot": {...} }`，用户归属由当前请求确定。内部接口 `POST /jobs/import-browser` 接收 `{ "owner": "...", "snapshot": {...} }`，需要采集服务令牌。导入任务不会调度服务器爬虫。本机任务的续抓通过本机命令完成，随后重新导入。

核对项包括最新排序起止、楼中楼声明数与实取数、连续分页、孤立回复及错误。通过后状态为 completed，否则为 partial。报告保留页面总数与实取数差额；通过只表示当前账号可访问范围核对通过，不保证获取隐藏、删除、审核中的评论。文件证据校验不构成来源真实性证明。

退出码：0 核对通过，2 部分结果，1 运行失败，130 用户中断。已有数据可先离线导出，不需要浏览器。

## Skill

已安装于 `C:/Users/Ynchen/.codex/skills/bilibili-comments`，可在识别该 Skill 的聊天中使用 `$bilibili-comments`。Skill 内 scripts/bilibili_browser.py 与 scripts/browser_capture.py 是项目同名模块的便携副本，修改采集实现时需同步。技能包含 CLI 使用说明、MCP 备用流程和项目导入说明。

本次修改是本地代码接入，不包含线上服务部署；已有运行进程需更新重启。

## 本次验证

272 项项目测试通过。历史真实快照经独立 Skill 离线校验、重新导出及临时数据库导入验证：1758 条，主评论 382 条、回复 1376 条、带图片评论 44 条；页面总数 1776 条，差额 18 条保留为原因未确认。

2026-10-27 的本机实时试采未能完成：当前 Chrome 调试连接初始化超时。因此尚未验证新 CLI 在当前浏览器会话下的实时翻页；不能将离线验证当作实时采集成功。已支持标准 HTTP CDP 发现和本机 Chrome DevToolsActivePort WebSocket 发现，连接恢复后仍需重跑限页试采。MCP 备用流程保存在 Skill references/mcp-workflow.md。
