# VideoInsight Agent

视频链接转写与结构化分析工具：提交链接或上传文件，自动完成下载音频 -> ASR 转写 -> LLM 总结/面经提取。

已支持平台：**抖音、B站、小红书、快手**（真视频链接均无需 cookies 即可匿名下载）。

```text
提交链接 -> 创建任务 -> 下载/提取音频 -> ASR 转写 -> LLM 总结/面经提取 -> 查询/历史记录
```

默认启用 `VIDEOINSIGHT_MOCK_MODE=true`，因此即使还没有安装 `ffmpeg`、没有配置 ASR/LLM API Key，也能先验证接口和工作流。

## 快速启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

打开：

- API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health

## 上传文件分析

上传音频或视频文件是当前更稳定的主链路，不依赖短视频平台 cookies。

在 API 文档里使用：

- `POST /analyze-file`
- `file`：选择本地音频/视频文件
- `mode`：`transcript`、`summary` 或 `interview`
- `use_mock`：真实分析填 `false`

命令行示例：

```powershell
curl -X POST http://127.0.0.1:8000/analyze-file `
  -F "file=@C:\path\to\your-video.mp4" `
  -F "mode=summary" `
  -F "use_mock=false"
```

上传文件会暂存到：

```text
media/<task_id>/
```

## 历史记录与删除

任务与结果持久化在 SQLite（默认 `data/videoinsight.db`，可用 `VIDEOINSIGHT_DB_PATH` 修改），服务重启不丢失。

```powershell
# 历史任务列表（可按状态过滤）
curl "http://127.0.0.1:8000/tasks?limit=50"
curl "http://127.0.0.1:8000/tasks?status=succeeded"

# 删除任务（同时清理结果 JSON 与临时文件）
curl -X DELETE http://127.0.0.1:8000/tasks/<task_id>
```

## 快手支持说明

- 快手在 yt-dlp 中**没有 extractor**，本项目内置 `KuaishouDownloader`（`app/adapters/downloader.py`）：
  - 解析分享链接得到 `photoId`；
  - 抓取移动端分享页 `v.m.chenzhongtech.com/fw/photo/<photoId>` 中的 `mainMvUrls` 字段获取 mp4 直链；
  - 下载后走统一 ASR/LLM 流程，**无需 cookies**。
- 极少数纯图文帖（无 `mainMvUrls`）会返回友好提示，请改用上传文件分析。

## 测试接口

```powershell
curl -X POST http://127.0.0.1:8000/analyze `
  -H "Content-Type: application/json" `
  -d "{\"url\":\"https://example.com/video\",\"mode\":\"summary\",\"use_mock\":true}"
```

返回中会有 `task_id`，再查询：

```powershell
curl http://127.0.0.1:8000/tasks/<task_id>
```

## 命令行运行

```powershell
python cli.py "https://example.com/video" --mode summary
python cli.py "https://example.com/video" --mode interview
```

## 切换真实链路

1. 推荐安装系统 `ffmpeg` 并加入 PATH；如果没有，项目会尝试使用 `imageio-ffmpeg` 内置二进制。
2. 安装 Python 依赖：

```powershell
pip install -r requirements-asr.txt
```

3. 修改 `.env`：

```env
VIDEOINSIGHT_MOCK_MODE=false
DASHSCOPE_API_KEY=你的 DashScope Key
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek Key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

真实链路当前包括：

- `yt-dlp` 下载音频；
- `ffmpeg` 转 16kHz 单声道 WAV；
- DashScope Paraformer 非流式 ASR；
- OpenAI-compatible Chat Completions 格式的 LLM 调用。

### 抖音等平台需要 cookies 时

部分平台会拒绝匿名下载，`yt-dlp` 会提示 `Fresh cookies are needed`。

本地个人使用时，优先配置浏览器 cookies：

```env
YTDLP_COOKIES_FROM_BROWSER=chrome
```

可选值示例：

```env
YTDLP_COOKIES_FROM_BROWSER=edge
YTDLP_COOKIES_FROM_BROWSER=chrome:Default
YTDLP_COOKIES_FROM_BROWSER=edge:Default
```

如果浏览器读取失败，再使用 cookies 文件：

1. 在浏览器中登录对应平台。
2. 使用 cookies 导出工具导出 Netscape 格式的 `cookies.txt`。
3. 在 `.env` 中配置：

```env
YTDLP_COOKIES_FILE=C:\path\to\cookies.txt
```

然后重启服务再试。

也可以直接在网页左侧的“平台 Cookies”区域上传 `cookies.txt`，项目会保存到：

```text
media/_cookies/cookies.txt
```

这种方式不需要重启服务。

网页还提供“打开登录窗口”和“抓取登录态”：

1. 点击“打开登录窗口”。
2. 在项目专用的浏览器窗口中登录抖音。
3. 回到网页点击“检查窗口”，确认 DevTools 已连接且找到抖音页面。
4. 点击“抓取登录态”。

这只读取项目专用浏览器窗口里的 cookies，不读取你日常 Chrome 配置。

## 项目结构

```text
app/
  main.py                  FastAPI 入口（含任务列表/删除接口）
  workflow.py              固定工作流编排
  task_store.py            SQLite 持久化任务存储
  schemas.py               请求、任务和结果模型
  config.py                环境配置
  prompts.py               LLM Prompt 模板
  errors.py                统一错误类型
  cookies.py               平台 cookies 归一化与摘要
  browser_cookies.py       专用浏览器抓取登录态（DevTools 协议）
  qr_login.py              扫码登录抓取 cookies
  adapters/
    downloader.py          Mock / yt-dlp / Kuaishou 下载器（select_downloader 路由）
    audio.py               ffmpeg 音频预处理
    asr.py                 Mock / Paraformer 转写
    llm.py                 Mock / OpenAI-compatible LLM
web/
  index.html / app.js / styles.css   前端页面（上传/链接分析/历史记录/cookies）
tests/
  test_api.py / test_workflow.py / test_downloader.py / ...
verify_real_chain.py       真实链路验证脚本（可复跑）
verify_kuaishou_video.py   单条快手全流程调试脚本
```

## 第一阶段边界

已完成：

- API 提交/查询/列表/删除任务；
- 本地文件上传分析；
- mock 模式完整闭环；
- 可替换的下载、ASR、LLM 模块；
- 抖音/B站/小红书（yt-dlp）+ 快手（内置下载器）真实链接分析；
- 结果落盘到 `data/results`，任务持久化到 SQLite；
- Web 前端：上传/链接分析、结果三视图、历史任务、平台 cookies；
- 基础测试。

暂不包含：

- Celery + Redis 异步队列；
- 飞书机器人；
- PostgreSQL；
- 内容安全和事实核查；
- 多模态画面分析。

这些适合在第二阶段接入。
