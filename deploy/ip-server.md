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

## Email Registration

On 2026-11-12, verified email registration was deployed with application
image `videoinsight:cd109bd`. The existing administrator login remains valid.

- The login page includes an email registration tab, also available at
  `/register`. Registration requires a six-digit email code and a password
  of 10 to 128 characters, then signs the new account in automatically.
- QQ SMTP uses certificate-verified TLS on port 465. Sender credentials
  exist only in the private server environment, not Git or browser assets.
  Server-side TLS and SMTP authentication succeeded. QQ accepted one real
  registration-code email; inbox delivery was not independently inspected.
- Codes expire after 10 minutes, are single-use, allow five incorrect
  attempts, and have a 60-second resend cooldown. Durable SQLite limits
  cover each email, source IP, and global send volume. Code digests use
  HMAC; account passwords use salted PBKDF2 hashes.
- Signed HttpOnly sessions bind task ownership to the account rather than
  a browser-supplied client ID. Cross-device login retains access to the
  account's tasks; another account cannot read or delete them by forging
  headers. Existing administrator/browser history is preserved, not
  automatically assigned to newly registered accounts.
- Registered users cannot change shared platform Cookies or start the
  administrator's QR authorization. They share the server's configured
  models and worker queue; per-account analysis quotas are not included.
- Only registration enablement, QQ SMTP host/port/username/password, and
  trusted reverse-proxy peer settings were added to the private environment.
  The Nginx-overwritten `X-Real-IP` is accepted only from those peers;
  untrusted forwarded headers cannot choose the registration rate-limit IP.

Windows and the isolated Linux runtime each passed all 504 Python tests,
with one existing dependency deprecation warning. Desktop and mobile
browser checks covered code sending, registration, logout, and email login.
Production verification covered administrator compatibility, two isolated
synthetic accounts, task completion, cross-device access, detail view, used
marker, authorization restrictions, Groq model listing (HTTP 200), and the
private QR browser connection. The synthetic accounts and tasks were removed.

The first cutover automatically rolled back because the verification
cleanup expected HTTP 200 instead of the deletion endpoint's correct 204.
After correcting the verification script, the cutover passed without
resending the real test email. Historical task counts and existing platform
Cookies were unchanged.

Submissions and registration were temporarily gated while queued/running
tasks drained. SQLite, private environment, container configuration, and
Nginx were backed up before switching. The successful transaction's
root-only backup is
`/opt/videoinsight/backups/20261112-registration-cd109bd-verified2`.
The previous application remains stopped as
`videoinsight-before-registration-cd109bd-verified2`, with restart disabled.
The first transaction's backup and stopped failed candidate are retained.

The original Nginx configuration was restored byte-for-byte and submissions
reopened. Application, Groq proxy, and private QR browser are healthy with no
OOM events. The proxy, browser, StarDevil, and other pre-existing containers
retained their IDs and start times. The private environment and database
have mode 0600; the backup directory has mode 0700. Public checks passed for
registration pages, static assets, authentication guards, and invalid-input
rejection, without sending another email.

Runtime packages are recorded at
`/opt/videoinsight/releases/cd109bd/runtime-packages.txt`.
The public IP endpoint remains HTTP. Add HTTPS or use an encrypted tunnel
before transmitting real passwords or platform login credentials over an
untrusted network. This release does not add TLS.

## QR Mobile Follow-Up

On 2026-11-13, the QR polling and mobile UI update was deployed as
`videoinsight:5885d77`. This identifier is derived from the three frontend
file hashes, not a Git commit. The image inherits `videoinsight:cd109bd`
and replaces only `web/app.js`, `web/index.html`, and `web/styles.css`.

Polling starts immediately, resumes on foreground/online events, and
retains the current QR session through transient connection failures.
The UI adds slow-loading feedback, a separate expiry indicator, QR image
download, mobile album-scanning guidance, 44px action targets, responsive
QR sizing, and browser zoom support. No backend or schema changes were made.

Production verification generated real 512x512 QR images and checked
client isolation, no-store responses, cancellation, browser-context release,
mock task completion, detail view, used marker, test-task cleanup, and the
private Groq route. Existing platform Cookies and historical task counts
were unchanged. The public JS/CSS hashes match the local release files;
registration and unauthenticated access guards still work.

