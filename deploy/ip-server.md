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

## Bilibili HTTP 567 Fix

On 2026-11-11, the original failed Bilibili link was reproduced on the server:
an ordinary yt-dlp audio download returned HTTP 567, while a Range request to
the same CDN succeeded. Using 1 MiB HTTP chunks downloaded the complete
4,244,718-byte audio stream. The application now uses ranged downloads for
Bilibili only, retains API-provided backup CDNs, checks selected candidates,
limits per-node retries, and refreshes signed URLs on outer retries.
Missing legacy fragments fail instead of silently producing partial media.
No proxy allowlist, credentials, dependency versions, or global networking
settings changed.

- Application image: `videoinsight:243fe78`.
- Both Windows and the isolated Linux runtime passed all 442 Python tests.
- Real audio and merged source-video downloads succeeded; both files were
  read through completely by ffmpeg and measured 196.5 seconds, consistent
  with the API's rounded 197 seconds.
- An actual application task for the original failed link completed download
  and Groq transcription in about 13.1 seconds, producing 1,176 characters.
  Detail view and used marker passed. The measured ASR input was 196.5 seconds.
- The last speech timestamp was 183.33 seconds. An initial deployment
  verification incorrectly required speech after 185 seconds and triggered
  rollback despite successful transcription. Verification was corrected to
  check measured audio completeness and valid timestamp bounds, then rerun.
- Only isolated verification tasks were deleted. The original failed task
  and all historical task counts were preserved; failed history is not
  automatically resubmitted.

Submissions were temporarily gated, all clients' tasks drained, and the old
container, SQLite database, private environment, and Nginx config backed up
before switching. The successful transaction's root-only backup is
`/opt/videoinsight/backups/20261111-bilibili-243fe78-verified`.
The previous application is stopped as
`videoinsight-before-bilibili-243fe78-verified` with restart disabled.
The original Nginx config was restored byte-for-byte and submissions reopened.
Application and Groq sidecar reported healthy with no OOM events.
The Groq sidecar and pre-existing unrelated containers retained their IDs
and start times. All application environment settings were verified unchanged.

## Cookie Authorization Environment Detection

On 2026-11-11, the missing Chrome/Edge error was traced to the existing
authorization flow launching a native browser on the backend host. This
Docker service has no desktop display, so installing a browser alone would
not provide a login window on the visitor's computer.

- Application image: `videoinsight:e17fd24`.
- `COOKIE_LOGIN_MODE=auto` now detects this headless environment and uses
  import-only authorization. File/JSON upload and pasted export content are
  directly visible; native browser and QR controls are hidden.
- Local desktop deployments retain their existing native-browser login flow.
  Inline web QR login and remote desktop access were not added.
- Windows and the isolated Linux runtime each passed all 450 Python tests.
  Desktop and mobile screenshots were checked. File and text imports were
  exercised on an isolated local test service with dummy data only.
- Production checks verified authorization capabilities, rejection of
  unsupported native-browser actions, login protection, a mock task, detail
  view, the used marker, and actual Groq model listing (HTTP 200). Only the
  isolated verification task was deleted; historical task counts matched.
- No real platform login data was imported. Existing cookie storage and all
  application environment settings were verified unchanged. The Bilibili
  download fix and private Groq route remain in place.

Submissions and cookie writes were temporarily gated, all clients' tasks
drained, and SQLite, the private environment, container configuration, and
Nginx vhost backed up before switching. The root-only backup is
`/opt/videoinsight/backups/20261111-cookie-auth-e17fd24-verified`.
The previous application is stopped as
`videoinsight-before-cookie-auth-e17fd24-verified`, with restart disabled.
The original Nginx configuration was restored byte-for-byte and submissions
reopened. Application and Groq sidecar reported healthy with no OOM events.
The sidecar and unrelated containers retained their IDs and start times.

Runtime package versions are recorded at
`/opt/videoinsight/releases/e17fd24/runtime-packages.txt`.
The public IP endpoint remains HTTP. Use HTTPS or an encrypted SSH tunnel
before importing real Cookies; this release does not add TLS.

## Inline Douyin QR Login

On 2026-11-12, inline web QR authorization replaced import-only mode for
this server. Native Chrome/Edge windows are not required on the visitor's
computer or the server.

- Application image: `videoinsight:1451cb5`, including feature commit
  `9e734a1` and the HttpOnly cookie/browser health fixes.
- Private browser image:
  `chromedp/headless-shell@sha256:830fa77e0602cb42e5cb2cbd9237ba27b649ad69d5fb82b54713aac8b7ab4f29`.
- Browser container: `videoinsight-qr-browser`, Compose project `qr-browser`.
  It shares `videoinsight-network` with the application and publishes no
  ports. Do not expose its CDP port to the Internet.
- Only `COOKIE_QR_BROWSER_URL=http://videoinsight-qr-browser:9222` was
  added to the private application environment. Existing settings, API
  keys, the private Groq route, and cookie storage were preserved.
- The browser uses UID 65532, a read-only root, no capabilities,
  no-new-privileges, 512 MiB memory, 0.75 CPU, 128 PIDs, a 64 MiB temporary
  filesystem, and bounded logs. An earlier 384 MiB probe hit its memory
  limit; the final configuration passed repeated real QR sessions without
  an OOM event. The health probe uses HTTP/1.1 and keeps its request input
  open briefly so the CDP forwarder can return its response.
- Each login uses a disposable isolated browser context. Only one context
  may be active. Status and cancellation are client-bound; another client
  receives 404. Cancellation, failure, expiry, and shutdown release the
  context. Login sessions expire automatically after 180 seconds even if
  the page is closed.
- Actual login cookies trigger an atomic private-file save, retaining
  other platforms' cookies. HttpOnly Netscape rows are accepted by the
  downloader. The web UI supports loading, countdown, refresh, cancellation,
  expiry, and automatic retry of a pending link after confirmed login.

Windows and the isolated Linux runtime each passed all 476 Python tests,
with one existing dependency deprecation warning. Browser checks at
1440x1000 and 390x844 verified real 512x512 QR images, refresh, cancellation,
and no horizontal overflow. Cancellation was checked against the actual
browser-context list. Staging and production API checks verified real QR
pixels, client isolation, no-store responses, context cleanup, mock task
completion, detail view, used marker, and test-task deletion. Production
Groq model listing returned HTTP 200.

No real Douyin account was authorized during verification; phone-side
approval remains the account owner's action. Existing cookies and global
historical task counts were unchanged. The temporary staging container,
test browser, and SSH forwarding listener were removed or closed.

The cutover gated submissions, drained all clients' queued/running tasks,
and backed up SQLite, private environment, container configuration, and
Nginx before switching. Root-only backup:
`/opt/videoinsight/backups/20261112-inline-qr-1451cb5-verified`.
The old application remains stopped as
`videoinsight-before-inline-qr-1451cb5-verified`, with restart disabled.
The Nginx vhost was restored byte-for-byte and submissions reopened.
Application, QR browser, and Groq proxy are healthy. The Groq proxy and all
pre-existing unrelated containers retained their IDs and start times.

Browser configuration is maintained in
`/opt/videoinsight/releases/1451cb5/deploy/qr-browser/compose.yaml`.
Runtime packages are recorded at
`/opt/videoinsight/releases/1451cb5/runtime-packages.txt`.
The deployment does not require the operator's Windows computer or a tunnel
to keep running. The public IP endpoint is still HTTP; use HTTPS or an
encrypted SSH tunnel for real account authorization. This rollout does not
add TLS or guarantee that platform risk controls will accept every download.
