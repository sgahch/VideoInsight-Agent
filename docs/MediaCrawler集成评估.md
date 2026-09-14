# MediaCrawler 评论爬虫 —— 集成可行性评估

> 评估对象：https://github.com/NanmiCoder/MediaCrawler （main 分支，2026-09 状态）
> 目标项目：VideoInsight Agent（链接 → 下载 → ASR → LLM 视频洞察）
> 评估结论：**可以加，但建议以「独立服务 + 文件/接口对接」方式接入，不要直接把源码塞进本项目；且必须遵守其非商业许可与平台 ToS。**

---

## 1. 它到底能干什么

多平台自媒体**公开数据**采集器，重点就是「评论」：

| 平台 | 关键词搜索 | 指定 ID 爬取 | 二级评论(楼中楼) | 登录态缓存 | 评论词云 |
|---|---|---|---|---|---|
| 小红书 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 抖音 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 快手 | ✅ | ✅ | ✅ | ✅ | ✅ |
| B 站 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 微博 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 百度贴吧 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 知乎 | ✅ | ✅ | ✅ | ✅ | ✅ |

覆盖的平台**正好和 VideoInsight 已支持的下载源（抖音/快手/B站/小红书）高度重合**——这是集成的天然契合点。

---

## 2. 技术栈与登录机制（关键约束）

- **核心**：Playwright 浏览器自动化，靠「保留登录态」拿签名参数，**不做 JS 逆向**。
- **登录方式**：默认 CDP 模式（连接你本机已登录的 Chrome，`127.0.0.1:9222`），扫码/点确认即可；也可切标准 Playwright 模式。
- **硬依赖**：
  - Python（官方建议 3.11，`requirements.txt` 按 3.11 锁的）
  - **Node.js ≥ 16**（爬抖音、知乎时必需，用于生成签名）
  - Playwright 浏览器驱动（标准模式需要）
  - 可选 Redis（IP 代理池）
- **评论默认关闭**：`config/base_config.py` 里 `ENABLE_GET_COMMENTS` 需手动开；爬取数量/翻页也有上限配置。

> 对比：VideoInsight 现在用 yt-dlp + cookie 文件下视频/音频，基本无需登录态。MediaCrawler **每个平台都要先登录**，这是它最大的接入成本（每次换账号/掉登录态要重新扫码）。

---

## 3. 许可与合规（务必先看）

**许可证：`NON-COMMERCIAL LEARNING LICENSE 1.1`（非商业学习许可证 1.1）**

关键条款（已核实 LICENSE 原文）：
- ✅ 允许**个人**出于**学习/研究**目的免费使用、复制、修改、合并。
- ❌ **不得用于任何商业用途**，也不得对第三方造成不当影响。
- ❌ 不得用于**大规模爬取**或对平台运营造成干扰。
- 必须保留版权声明与许可证声明。
- 遵守目标平台 ToS 与 robots.txt，合理控制请求频率。

**对你意味着什么：**
- 你这是「高校 AI/大数据方向学习项目」，定位契合 ✅。
- 一旦将来要**商业化/接毕设外的产品化/对外服务**，就不能直接用它——需要自研或换合规数据源（如各平台开放 API / TikHub 之类付费接口，MediaCrawler 的赞助商里就有）。
- 当前 main 分支是「教学版」：**已对用户 ID 做匿名哈希、昵称脱敏，且不再采集创作者粉丝/关注等个人信息**（我在 `store/bilibili/__init__.py` 里实看到 `anonymize_user_id` / `mask_nickname` 与多处 `return` 掉的个人信息采集）。这点对合规是加分的。

---

## 4. 评论数据长什么样（已核实字段）

以 B 站为例，`update_bilibili_video_comment` 落库的评论字段：

| 字段 | 含义 |
|---|---|
| `comment_id` | 评论 ID |
| `parent_comment_id` | 父评论 ID（0 = 一级评论） |
| `create_time` | 评论时间 |
| `video_id` | 视频 ID（与 VideoInsight 的下载记录可关联） |
| `content` | 评论正文 |
| `creator_hash` | 评论者匿名哈希（**非原始 ID**） |
| `nickname` | 脱敏昵称 |
| `sub_comment_count` | 二级评论数 |
| `like_count` | 点赞数 |
| `last_modify_ts` | 落库时间戳 |

- 二级评论（楼中楼）由 `get_video_all_comments` 递归抓取，结构同上、靠 `parent_comment_id` 串成树。
- **其他平台字段形状类似**（都有 comment_id/parent/content/like_count 等），但字段名/层级略有差异——**接入前需逐平台核对**（标记为待核实）。