Authenticated browser checks used an encrypted SSH tunnel to the production
application. Desktop and Android Pixel 10 emulation loaded real QR images
in 32.449 and 32.932 seconds respectively. Pixel checks confirmed nonblank
images. Layouts at 1440, 390, and 320 CSS pixels had no horizontal overflow;
mobile action targets measured 44px. Downloading the real QR PNG succeeded.
Actual phone approval and real-account automatic save were not performed.
The remaining platform loading delay is not claimed to be eliminated.

The focused local regression run passed 43 tests with one opt-in transport
test skipped and one existing dependency warning. The earlier full suite
passed 504 tests with that transport test skipped; its separate isolated
browser transport run verified automatic saving with synthetic credentials.

The root-only rollback backup is
`/opt/videoinsight/backups/20261113-qr-mobile-5885d77-verified`.
The previous application is retained, stopped with restart disabled, as
`videoinsight-before-qr-mobile-5885d77-verified`. Environment and Nginx
configuration match their backups byte-for-byte. All three service
containers are healthy without OOM events; the QR browser and Groq proxy
kept their container IDs. Frontend sources and their manifest are retained
under `/opt/videoinsight/releases/5885d77`.

The public endpoint remains HTTP. This rollout does not add TLS. Use HTTPS
or an encrypted tunnel for real credentials and platform authorization.

## Email Password Recovery

On 2026-11-14, email-verified password recovery and account security were
deployed as `videoinsight:c25e231`. This identifier is a source-content hash,
not a Git commit. The image inherits `videoinsight:5885d77` and replaces four
authentication modules and nine frontend files; runtime dependencies and
the existing QR mobile styles are unchanged.

- The login page includes a forgot-password action at `/forgot-password`.
  Users verify a six-digit email code before setting a new password.
- Signed-in users open `/account` from the account-security link. Password
  changes require the current password and a code sent to the account's
  server-selected email address. Successful reset/change requires login
  again and invalidates every previous session without changing task
  ownership.
- The bootstrap administrator first binds their own recovery email using
  the current password and an email code. No email is silently inferred
  from SMTP settings. The real administrator's password and unbound-email
  state were deliberately preserved during deployment.
- Reset, change, binding, and registration codes are purpose-separated.
  Codes are HMAC-digested, single-use, password-version-bound, expire after
  10 minutes, and allow five incorrect attempts. Durable email/IP/global
  limits are shared with registration, with a 60-second resend cooldown.
  Anonymous reset-code responses do not disclose account existence.
- Two additive SQLite tables retain administrator credentials and pending
  action codes. Email binding and registration cannot claim the same
  address concurrently. An explicit bootstrap password-hash rotation in
  the private environment overrides the administrator's saved password
  and recovery binding; ordinary restarts retain both.

The full Python regression run passed 529 tests with one opt-in transport
test skipped and one existing dependency deprecation warning. A subsequent
focused run passed all 27 recovery tests, including two additional tests
for concurrent email claims and migration from the legacy user schema.
JavaScript syntax checks and whitespace checks passed.

An isolated browser fixture exercised registration, password change,
forgot-password recovery, first administrator binding, session revocation,
invalid current passwords/codes, and mismatching passwords. Its fixed OTP
and synthetic credentials were not deployed. Desktop and Android-emulated
layouts at 1440, 390, and 320 CSS pixels had no horizontal overflow and
retained at least 44px action targets. Read-only checks of the production
forgot-password page and authenticated administrator security page passed;
no real administrator binding or password change was performed.

Production HTTP verification used a temporary synthetic account and
server-inserted signed test challenges, not real email delivery. It covered
password change/reset, replay rejection, old-password rejection, cross-device
session invalidation, stable task ownership, task completion/detail, and
cleanup. The account, challenges, and task were removed. SMTP certificate
validation, TLS, and authentication passed without sending an email.
Actual recovery-code inbox delivery remains unverified.

The original QR authorization flow also passed an authenticated production
browser regression over an encrypted SSH tunnel. A real nonblank 512x512
platform QR image loaded in 41.647 seconds in Android Pixel 10 emulation,
downloaded successfully, and fit 1440, 390, and 320 CSS-pixel layouts with
44px action targets. The temporary QR session was cancelled. Real phone
approval and real-account automatic save were not performed.

