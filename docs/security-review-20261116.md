# 2026-11-16 Security And Regression Review

This review covers account authentication, password recovery, per-account
Cookies, inline QR authorization, the Windows login helper, model requests,
video URL handling, and the deployment workflow. It is a targeted source review
and regression exercise, not a guarantee that the application has no other
vulnerabilities.

## Confirmed Issues And Fixes

| Priority | Confirmed problem | Fix and regression |
| --- | --- | --- |
| High | Ordinary accounts could direct model probes and video downloads to loopback addresses. Platform routing accepted names embedded in an unrelated URL. | Exact official single-video URLs, query allowlists, same-platform short-link validation, public DNS checks, and IP-pinned native HTTP requests. Queued/recovered tasks, source-video downloads, and comment retries are checked again. |
| High | An upstream model response could echo the configured server API key in an error or probe sample. | Redact configured and request keys in errors, non-JSON snippets, samples, normal output, and streaming chunks, including keys split between chunks. Unexpected probe exceptions return a fixed message. |
| High | Logging out removed the browser Cookie but did not invalidate a copied signed session. | Store only a SHA-256 revocation hash and expiration in SQLite. Reject replay for both roles while retaining other devices' sessions. |
| Medium | A helper start waiting for the database lock could race with logout. | Recheck the website account under the same write lock used to create the handoff. Logout revocation and helper cancellation share one transaction. |
| Medium | Helper completion performed blocking database/file work in the async request handler. | Run the complete persistence transaction in the thread pool. Test concurrent requests and write-lock races. |
| Medium | Empty, expired, or unrelated-domain session Cookies could be reported as logged in. Legacy browser matching could select a spoofed domain or unrelated tab. | Require nonempty, unexpired login Cookies on exact Douyin domains, and parse page hostnames rather than matching substrings. |
| Medium | After repeated polling failures, the website stopped checking a pending helper until a separate browser event occurred. | Continue bounded exponential-backoff polling; stop for invalid website sessions. Test recovery without an `online` event. |
| Medium | Helper cancellation, queued claims, browser cleanup errors, and versioned uninstallation had inconsistent outcomes. | Close or cancel owned work, keep a confirmed save successful despite cleanup warnings, retry transient Windows file locks within a bounded window, reject linked cleanup directories, release resources independently, and recognize only the helper's own known install paths. |
| Medium | A non-object model JSON response could reference an undefined exception variable. | Return the intended dependency error and test malformed response shapes. |

The Windows helper uses a new temporary browser profile for every handoff. It
does not read a daily browser profile or reuse an administrator's authorization.
Its executable is unsigned; users must verify the source and intended website
account rather than bypass security warnings.

## Verification

The README records the final suite count, browser viewport coverage, and the
current screenshots. Tests use isolated accounts, synthetic HttpOnly Cookies,
fake SMTP, and temporary local HTTP/TLS services. The TLS regression uses the
real HTTPX transport to verify that the original hostname, not the pinned IP,
is used for SNI and certificate validation. No real Douyin phone confirmation
or installation into the developer's actual protocol registry is performed.

`pip-audit` is run separately on the developer environment and the actual
container package inventory. Security minimums are in
`constraints-security.txt`; the Docker build also upgrades its installer.
Scanner output is a point-in-time advisory check, not an operating-system image
scan or proof that every dependency is safe.

The default pytest collection is restricted to `tests`, excluding generated
test copies under `output`. Deployment scripts have a separate 41-test offline
suite. Their maintenance gate also blocks task deletion to avoid treating a
legitimate concurrent deletion as a failed upgrade.

## Remaining Boundaries

- Server configuration and administrator-supplied addresses are trusted,
  including internal model endpoints.
- yt-dlp receives canonical official platform inputs for ordinary accounts, but
  platform extractors and returned CDNs remain trusted dependencies. Use network
  egress controls for a stronger deployment boundary.
- Email quotas and bounded queues are not per-user billing or comprehensive
  application rate limiting.
- The helper is Windows-only and has no automatic update mechanism.
- Platform QR loading, phone approval, account challenges, and anti-abuse
  restrictions still require real user verification.
- Saved login Cookie presence is not a live verification of platform access.
  Even unexpired Cookies may be rejected or revoked by the platform.
- Existing comment jobs with noncanonical URLs cannot be retried; recreate them
  with a complete official video URL.

Deployment results, rollback location, source identifiers, and verification
details are recorded in [the server runbook](../deploy/ip-server.md).