---

## 5. 与你现有架构的契合点

VideoInsight 当前链路：
```
分享链接 → link_parser 标准化 → downloader(yt-dlp) → ASR(Paraformer) → LLM(DeepSeek) → 洞察结果
```
加上评论后：
```
分享链接 → [VideoInsight 下载+ASR] + [MediaCrawler 抓评论] → 合并喂给 LLM → 「字幕+观众评论」综合洞察
```
- 你的 `link_parser` 已经能识别平台、抽出 `video_id`/`aweme_id`/`BV号`，正好能当 MediaCrawler 的「指定 ID 爬取」输入。
- LLM 提示词加一段：「以下是该视频的观众评论，请结合字幕与评论给出洞察（情绪、争议点、高频问题）」。改动很小，价值明显。

---

## 6. 三种集成方案（推荐方案 B）

### 方案 A：子进程调用（最快验证）
- VideoInsight 分析任务里 `subprocess` 调 `python main.py --platform bili --type detail --id <BV号>`，等它把评论写成 JSON/SQLite，再读回。
- 优点：零耦合，MediaCrawler 独立升级不影响本项目。
- 缺点：进程管理重、要等登录态、难以实时；每次都要起 Playwright。

### 方案 B：独立 FastAPI 服务（★ 推荐）
- 把 MediaCrawler 当作**常驻服务**跑（它自带 `api/`，端口 8080，还有 WebUI）。
- VideoInsight 的分析任务里用 HTTP 调它的接口拿到评论 JSON，或读它落库的 SQLite。
- 优点：解耦干净、可单独扩缩容、登录态常驻复用；两边各管各的依赖（本项目不背 Playwright/Node 的包袱）。
- 缺点：多一个服务要运维。

### 方案 C：抽模块当库 import（不推荐）
- 直接 `from media_platform.bilibili import ...`。
- 缺点：MediaCrawler 是「脚本/服务」形态，依赖 `config/var/tools` 全局状态，强耦合、升级即碎；依赖体积直接污染本项目。

**结论：选 B（服务化对接）。** 与你现有 FastAPI 风格一致，也最省心。

---

## 7. 落地步骤（若你拍板做）

1. **单独建目录** `comment_crawler/`（git submodule 或 vendored 副本），不动 `app/`。
2. 配 `config/base_config.py`：`ENABLE_GET_COMMENTS=True`、选平台、`SAVE_DATA_OPTION=sqlite`（或 json）。
3. 启动它的 `api` 服务（端口 8080），复用本机 Chrome 登录态（CDP 模式）。
4. VideoInsight 侧新增 `app/adapters/comment_fetcher.py`：
   - 输入 `platform + 平台 video_id`；
   - 调 MediaCrawler 接口 / 读其 SQLite，拉回评论；
   - 归一化成统一 schema（`comment_id, parent_id, content, like_count, create_time, sub_comments`）。
5. `workflow.py` 的分析流程里把「评论文本」拼进 LLM 提示词，产出「字幕+评论」综合洞察。
6. 前端 `web/` 在结果页加一个「观众评论」折叠区 + 词云（MediaCrawler 已能生成词云图）。

---

## 8. 风险与注意

- **ToS / 风控**：各平台明令限制自动化抓取；评论默认关闭、需手动开；务必控制频率，避免封号。
- **登录态是脆弱点**：掉登录就要重新扫码，自动化链路会断——建议做成「可选增强」而非核心路径。
- **依赖体积**：Playwright + Node + 浏览器驱动，会显著加重本项目环境；服务化隔离能挡住这点。
- **许可红线**：非商业学习用途 OK；任何商业化/对外服务前必须替换数据源。
- **字段差异**：各平台评论 schema 不一致，归一化层要逐个平台验证（待核实项）。

---

## 9. 一句话总结

能加，且和你的链路天然互补（同样覆盖抖音/快手/B站/小红书）。**最稳的做法是把它当独立评论服务跑，VideoInsight 通过接口/SQLite 拿评论再喂给同一个 LLM**，而不是把它的源码揉进 `app/`。代价是要接受「每平台先登录 + 非商业许可」两条约束。

> 待核实项：抖音/小红书/快手各自评论字段的精确名称与层级（仅 B 站已逐字段核实）；MediaCrawler `api/` 暴露的具体端点路径（需拉源码后确认）。