The root-only rollback backup is
`/opt/videoinsight/backups/20261114-password-c25e231-verified`.
The previous application is retained, stopped with restart disabled, as
`videoinsight-before-password-c25e231-verified`. The private environment,
container environment, and Nginx configuration match their backups.
Historical task counts remain five failed and two succeeded; platform
Cookies are unchanged. Application, private QR browser, and Groq proxy are
healthy, and the browser/proxy container IDs are unchanged. Sources and
their manifest are retained under `/opt/videoinsight/releases/c25e231`.

The public endpoint remains HTTP. Use HTTPS or an encrypted tunnel before
entering real passwords or email codes over an untrusted network. This
release does not add TLS.

## QR Confirmation Follow-Up

On 2026-11-14, the QR confirmation correction was deployed as
`videoinsight:eb0e5f0`. This is the SHA-256 prefix of `app/remote_qr.py`, not
a Git commit. The image inherits `videoinsight:c25e231` and replaces only
that backend module. Password recovery, frontend assets, runtime packages,
private settings, and service dependencies are unchanged.

After the account owner reported confirming the phone-side prompt without
a success indication, investigation reproduced two independent defects in
an isolated real Chromium context:

- Missing QR nodes caused every poll to click the login opener again,
  including after a QR had already been displayed. Opening is now limited
  to one successful attempt per browser connection, including across page
  document changes.
- Status text was read only from the QR node's nearby ancestors. Once that
  node was replaced, scan/confirmation messages became unreadable. Status
  now comes from the stable platform login component observed in the live
  page.

The worker preserves scanned/confirming progress rather than reverting to
the waiting state. It reports confirmation processing and delayed results,
and distinguishes detected extra verification, rejection, platform errors,
and server-side saving failures. Only actual login cookies followed by a
successful private-file save can produce confirmed success. New diagnostic
logs contain a shortened opaque session ID, categorical state/error type,
and elapsed time; they do not include cookie values, account names, page
content, or private browser addresses.

All four new real-browser DOM regressions failed against the original
implementation and passed after correction. The focused QR/cookie suite
passed 54 tests. The full suite passed all 542 tests, including the opt-in
real CDP transport tests, with no skips and one existing dependency warning.
Synthetic cookies were used only in isolated contexts and temporary files,
never in the real platform-cookie file.

Production verification covered the new module in a separate process,
then again after cutover: QR replacement, scanned/confirming progress,
synthetic-cookie save, context disposal, administrator compatibility,
password pages, real platform QR generation, mock-task completion/detail,
and cleanup. Existing platform Cookies retained their original hash;
historical task counts stayed at five failed and two succeeded.

Authenticated Playwright checks over an encrypted SSH tunnel used Android
Pixel 10 emulation. A real nonblank 512x512 QR loaded in 33.587 seconds and
downloaded successfully. Layouts at 1440, 390, and 320 CSS pixels had no
horizontal overflow and retained 44px action targets. Separate response
fixtures verified scanned-to-confirming-to-saved feedback, no premature
success, reconnect/foreground resume, expiry, and cancellation at 320, 390,
768, and 1440 CSS pixels. These UI fixture responses did not modify real
server Cookies or authorize a real account.

The first two cutovers rolled back automatically because of verification
script mistakes: expecting a public forgot-password page instead of the
normal authenticated redirect, then omitting the client ID required by QR
start. After correcting and separately passing the complete verification
script, the third transaction succeeded. The failed candidates and their
root-only backups remain available for audit.

The successful rollback backup is
`/opt/videoinsight/backups/20261114-qr-confirm-eb0e5f0-verified3`.
The prior application is stopped with restart disabled as
`videoinsight-before-qr-confirm-eb0e5f0-verified3`. No active QR login was
interrupted by the cutover. The original Nginx configuration and all
application environment values were restored/preserved. Application, QR
browser, and Groq proxy are healthy; browser/proxy container IDs are
unchanged. Sources, tests, and a manifest are retained under
`/opt/videoinsight/releases/eb0e5f0`.

The account owner's real phone confirmation still requires a fresh-code
retest. The reproduced code defects are not proof of the exact platform
response in the earlier attempt, whose response body was not recorded.
The public endpoint remains HTTP; this correction does not add TLS.

## Shared Authorization Navigation

On 2026-11-14, `videoinsight:153a064` replaced only `web/app.js`,
`web/index.html`, and `web/styles.css` on top of `videoinsight:eb0e5f0`.
The identifier is a hash of the three source-file hashes, not a Git commit.
The QR confirmation and password-recovery backend modules are unchanged.

