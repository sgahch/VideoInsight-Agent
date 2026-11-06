# 运行 MediaCrawler 产出评论数据（VideoInsight 对接手册）

> 日常使用请优先按 [评论服务部署与验证](../deploy/comments.md) 安装后台评论服务：
> `.\.venv\Scripts\python.exe -m crawler_service.manage install`，
> 再执行 `python -m crawler_service.manage start`。它支持持续分页、子评论、断点续抓和评论中心排序。
> 本文以下保留为“离线文件导入”的备选方案，不是实时抓取服务。

> 本文件是 `docs/MediaCrawler集成评估.md` 的**落地操作手册**。
> 目标：让 MediaCrawler 作为**独立评论服务**跑起来，把评论落盘，VideoInsight 自动按视频 ID 关联并喂给同一个 LLM。
> 评估里的「非商业学习许可」「ToS/风控」约束依然生效，先读评估文档。

---

## 0. 总览：两边怎么接

```
MediaCrawler（独立仓库，单独跑）
   └─ 爬评论 → 落盘到 MediaCrawler/data/<平台>/..._comments.<csv|json|jsonl|db>
                                              │
                  VideoInsight 读这个目录（MEDIA_CRAWLER_OUTPUT_DIR）
                                              │
                                              ▼
        analyze() 里 fetch_comments_for_url() 按 (平台, 视频ID) 读回 → 归一化 → 拼进 LLM 提示词
```

关键点：**两边不共享代码、不共享进程**。MediaCrawler 只负责「产数据」，VideoInsight 只负责「读数据 + 分析」。

---

## 1. 准备 MediaCrawler（独立目录，别放进本项目）

```bash
# 放在 VideoInsight 之外，避免污染本项目依赖
git clone https://github.com/NanmiCoder/MediaCrawler.git ../MediaCrawler
cd ../MediaCrawler
uv sync            # 或 python -m venv venv && pip install -r requirements.txt
playwright install # 仅标准 Playwright 模式需要；用 CDP 模式可跳过
```

- **Node.js ≥ 16** 必须装（爬抖音/知乎时生成签名用）。
- 默认用 **CDP 模式**（复用你本机已登录的 Chrome，风控最低）：Chrome 开 `chrome://inspect/#remote-debugging` → 勾选允许远程调试 → 出现 `Server running at: 127.0.0.1:9222` 即可。
- 如需标准 Playwright 模式：改 `config/base_config.py` 里 `ENABLE_CDP_MODE = False`。

---

## 2. 配置 MediaCrawler（开评论 + 选格式）

编辑 `MediaCrawler/config/base_config.py`：

```python
ENABLE_GET_COMMENTS = True
ENABLE_GET_SUB_COMMENTS = True    # 上游默认关闭楼中楼
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 10000  # 根据采集范围设置；上游默认仅 10 条
SAVE_DATA_OPTION = "csv"          # 推荐 csv 或 json；VideoInsight 都认
# 想爬的平台的开关按需开启（默认大多已开）
```

> 选 `csv` / `json` 都可以；VideoCrawler 适配层（`app/adapters/comment_fetcher.py`）支持
> `.csv / .json / .jsonl / .sqlite / .db`。**不要选需要联网数据库（MySQL/MongoDB）** 的格式，
> 否则 VideoInsight 读不到本地文件。

---

## 3. 爬一条视频的评论（指定 ID 模式）

拿你要分析的视频链接，抽出原生 ID，例如 B 站 `https://www.bilibili.com/video/BV1xx411c7mD` → `BV1xx411c7mD`。

```bash
# 抖音 / 快手 / B站 / 小红书 / 微博 / 贴吧 / 知乎 都是这个套路
python main.py --platform bili --lt qrcode --type detail --id BV1xx411c7mD
```

- 首次会弹二维码，用对应 App 扫码登录（登录态会缓存，掉线再扫）。
- 跑完，评论落在 `MediaCrawler/data/bili/<类型>_comments/<ID>_comments.csv`（文件名含视频 ID，VideoInsight 就靠这个关联）。

