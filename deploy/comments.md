# 评论服务部署与验证

入口 `/comments`，与视频分析共用账号登录。支持抖音、快手、B站、小红书的一级评论和回复，后台分页抓取、不设置条数上限、逐页写入 SQLite，页面可选每页 20/50/100 条。导出为逐行 JSON，包含 comment_id、parent_comment_id、root_comment_id、正文、昵称、点赞与时间。

浏览支持一级/全部/仅子评论、最热（点赞数）/最新（发布时间）/采集顺序/热评前 10，主评论可独立分页展开所属回复。前 10 在整个任务的当前范围内选取，不是只排当前页；它是采集数据的点赞排名，不是平台推荐热榜。未完成任务会标明排名仍可能变化。“导出当前筛选”复用同一排序及范围，“导出全部 JSONL”始终保留全量。

评论查询及导出接口接受 `sort=collected|likes|newest`、`scope=all|roots|replies`、`root_id`、`top=1..1000`；查询另外支持 `offset` 和 `limit`（最多 200）。`total` 是应用 top 后的当前结果数，`matched_total` 是未应用 top 的范围总数，`job_total` 是任务入库总数。旧库启动时自动补建时间排序列及索引，兼容秒、毫秒和 ISO 时间，未知时间排在最新排序末尾。并列点赞按时间、评论 ID 稳定排序。

平台适配基于 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)，固定源码版本 `8f583999006d5b692272c608440e193ca282e73b`。镜像保留其源码及 LICENSE；其许可为 NON-COMMERCIAL LEARNING LICENSE 1.1。上游负责平台请求、签名与浏览器登录，本项目负责完整分页、持久化、去重、状态和续抓。B站按时间获取评论，避免将热门列表作为完整评论区。

完整指当前账号可见、接口仍提供的评论；删除、隐藏或权限限制的评论无法恢复。仅当所有一级评论和回复流均到末页时显示“已遍历完成”。缺分页字段、游标重复、请求失败均不判完成。评论区持续变化时，本轮抓取不是冻结快照，需要新建任务重新抓取新增内容。

视频分析默认创建独立评论任务，不等待评论抓完。首次视频总结不会自动包含后台评论结果，结果的“观众评论”页链接到评论中心。完整评论不塞入一次分析响应或浏览器内存。

## 配置

### 本机运行（无需 Docker）

从项目根目录使用主应用的 Python 执行一次安装：

```powershell
.\.venv\Scripts\python.exe -m crawler_service.manage install
.\.venv\Scripts\python.exe -m crawler_service.manage check
.\.venv\Scripts\python.exe -m crawler_service.manage start
```

Chromium 下载受限时，可复用已安装的 Chrome：
`.\.venv\Scripts\python.exe -m crawler_service.manage install --browser chrome`。
Edge 使用 `--browser msedge`。仅复用浏览器程序，启动独立无头实例，不读取日常浏览器的个人资料或标签页。

需要 Git、uv 和 Node.js。安装器检出本文固定的源码版本到 `.vendor/MediaCrawler`，
建立独立 Python 3.12 环境 `.venv-comments` 并安装 Chromium，不修改主应用环境。
离线自检会导入四平台采集器、检查分页接口并实际启动一次 Chromium；不会访问平台或验证登录。

安装成功后，自动在 `.env` 写入本机服务地址、随机私密令牌及绝对状态路径，保留其他配置。
已有非本机服务地址时拒绝覆盖，可使用 `install --no-config` 只安装依赖。
上游源码及 LICENSE 保留，不纳入本项目 Git；不得将其视为商业授权。

`start` 后台启动并检查健康状态和令牌，重复执行不会重复启动；不会停止占用端口的其他程序。
服务绑定 `127.0.0.1:8090`。自定义端口使用 `install --port 8091`，之后 `start` 自动读取。
`serve` 为前台运行，Ctrl+C 停止；后台日志为 `data/logs/comments-service.log`。
Windows 的 `restart.bat` 在已安装独立环境时自动拉起评论服务。
配置后需重启主应用，再打开 `/comments`，导入各平台 Cookies 并创建任务。
`/comment-api/health` 可检查主应用代理与采集环境状态，但健康检查成功不代表平台登录有效。

