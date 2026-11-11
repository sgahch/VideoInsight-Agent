# IP server deployment

Target: `http://206.187.222.21/`. The IP-only Nginx virtual host does not
replace the existing domain virtual hosts.

## Layout

- Release source: `/opt/videoinsight/releases/<commit>`
- Root-only configuration: `/opt/videoinsight/shared/.env` (mode `0600`)
- Persistent state: `/opt/videoinsight/shared/data` and `shared/media`
- Container: `videoinsight`, binding only `127.0.0.1:18000`
- Nginx: `/www/server/panel/vhost/nginx/videoinsight-ip.conf`
- Runtime UID/GID: `10001:10001`, matching the Dockerfile

Transfer a clean `git archive` over verified SSH. Do not upload the local
`.env`, browser cookies, databases, or downloaded media as part of the source.
Build with the repository Dockerfile:

```sh
docker build --network host -t videoinsight:<commit> /opt/videoinsight/releases/<commit>
```

The default Docker bridge failed DNS resolution during setup. The build uses
host networking only while installing dependencies. Runtime uses the separate
`videoinsight-network` bridge, whose outbound DNS and HTTPS were verified:

```sh
docker network create videoinsight-network
```

Provision `shared/.env` separately. Enable account authentication with
`VIDEOINSIGHT_AUTH_USERNAME`, `VIDEOINSIGHT_AUTH_PASSWORD_HASH`, and a stable
random `VIDEOINSIGHT_SESSION_SECRET`. Use a separate application password,
not the SSH password. Copy only the required cloud API settings.

This server also hosts other applications and has about 2 GiB RAM. The
application uses cloud Groq ASR, two task slots, and one chunk worker per
video. No local model weights or GPU runtime are installed.

```sh
docker run -d --name videoinsight \
  --restart unless-stopped \
  --network videoinsight-network \
  --env-file /opt/videoinsight/shared/.env \
  -p 127.0.0.1:18000:8000 \
  -v /opt/videoinsight/shared/data:/app/data \
  -v /opt/videoinsight/shared/media:/app/media \
  --memory 640m --memory-swap 640m --cpus 1.25 --pids-limit 128 \
  --log-opt max-size=10m --log-opt max-file=3 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  videoinsight:<commit>
```

Run a single Uvicorn worker: the task queue is process-local and uses SQLite.
Check the container health and authenticated endpoints before installing
`deploy/nginx-ip.conf`. Run `nginx -t` before reloading Nginx.

## Operations

```sh
docker ps --filter name=videoinsight
docker logs --tail 100 videoinsight
curl -fsS http://127.0.0.1:18000/health
```

Changing `.env` requires recreating the container; `docker restart` alone
does not reload `--env-file`. Before replacing a container, let running
tasks finish, back up the database and configuration, and retain the old
image/container for rollback. Do not run two instances against the same
task database.

The initial IP endpoint is HTTP, not HTTPS. Use it on a trusted network;
configure a domain and HTTPS before sending sensitive content over an
untrusted network. The standalone comment crawler is not included in this
lightweight deployment; it needs separate resources and platform sessions.

## Initial Verification

Application image: `videoinsight:e2434d0`. The container is healthy and uses
the `unless-stopped` restart policy. Docker and Nginx are enabled at boot.
The existing `stardevil.cn` HTTP-to-HTTPS redirect was unchanged after reload.

The deployed Linux image passed all 421 Python tests (one dependency
deprecation warning). Public HTTP checks passed for login, API protection,
nine static assets, two mock tasks, details, the used marker, cleanup, and
logout. These checks did not invoke paid model inference. The temporary test
tasks were deleted. Browser checks at 1440x1000 and 390x844 verified the
dashboard and four platform logos; mobile document width matched the viewport.
The only browser resource error was the existing missing favicon.

The initial application login is stored on the operator's Windows machine
in the Git-ignored `output/deploy/login.env`, restricted to that user, SYSTEM,
and administrators. It contains a separate generated application password,
not any cloud API keys or the SSH password.

On 2026-11-11, the configured Groq key returned HTTP 200 from the developer
machine. From this server, both model listing and an actual transcription
request returned HTTP 403 with `{"error":{"message":"Forbidden"}}`. The
request was also rejected without credentials and with another user agent.
The website can run, but server-side transcription is not ready until this
access issue is resolved. No paid-provider fallback was enabled.

DeepSeek model listing returned HTTP 200. The server configuration uses
`deepseek-flash`, which was present in that response, instead of the local
legacy `deepseek-chat` value. No paid summary request was needed for this check.

The installed runtime package versions were recorded separately on the server
at `/opt/videoinsight/releases/e2434d0/runtime-packages.txt`.