The missing Cookies navigation was caused by registered-account role
handling, not a mobile-only CSS rule: the old frontend hid both the link
and the whole section for non-administrators. All signed-in accounts now
retain the Cookies and model-settings links. Registered accounts see the
shared authorization status, an explicit administrator-management notice,
and a read-only refresh action. Administrator QR/import controls stay
hidden until the account role is verified. Backend administrator guards
remain unchanged, and mobile navigation/action targets are at least 44px.
Asset URLs were versioned to avoid retaining the old hidden-link script.

The focused account/security suite passed all 80 tests, with one existing
dependency warning. Local browser fixtures covered both account roles in
remote, desktop, and import-only modes; failed refresh recovery; read-only
status access; rejected mutation requests; and navigation/layouts at 320,
390, 768, and 1440 CSS pixels. The QR UI regression also passed, including
confirmation feedback, download, reconnect, expiry, and cancellation.

Production HTTP checks verified administrator and temporary registered-user
login, navigation assets, status/model access, and six administrator-only
guards. Authenticated Android-emulated browser checks exercised both roles
at the same four widths without horizontal overflow. No real account
passwords, emails, or roles were changed; temporary test accounts were
removed. A real nonblank 512x512 QR loaded in 30.186 seconds, downloaded,
and fit desktop/mobile layouts. Its session was cancelled. Real phone
approval was not performed.

The successful rollback backup is
`/opt/videoinsight/backups/20261114-cookie-access-153a064-verified`.
The previous container is stopped with restart disabled as
`videoinsight-before-cookie-access-153a064-verified`. The cutover drained
tasks and checked that no QR login was active before stopping the service.
Existing platform Cookies, historical task counts, private environment,
and Nginx configuration were preserved. Source and verification artifacts
are retained under `/opt/videoinsight/releases/153a064`.

The public endpoint remains HTTP; this frontend correction does not add TLS.

## 2026-11-15: Account-Scoped Douyin Authorization

This supersedes the read-only registered-user behavior of the 2026-11-14
frontend release. Every registered account can use inline QR login or
import its own Cookies. Private files live beside the SQLite database in
`account-cookies/<SHA256-of-server-account-owner>/cookies.txt`, outside the
media tree, with atomic replacement and mode 0600 (directory 0700).
Administrator authorization stays in its existing file and is never
copied to registered users.

Signed account identity controls HTTP operations, QR ownership and storage,
background download workers, recovered queued tasks, and on-demand source
video downloads. Client-supplied IDs cannot choose another account.
Unconfigured or expired user authorization never falls back to the
administrator file, system browser, or shared desktop browser profile.
Completed QR results remain readable after the next account starts login.
The QR browser still admits one active context at a time to bound memory;
other accounts get an explicit busy response and can retry.

The optional comment-service API and worker also support private Cookies.
Its authenticated health response must advertise `account_cookies: true`;
the application fails closed for account authorization or job submission
against an older service. The production server currently has no separate
comment service configured, so that service does not need a runtime restart.

Both account roles retain mobile Cookies/settings navigation. Registered
users see "我的抖音授权 · 仅当前账号使用" and the same QR/import workflow.
The public endpoint is still HTTP: account separation is not transport
encryption. Use HTTPS or an SSH tunnel before transmitting real credentials.

### Deployment And Verification

`videoinsight:b95e885` was deployed on 2026-11-15 over
`videoinsight:153a064`. Its identifier hashes the twelve changed application
and frontend source files, not a Git commit. No dependency upgrades or
environment changes were made.

The complete Python suite passed 551 tests with the isolated Chromium CDP
checks enabled, with one existing dependency deprecation warning.
Browser fixtures covered QR waiting/confirmation/success, reconnect,
expiry, cancellation, download, both roles, and 320/390/768/1440px layouts.
Staging and production checks created two temporary registered accounts,
verified private imports and mode 0600 files, concurrent recovered worker
ownership, task and QR access isolation, shared-desktop denial, administrator
compatibility, and cleanup of test users, tasks, and files.