### 容器运行

- 主应用 `COMMENT_CRAWLER_URL=http://videoinsight-comments:8090`，`COMMENT_CRAWLER_TOKEN` 为随机私密令牌。
- 评论容器同样设置 `COMMENT_CRAWLER_TOKEN`，`COMMENT_CRAWLER_STATE=/state`。
- 评论服务只加入私有 Docker 网络，不发布公网端口。仅主应用代理访问，评论 API 使用账号会话保护，任务按浏览器身份隔离。
- `/state` 为持久目录，包含 comments.db 与 cookies/*.txt。Cookies 通过评论中心导入，权限 0600，不提供读取接口；导入成功只表示已保存，实际登录有效性在抓取时验证。
- 单个抓取进程串行执行，请求间隔 2 秒；临时错误重试 3 次。任务运行上限 24 小时，到时保留断点，可继续。
- 服务重启将未完成任务标为“待继续”，不会悄悄丢失已保存评论。

## 构建

在临时构建目录放置本项目的 crawler_service/，另将固定版本的 MediaCrawler 源码放入 mediacrawler/（包含 LICENSE），然后：

```sh
docker build -f crawler_service/Dockerfile -t videoinsight-comments:VERSION .
docker network create videoinsight-network
docker run -d --name videoinsight-comments --restart unless-stopped \
  --network videoinsight-network --env-file /opt/videoinsight/comments.env \
  -v /opt/videoinsight/comments-state:/state \
  --memory=2g --shm-size=256m --pids-limit=256 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --log-opt max-size=10m --log-opt max-file=3 videoinsight-comments:VERSION
```

主应用运行时也添加 `--network videoinsight-network`。

本次目标服务器内核 4.4、Docker 20.10.7 的 seccomp 会阻断较新的 glibc 调用，因此仅评论容器额外使用 `--security-opt seccomp=unconfined`。依赖安装在受此参数控制的临时构建容器完成后提交为镜像，源码和运行配置与 Dockerfile 一致。主应用仍保留原有 seccomp 配置。现代 Docker 可直接使用上述 Dockerfile 构建。

## 实际运行限制

需要导入四个平台各自有效的 Cookies。登录失效、验证码或平台限制服务器 IP 时，任务会显示需要处理或部分完成，保留评论和断点；更新登录状态或解决网络限制后点击“继续抓取”。同一账号在本机浏览器能访问，不代表数据中心 IP 可以访问。2026-10-23 测试时，该服务器匿名访问 B站视频信息接口得到 HTTP 412，不能据此声称四个平台均已完成线上全量实测。

本地自动化测试覆盖四平台多页一级评论及回复、超过 10 条的完整数据、去重、回复失败后续抓、畸形分页、权限隔离、三种 Cookies 格式、分页导出与重启恢复。真实视频验证须另外提供实际平台登录状态。

## 本次实测结果

2026-10-23 使用用户提供的 B站视频 `BV1zcJf6LEPe`，通过本机已登录浏览器遍历一级评论与回复接口至末页，去重后保存 **2407 条：一级评论 362 条、子评论 2045 条**。全部回复均有已保存的所属一级评论。结果已导入线上评论中心，JSONL 同时保存在本项目 `data/results/BV1zcJf6LEPe-comments.jsonl`。

抓取开始时平台显示 2418 条，结束后显示 2413 条；与实际返回数量的差额原因尚不能确认，因此不声称已与平台标数完全对齐。线上服务器匿名访问 B站返回 HTTP 412，本次数据由本机浏览器采集，不能作为服务器已成功直接抓取的证明。四个平台的服务器持续抓取均仍需有效 Cookies 和可访问对应平台的网络。

全部 249 项 Python 测试通过；线上桌面 1440px 和手机 390px 页面已检查，登录保护、建任务、等待登录提示、继续按钮及页面宽度均正常，浏览器无脚本错误。
