# Windows Local Login Helper

The cloud website cannot read another site's browser Cookies. The optional
Windows helper bridges that boundary with a one-time, account-bound handoff.
It does not run a background web server and does not read a daily browser profile.

## User Flow

1. Sign into the HTTPS website with your own administrator or ordinary account.
2. Download `VideoInsightLoginHelper.exe` from Cookies authorization and run it.
3. Choose the helper's install action. It registers `videoinsight-login://` for
   the current Windows user, without administrator privileges or a startup task.
4. Return to the website and choose the local Douyin login action.
5. Allow the browser to open the installed helper. Check the destination account
   displayed by the helper, then open its dedicated Douyin window.
6. Complete the official Douyin login. Detection and saving are automatic.
   Both the helper and website report success only after the server confirms.

Windows x64, Microsoft Edge or Google Chrome, and network access are required.
The executable is not Authenticode-signed. Do not disable security software or
ignore an unexpected publisher, origin, or destination account. Source and
build instructions are provided below.

The helper does not support Android, iOS, macOS, or Linux in this release.
Mobile web users retain inline QR and manual import. Downloading the helper does
not authorize any account; every authorization requires a fresh website handoff.

## Isolation And Revocation

- The website session determines the destination. Client IDs, posted owners, and
  helper-supplied account names cannot select a different user's storage.
- Registered users save to `data/account-cookies/<owner-sha256>/cookies.txt`.
  The existing administrator Cookie file is not copied or used as fallback.
- Every local handoff creates a new temporary browser profile, even on the same
  PC. An account previously authorized on that PC is not silently reused.
- The helper reads only exact `douyin.com` domain Cookies from its own browser,
  including HttpOnly Cookies. Other sites' Cookies are never sent.
- A Windows job owns the browser process tree, including Edge's launcher
  relaunch. Browser children terminate when the helper exits. Completion,
  cancellation, and errors attempt to remove the temporary profile with bounded
  retries. Persistent file locks produce a cleanup warning; a server-confirmed
  save remains successful even if local cleanup needs attention.
- The helper connects only to the compiled trusted origins, verifies HTTPS
  certificates, and refuses redirects. Public HTTP is rejected on both sides.
  Explicit local development origins are allowed over loopback HTTP.
- A handoff lasts ten minutes. Its initial bearer credential is consumed by an
  atomic claim and replaced with a separate upload credential. Only SHA-256
  hashes are stored, not the raw credentials or captured Cookies.
- Claim, completion, cancellation, and clearing use SQLite write transactions.
  Repeated completion can acknowledge an earlier save but cannot overwrite it.
  New authorization supersedes a previous pending one for the same account.
- Website logout, Cookie clearing, password changes, account deletion,
  cancellation, and expiration prevent old helpers from writing new Cookies.
  Clearing also revokes the idempotent acknowledgment credential.
- The helper periodically checks revocation and closes its browser when revoked.
  No website password, administrator token, or SSH credential is given to it.
- Cookie writes atomically replace only Douyin entries, preserve other platform
  entries, and use private file permissions. Values are not returned to the page.

Do not publish a browser's CDP port. The temporary debugging port listens only
on loopback. A Windows user with control over the same desktop session can still
access that user's processes; this is not an OS-user security boundary.

## API

Authenticated website endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/cookies/helper/start` | Create the current account's one-time handoff |
| GET | `/cookies/helper/{id}/status` | Read only the current account's handoff |
| DELETE | `/cookies/helper/{id}` | Revoke the current account's handoff |
| GET | `/cookies/helper/download` | Download the Windows executable |

Native callbacks authenticate only with the scoped handoff bearer credential,
not a website cookie. These exact callback paths are public to the middleware
but reject requests lacking the correct credential:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/cookies/helper/claim` | Consume the launch credential and get upload credential |
| POST | `/cookies/helper/progress` | Check whether the upload credential is still valid |
| POST | `/cookies/helper/complete` | Save login Cookies once, with idempotent acknowledgment |
| POST | `/cookies/helper/fail` | Finish without saving |

The website only stores the pending session ID in `sessionStorage`; it does not
persist the launch credential. A page refresh resumes status checking.

## Build And Test

Use a Windows Python virtual environment, then:

```powershell
.\.venv\Scripts\python.exe -m pip install -r local_helper/requirements-build.txt
.\local_helper\build.ps1
.\web\downloads\VideoInsightLoginHelper.exe --self-test
Get-FileHash .\web\downloads\VideoInsightLoginHelper.exe -Algorithm SHA256
```

The build includes only the helper and its runtime. It must not include `.env`,
application data, local Cookies, server credentials, or daily browser profiles.
The generated binary is ignored by Git, but must be included in a release's
`web/downloads` directory. Missing binaries hide the website helper controls.

Trusted production origins live in `local_helper/core.py`. Adding a production
domain requires rebuilding and distributing a new helper; a URI cannot override
this list. Protocol installation uses `HKCU\Software\Classes\videoinsight-login`.
The helper's removal action unregisters only its own matching command. The
installed executable remains under the user's local app data and may be removed
after closing the helper.

```powershell
$env:VIDEOINSIGHT_AUTH_PASSWORD_HASH = ''
$env:VIDEOINSIGHT_AUTH_TOKEN = ''
$env:HELPER_BROWSER_TEST = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_local_login.py tests/test_local_helper.py tests/test_local_helper_browser.py tests/test_local_helper_install.py
```

The browser integration uses a fresh, real headless Chromium and synthetic
HttpOnly Cookies. It never opens Douyin or a daily profile. The web UI regression
uses `tests/password_browser_server.py` and
`tests/browser_local_helper_smoke.js`, with OS protocol invocation intercepted.
Real phone approval and platform-side anti-abuse challenges require user testing.

## Version 1.0.1

Deployed on 2026-11-16 as `videoinsight:49330ba`. The helper trusts
`https://206.187.222.21`; public HTTP redirects to HTTPS. The Windows executable
SHA-256 is
`6bb4513e494505897481a78a14af6b4c41b2102311e169cb05a6e9364f84f4a9`.
It has been built, inspected, and self-tested, not installed on the developer's
Windows user. Existing installations need to download and install 1.0.1;
there is no automatic update mechanism.

The formal Python suite passed 776 tests with real isolated Chromium integration
enabled. Another 41 deployment-script tests and five Node tests passed.
Installer tests use an in-memory registry without changing actual Windows
protocol associations. Windows cleanup tests include a real delayed file lock
and reject directory junctions. Both website roles passed isolated browser
checks at 320, 390, 768, and 1440 CSS pixels.

The release passed isolated and production service checks for account separation, private
files, one-time claims, revocation, logout-session replay, and exact download
hashes. Both roles passed live HTTPS start/cancel and layout checks at
320, 390, 768, and 1440 CSS pixels, plus a mobile-user-agent QR fallback check.
A fresh real Edge profile captured synthetic HttpOnly Cookies; the actual
native client saved them over certificate-verified HTTPS to a temporary ordinary
account. The account, private Cookies, temporary profile, and exported browser
states were removed. Existing accounts and full task rows match the backup;
administrator authorization is unchanged.

The previous image was `videoinsight:0319728`. Deployment and rollback details
are recorded in [the server runbook](../deploy/ip-server.md).
No real Douyin login or phone approval was performed.
