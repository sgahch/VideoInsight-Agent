# IP 端口部署

2026-11-11 新服务器部署见 [IP server deployment](ip-server.md)。
以下保留旧服务器部署记录，不代表新服务器的配置。

访问地址：`http://64.90.27.136:8000`。账号 `ynchen`，密码由部署时设置。

- 项目目录：`/opt/videoinsight`
- 配置：`/opt/videoinsight/shared/.env`（仅 root 可读，密钥不打入镜像）
- 持久化数据：`/opt/videoinsight/shared/data`、`/opt/videoinsight/shared/media`
- 容器：`videoinsight`，镜像带版本号，自动重启策略 `unless-stopped`
- 评论中心：`/comments`，独立容器 `videoinsight-comments`；[配置与验证说明](comments.md)
- Nginx：`/www/server/panel/vhost/nginx/videoinsight-8000.conf`
- 容器内部服务只映射到服务器 `127.0.0.1:18000`；Nginx 对外监听 `8000`

这台服务器的 Docker 20.10.7 与 Bookworm 镜像的线程调用不兼容，使用已验证可运行线程的 Python 3.13 Bullseye 镜像构建。

2026-10-23 B 站 412 修复部署：当前镜像 `videoinsight:20261023-bili412-02`，基于
`videoinsight:20261023-02` 仅替换 `app/adapters/downloader.py` 和新增 `app/adapters/bilibili.py`。
构建目录 `/opt/videoinsight/releases/bilibili-412-20261023-01`，原容器保留为
`videoinsight-before-bili412-20261023`。详情接口 412 时回退分 P 接口，再通过播放接口下载。
已在服务器实测 `BV1fZPQzuERu` 的 197 秒音频和完整视频下载，公网健康检查返回 `ok`。
线上重新提交原失败链接后，任务 `3dea28ec31184380a3c093deec570bfd` 已成功完成下载、
转写和 AI 总结，转写长度 1375 字符。本地全量回归测试 216 项通过。

查看状态与日志：

```sh
docker ps --filter name=videoinsight
docker logs --tail 100 videoinsight
docker restart videoinsight
```

修改账号密码：在本项目 Python 环境运行以下命令，按提示输入新密码，将输出哈希写入服务器配置中的 `VIDEOINSIGHT_AUTH_PASSWORD_HASH`。`VIDEOINSIGHT_AUTH_USERNAME` 是账号；`VIDEOINSIGHT_SESSION_SECRET` 应保持为随机私密值。修改账号、密码或会话密钥会让此前登录失效。

```sh
python -c "import getpass; from app.account_auth import hash_password; print(hash_password(getpass.getpass('New password: ')))"
```

修改配置后必须重建容器，`docker restart` 不会重新读取 `--env-file`：

```sh
IMAGE=$(docker inspect --format '{{.Config.Image}}' videoinsight)
docker stop videoinsight
docker rename videoinsight videoinsight-previous
docker run -d --name videoinsight --restart unless-stopped \
  --network videoinsight-network \
  --env-file /opt/videoinsight/shared/.env \
  -p 127.0.0.1:18000:8000 \
  -v /opt/videoinsight/shared/data:/app/data \
  -v /opt/videoinsight/shared/media:/app/media \
  --log-opt max-size=10m --log-opt max-file=3 \
  --cap-drop ALL --security-opt no-new-privileges:true "$IMAGE"
```

`videoinsight-previous` 保留供检查；再次更新时使用不同的备份名称。应用使用 SQLite 和进程内任务队列，维持单进程运行。