> 教学版 MediaCrawler 已对评论者做匿名哈希 + 昵称脱敏，且不再采集创作者个人信息，合规上更稳。

---

## 4. 让 VideoInsight 读到这些数据

在 VideoInsight 的 `.env`（项目根目录）里指过去：

```bash
# 绝对路径最稳；或相对 VideoInsight 根目录
MEDIA_CRAWLER_OUTPUT_DIR=../MediaCrawler/data

# 备选：直接打 MediaCrawler 的 api(:8080)。端点目前待核实，失败会自动降级为空，不影响主链路。
# MEDIA_CRAWLER_API_URL=http://127.0.0.1:8080
```

重启 VideoInsight 服务后生效（`MEDIA_CRAWLER_OUTPUT_DIR` 或 `MEDIA_CRAWLER_API_URL` 任一非空即启用评论抓取）。

---

## 5. 在 VideoInsight 里用

正常提交一条**同一个视频**的链接分析即可（`with_comments` 默认 `true`，summary/interview 模式会自动融合评论）：

- 后端 `fetch_comments_for_url()` 按 `detect_platform(url)` + 从 URL 抽出的视频 ID，去 `MEDIA_CRAWLER_OUTPUT_DIR` 里找文件名含该 ID 的评论文件，归一化后拼进 LLM 提示词。
- 结果 `AnalysisResult.comments` 存归一化评论（含二级评论树 `sub_comments`）；前端「观众评论」Tab 展示。
- 抓不到 / 未配置 / 异常 → **优雅降级**：只做字幕分析，评论区显示原因，绝不 500。

匹配规则提醒：适配层是按「文件名包含视频 ID」宽松递归查找。MediaCrawler **指定 ID(detail) 模式**落盘的文件名含 ID，能稳定匹配；**关键词搜索(search) 模式**文件名是按关键词命名的，匹配不到——所以对接请用 detail 模式。

---

## 6. 字段映射（已对齐）

MediaCrawler 落盘的每行 → VideoInsight `CommentItem`：

| MediaCrawler 字段（常见别名） | CommentItem 字段 |
|---|---|
| `comment_id` | `comment_id` |
| `parent_comment_id` / `parent` | `parent_comment_id`（用于还原楼中楼） |
| `content` / `message` / `comment` | `content` |
| `nickname` / `user_name` / `uname` / `name` | `author_nickname` |
| `like_count` / `like` | `like_count` |
| `create_time` / `ctime` / `add_ts` | `create_time` |

二级评论（楼中楼）由 `parent_comment_id` 在 VideoInsight 侧重建为 `sub_comments` 树，前端递归渲染。

---

## 7. 验证是否接通（不依赖真实爬虫）

没有真实 MediaCrawler 也能验证整条链路：在 `MEDIA_CRAWLER_OUTPUT_DIR` 指向的目录里放一个文件
`<视频ID>_comments.csv`（字段如上），再分析同一条链接即可看到评论出现在结果与前端。
项目测试 `tests/test_comment_fetcher.py` 的 `test_analyze_injects_comment_tree` 就是这么验的。

---

## 8. 风险与注意（再强调）

- **ToS / 风控**：各平台限制自动化抓取；控制频率、别大规模爬，避免封号。掉登录态要重扫。
- **许可红线**：`NON-COMMERCIAL LEARNING LICENSE 1.1`，仅限非商业学习用途；商业化前必须换合规数据源（如各平台开放 API / 付费数据服务）。
- **默认范围有限**：检查评论条数上限和 `ENABLE_GET_SUB_COMMENTS`；后台服务不使用上游的 10 条上限。
- **字段差异**：各平台评论字段名/层级略有不同，归一化层已做别名兼容，但新平台接入前建议抽样核对一次。
- **不是核心路径**：评论是「可选增强」。任何抓取失败都降级为纯字幕分析，不会拖垮主功能。