Authenticated Android-emulated production browser checks passed for both
roles at all four widths. A normal user's real nonblank 512x512 Douyin QR
loaded in 73.707 seconds, downloaded, and fit the 320/390/1440px layouts.
That delay remains a usability limitation. The QR session was cancelled;
no phone approval or real Douyin login was performed. Synthetic login
capture and private atomic saving were verified through the real CDP
transport in a separate offline context.

The rollback backup is
`/opt/videoinsight/backups/20261115-account-cookies-b95e885-verified`.
The old container is stopped with restart disabled as
`videoinsight-before-account-cookies-b95e885-verified`. Existing tasks and
QR sessions were drained before the switch. Administrator authorization,
account records, task history, private environment and Nginx configuration
were retained. Source, tests and manifest are archived under
`/opt/videoinsight/releases/b95e885`.

## 2026-11-16: Windows Local Login Helper And HTTPS

`videoinsight:0319728` replaces `videoinsight:b95e885`. Its identifier hashes
eighteen runtime source/binary files, not a Git commit. Ordinary and administrator
accounts can download the optional Windows x64 helper from Cookies authorization.
After a one-time current-user protocol installation, the website hands off to a
fresh local Edge/Chrome login window. The helper shows the destination website
account before opening Douyin, captures only that window's Douyin Cookies, and
reports success only after the server confirms a private save.

The server derives storage ownership from the signed website session. One-time
claim/upload credentials are hashed in SQLite, expire after ten minutes, and
cannot name a different account. Clearing Cookies, logout, password change,
expiry, and cancellation revoke a pending helper. Upload retries acknowledge
the original save without overwriting it. Normal users never reuse the
administrator's authorization. Mobile devices retain inline QR and import;
the Windows controls are hidden there. The installer is not Authenticode-signed.
It requires no administrator elevation, startup task, or daily browser profile.

The IP endpoint now has a certificate-verified HTTPS listener. The certificate is
stored under `/opt/videoinsight/tls/live/videoinsight-ip`, initially expiring at
2026-11-22 03:01:34 UTC. Certbot 5.10.0 uses the short-lived IP certificate
profile, with an every-four-hours renewal schedule in
`/etc/cron.d/videoinsight-tls`. The deployment hook validates and reloads Nginx.
The ACME webroot is `/var/lib/videoinsight-acme`; the protected `/opt` parent
cannot serve the HTTP challenge. HTTP now redirects to HTTPS with status 308,
except for the challenge location.

`videoinsight-helper-tls.conf` is the default TLS listener because IP clients do
not send a hostname SNI value. This changes no-SNI default certificate selection.
The existing `stardevil.cn` SNI certificate was verified unchanged and valid.
The application's trusted-peer middleware accepts forwarded HTTPS only from the
configured reverse proxy addresses, enabling Secure session Cookies and
same-origin checks without trusting arbitrary client headers.

The complete Python suite passed 598 tests, including isolated real Chromium
transport, with one existing dependency deprecation warning. Four subsequent
installer tests passed against a fake registry. The frozen helper self-test
passed. Both account roles passed live HTTPS browser checks at 320, 390, 768,
and 1440 CSS pixels, including start/cancel without altering saved Cookies and
mobile fallback. Staging and production checks verified independent private
files, mode 0600, token claim/replay, cross-account denial, revocation, password
changes, administrator compatibility, and exact executable hashes.

A real isolated Windows Edge process captured synthetic HttpOnly Cookies, and
the actual helper HTTPS client saved them to a temporary ordinary cloud account.
The browser process tree, temporary profile, test account, its private Cookies,
and exported browser sessions were removed. Real Douyin phone approval was not
performed. The executable was inspected but not installed on this Windows user.

The previous container is stopped with restart disabled as
`videoinsight-before-local-helper-0319728-verified`. The protected rollback
backup is `/opt/videoinsight/backups/20261116-local-helper-0319728-verified`.
Existing tasks and QR sessions were drained before switching. Application
environment, administrator authorization, account records, and historical tasks
were preserved. Main, QR browser, and Groq proxy are healthy. Sources, tests,
binary, deployment scripts, and hashes are archived under
`/opt/videoinsight/releases/0319728`.

A full rollback must restore the backed-up HTTP Nginx configuration and disable
the new TLS listener as well as restoring the old application container. The
old image does not understand the trusted forwarded HTTPS scheme, so reverting
only the image would break same-origin authenticated HTTPS requests. TLS renewal
scheduling must also be reviewed as part of that rollback.

