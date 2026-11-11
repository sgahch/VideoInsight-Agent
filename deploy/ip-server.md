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

### Groq Route Verification

A follow-up on 2026-11-11 isolated the network route from the credentials:
both the original and replacement keys could list models through the Windows
machine's existing proxy, while direct requests returned 403. This does not
establish which Groq IP or regional policy caused the rejection.

A temporary SSH reverse forward bound only to the server's loopback address,
restricted to `CONNECT api.groq.com:443`, also succeeded from an isolated
host-network test container. Model listing and actual transcription returned
200. A generated 6.73-second Chinese speech sample was transcribed correctly.
The tunnel and test container were closed afterward; production configuration
and the existing services were not changed.

The application now supports a private `GROQ_PROXY_URL` for HTTP/HTTPS
proxies. An explicit proxy overrides environment proxies for Groq only;
connection failure does not trigger direct or paid-provider fallback.
Proxy credentials are excluded from settings repr and redacted from errors.
The production rollout below supersedes the initial direct-access limitation.
Do not rely on an operator's laptop staying online or expose an open proxy.

DeepSeek model listing returned HTTP 200. The server configuration uses
`deepseek-flash`, which was present in that response, instead of the local
legacy `deepseek-chat` value. No paid summary request was needed for this check.

The installed runtime package versions were recorded separately on the server
at `/opt/videoinsight/releases/e2434d0/runtime-packages.txt`.

## Groq Production Rollout

On 2026-11-11, with the owner's approval, exactly one locally configured
upstream node was installed in a private Docker sidecar. No subscription URL,
complete subscription, or browser session was copied to the server.

- Application image: `videoinsight:9ff867c`.
- Proxy image: `videoinsight-groq-proxy:v1.19.34`, using the official Linux
  compatible release asset verified against its published SHA-256:
  `2763f5b414527f7baf6dc57e3f82f3ab561eedf34bfd7c2f2cde1f7e592efe06`.
- Both services use `videoinsight-network` and `unless-stopped`.
- Proxy runs as UID 65532, without capabilities or published ports, with a
  read-only root filesystem, a 16 MiB temporary filesystem, 96 MiB memory
  limit, 0.3 CPU limit, 64 PID limit, and bounded logs.
- Proxy requires separate generated authentication and allows only
  `api.groq.com:443`. No TUN, DNS listener, controller, or global proxy
  environment was enabled.
- Node credentials stay in `/opt/videoinsight/groq-proxy/config.json`,
  mode 0600, UID 65532, inside a root-only directory. The application proxy
  URL and replacement Groq key stay in the root-only private `.env`.
- The provider's node hostname returned both reachable and unreachable
  ingress addresses. A verified reachable ingress is pinned in the private
  proxy `hosts` configuration; no host-level DNS setting was changed.
  If the provider retires that ingress, update this private override after
  testing a replacement. The health check reports failure, not node rotation.
- An authenticated CONNECT and verified TLS health check runs every three
  minutes. It expects Groq's unauthenticated model-listing response to be
  401 and does not call a model or require a Groq key.

New analysis submissions were temporarily gated in this application's Nginx
vhost. After all clients' queued/running tasks drained, SQLite, `.env`, the
original container configuration, and the Nginx vhost were backed up to
`/opt/videoinsight/backups/20261111-groq-9ff867c` (root-only). The original
container remains stopped as `videoinsight-before-groq-9ff867c`, with restart
disabled, for rollback. Only the Groq key and proxy setting changed; all
other application environment settings were verified unchanged.

Both Windows and the deployed Linux runtime passed all 433 Python tests,
with the existing dependency deprecation warning. The Linux suite needs
network access for an existing real Douyin short-URL redirect test; test-only
comment-service source was mounted read-only, not enabled as a service.

Actual Groq model listing and Chinese speech transcription returned 200.
An application API upload in real transcript mode then completed successfully
in about 1.6 seconds for the generated 6.73-second sample. Transcript text,
timestamps, detail page, used marker, authentication, and cleanup passed.
Only the isolated test task was deleted; historical task counts matched.
No paid-provider fallback or paid LLM request was used.

The original Nginx configuration was restored byte-for-byte and submissions
were reopened. Both application and sidecar reported healthy. StarDevil and
the other pre-existing containers retained the same IDs and start times.
No host networking, firewall, or unrelated service configuration was changed.
The deployment does not depend on the Windows computer or an SSH tunnel.

Runtime package versions are recorded at
`/opt/videoinsight/releases/9ff867c/runtime-packages.txt`. The public endpoint
is still HTTP; this rollout does not add HTTPS or resolve separate platform
download restrictions.