## 2026-11-16: Security Review And Helper 1.0.1

`videoinsight:49330ba` replaces `videoinsight:0319728`. The runtime identifier
hashes application/frontend sources, the helper executable, requirements,
the deployment Dockerfile, and the installer wheel, not a Git commit.
The source changes and four README screenshots were pushed to GitHub in
`a32ed96` before the production switch. This section records the completed
deployment; the separate security report is
[`docs/security-review-20261116.md`](../docs/security-review-20261116.md).

The review fixed signed-session replay after logout, ordinary-user SSRF paths,
upstream model-key disclosure, a helper-start/logout race, blocking helper
persistence, invalid login-Cookie detection, exhausted polling, native
cancellation/cleanup/uninstallation handling, and malformed model JSON errors.
Official single-video URL validation also applies to recovered queued tasks,
source-video downloads, comment creation, and comment retries.

The Windows helper is version 1.0.1. Its SHA-256 is
`6bb4513e494505897481a78a14af6b4c41b2102311e169cb05a6e9364f84f4a9`.
Existing installations must download and install the new executable.
It remains unsigned, Windows-only, and without automatic updates.
The executable self-test passed; it was not installed on the developer's
actual Windows user. Real file-lock cleanup and junction rejection were tested.

The formal Python suite passed 776 tests with real isolated browser integration
enabled, with one existing dependency deprecation warning. The independent
deployment-script suite passed 41 tests and Node passed five tests.
Default pytest collection now excludes temporary scripts under `output`.
Isolated browser fixtures covered password recovery/change and helper/QR
states at 320/390/768/1440px. The README uses synthetic test data; its mobile
QR-entry screenshot came from the temporary production test account.

`pip-audit` reported no known advisories in the developer environment or the
actual 59-package candidate inventory after the dependency fixes.
The production application dependencies already met the security floors;
this image additionally upgraded pip from 25.2 to 26.3 using an offline wheel.
The normal Dockerfile upgrades pip and includes `constraints-security.txt`.
This is a point-in-time Python advisory check, not an OS/container scan or a
claim that no other vulnerabilities exist.

The TLS maintenance gate temporarily blocked new submissions, authorizations,
and task deletion. Existing queued/running tasks, QR contexts, and helper
handoffs drained before the application was stopped and SQLite was backed up.
This does not guarantee draining every unqueued long HTTP request, such as an
in-progress streaming chat. The gate was removed after service checks passed.
The original HTTP and TLS Nginx files were restored byte-for-byte.

Production checks verified account-private atomic Cookie saving, mode 0600,
single-use claims, idempotence, cross-account denial, logout/password/clear
revocation, replay rejection, private-URL rejection, and executable hashes.
A real isolated Edge captured synthetic HttpOnly Cookies, and the actual native
client saved them to a temporary ordinary account over certificate-verified
public HTTPS. Both roles passed live start/cancel and layout checks at all
four widths without changing existing authorization. A mobile user agent
retained QR authorization and hid Windows-only controls.
No real Douyin login, phone approval, or real verification email was performed
by this review.

Temporary accounts, private Cookies, browser profiles/states, and the unpublished
staging container were removed. Existing accounts and full task rows matched
the pre-switch backup, and administrator Cookies were unchanged. Runtime source
and helper hashes match the verified local build. Main, QR browser, and Groq
proxy containers are healthy; the latter two were not replaced. HTTP still
redirects to HTTPS with status 308, certificate verification passed, and the
existing TLS renewal cron remains active.

## 2026-11-17: Per-Video Chat Memory

`videoinsight:4d6db32` replaces `videoinsight:49330ba`. This is a source-content
identifier, not a Git commit. The candidate inherits the verified production
image and replaces only application/frontend sources; dependencies, helper
1.0.1, environment, resource limits, mounts, TLS, and auxiliary services are
unchanged.

Chat history is stored as structured messages in browser `localStorage`,
scoped by browser client and video task. Refreshing or reopening a video
restores the conversation. Each follow-up includes that video's completed
prior question/answer pairs. The cache retains up to 20 videos and 20 rounds
per video; model context is additionally bounded to 60,000 historical
characters. Interrupted answers remain visible but are excluded from future
model context. Browser storage is not a server-side conversation database and
does not synchronize across devices.

Local regression: 771 Python tests passed, seven optional integrations skipped,
with the existing dependency deprecation warning. Three Node cache tests passed.
An isolated Linux candidate verified both chat routes, history prompt
construction, role validation, and task isolation without paid model calls.
Production smoke verified administrator login, the new frontend asset hash,
streaming and non-streaming chat requests with history, and invalid-role
rejection. Its synthetic task was removed. New submissions were temporarily
gated and existing tasks, QR contexts, and helper sessions drained before
switching. The original Nginx configurations were restored.

Rollback container:
`videoinsight-before-local-helper-4d6db32-chatmemory` (stopped, restart disabled).
Private backup:
`/opt/videoinsight/backups/20261117-local-helper-4d6db32-chatmemory`.
The backup includes the original database, configuration, and container metadata.
No user credentials or browser history are included in the release source.

Rollback resources are protected under
`/opt/videoinsight/backups/20261116-local-helper-49330ba-security`.
The previous application container is stopped with restart disabled as
`videoinsight-before-local-helper-49330ba-security`. Reverting this release
to `0319728` does not require disabling HTTPS: both understand the existing
trusted proxy scheme. The older `0319728`-to-`b95e885` rollback caveat above
still applies to that separate historical transition.
Sources, tests, screenshots, helper/build inputs, and verification scripts are
archived with hashes under `/opt/videoinsight/releases/49330ba`.

## 2026-11-27: Resumable Audio And Video Uploads

`videoinsight:c4e9a2b` replaces `videoinsight:4d6db32`. The release identifier
hashes source contents, tests, the release Dockerfile, and the base image ID;
it is not a Git commit. A comparison against the running baseline confirmed
that only the eight application/frontend files for resumable uploads changed.
Dependencies, helper 1.0.1, environment values, resource limits, persistent
mounts, TLS, the QR browser, and the Groq proxy were preserved.

The web uploader uses 4 MiB chunks, at most three concurrent requests,
individual retries, and persisted upload sessions. Pause and resume are
available in the upload card. After refresh, the user selects the original
file again; the complete file fingerprint is checked before accepted chunks
are skipped. Sessions default to a 24-hour retention window, with four
pending uploads per owner and a declared-size quota of twice the individual
file limit. Initialization, duplicate chunks, and completion requests are
idempotent. Completed uploads enter the existing analysis queue once.
This resumes file transfer; interrupted ASR/LLM work still follows the
existing task recovery rules.

Session databases and chunks live under `data/tmp/_uploads/<upload_id>/`.
Final assembly happens in the media destination directory before atomic
publication, which supports the existing separate `/app/data` and `/app/media`
Docker mounts. Expired initialization remnants cannot block new uploads,
and database lock contention does not trigger deletion of an active session.

Windows regression passed 793 Python tests with seven optional skips;
all 17 Node tests and JavaScript syntax checks passed. The headless Linux
verification passed 789 tests with eight optional skips. The Tk desktop
window module was excluded from the Linux run because Tk is absent from
the server base image; it passed in the Windows run. The Linux test image
added test-support sources without changing the production dependencies.
One existing dependency deprecation warning remains. All 17 deployment
cutover tests and the upload maintenance-gate check passed.

The isolated Linux service and the production service both verified
authenticated initialization, ownership checks, out-of-order chunks,
resume state, duplicate completion, integrity, and cancellation. Public HTTPS
browser verification paused an 8 MiB plus 17-byte synthetic upload with
chunks 0 and 2 accepted. After refresh, only chunk 1 was sent; the mock
analysis succeeded. The 390px layout had no horizontal overflow. Synthetic
tasks and upload metadata were removed. No paid model inference or real
platform login was used for these checks.

The TLS maintenance gate temporarily blocked new submissions, including
upload endpoints, while existing tasks and login handoffs drained. The
database and root-only configuration were backed up before replacement.
Both original Nginx files were restored, HTTP still redirects to HTTPS,
and public certificate verification and frontend asset hashes passed.
Existing account and task rows were preserved.

Rollback container:
`videoinsight-before-local-helper-c4e9a2b-uploads` (stopped, restart disabled).
Private backup:
`/opt/videoinsight/backups/20261127-local-helper-c4e9a2b-uploads`.
Verified source, manifest, Linux results, and deployment scripts:
`/opt/videoinsight/releases/c4e9a2b`.
The release-specific build file is `Dockerfile.resumable-upload`.
