# ASR providers and free-only operation

Every task names one provider. Ordinary providers do not automatically fail over
or repeat uploads. The separately authorized paid Qwen provider may retain local
Whisper results after failure, as described below. Explicit selection authorizes that destination;
retrying a task retains its stored selection. Existing task JSON remains readable.

| Provider | Timestamps | Free policy | Readiness |
| --- | --- | --- | --- |
| faster_whisper | Actual segment times | Offline CPU; no API bill | Preinstalled base int8 model and optional dependencies |
| siliconflow | Text only | Exact SenseVoiceSmall allowlist plus official pricing preflight | SILICONFLOW_API_KEY |
| cloudflare | Actual segment times | Subscription API must positively identify Workers Free; unknown/paid plans rejected | Account ID and token with Workers AI and subscription-read access |
| groq | Actual segment times | In strict mode, operator must confirm a free account | Existing key; proxy optional |
| siliconflow_qwen | Offline Whisper speech intervals | Explicit per-account paid and fallback consent | Personal Qwen key plus installed offline model |

Production uses `ASR_FREE_ONLY=true`. Paid Paraformer and unconfirmed Groq
accounts are blocked. There is no endpoint that upgrades a plan. Qwen3-ASR-1.7B
remains blocked through the free SenseVoice adapter. Following explicit user
authorization on 2026-11-27, the separate Qwen provider permits only
`Qwen/Qwen3-ASR-1.7B` with personal paid consent. A real 6.73-second server
probe returned correct text in 0.45 seconds (upstream reports 7 billed seconds).
The current official price is ¥0.00022/second. The supplied voucher list omits
this model; a successful request does not establish voucher eligibility.

## Authorized paid Qwen and free fallback

The paid provider first uses installed offline Whisper to locate speech
intervals. It cuts actual PCM samples at those boundaries and sends each interval
to Qwen once. Qwen replaces the interval text while preserving its actual start
and end, so SRT/VTT remain available. These are offline segment boundaries, not
native Qwen word-level alignment; missed speech or inaccurate boundaries in the
offline pass may limit results. This adds local CPU time and may increase billed
seconds because the upstream rounds each short interval.

If a paid interval fails and fallback consent is present, retain its offline
text and use offline results for every remaining interval. Successful Qwen text
is preserved. No paid POST retry, Groq/Cloudflare upload, or automatic model
upgrade occurs. Paid and fallback consent are pinned at submission, rechecked
before each upload/use, and revocable. Signed accounts never inherit a shared
paid server key. Both direct and resumable uploads preserve explicit denials.
Diagnostics identify the actual model, paid/fallback segment counts, category
of the first failure and the offline timestamp source. Free providers retain
their existing policy.

SenseVoiceSmall cannot satisfy a timestamp-required request. Selecting its
clearly marked text-only option explicitly opts out of timestamps. The task
contains plain transcript text, an empty public segment list and
`diagnostics.asr_timestamps=false`; SRT/VTT export returns 409. Other providers
preserve timestamps, transcript formatting and ownership-checked subtitle downloads.

The catalog in authenticated `/config` exposes configuration and capability
metadata, never keys. Cloudflare's free plan is rechecked before any audio POST.
Lack of billing-read permission blocks use rather than assuming the plan is free.
SiliconFlow pricing verification also fails closed if its official page cannot
be parsed or no longer marks the model free. HTTP/auth/access/quota/timeout/
configuration/capability/response errors are separated and redact upstream bodies.

## Account configuration in the web interface

Expand **配置转写服务 / API Key** below the new-task ASR selector. All four
providers are available in the configuration form even when the task selector
marks them unconfigured. Save the provider credentials to enable selection;
the optional default applies only to new tasks. Queued jobs and explicit retries
retain their original provider. Blank password fields retain saved credentials;
clearing removes only the current account's override, preserving server defaults.

The API exposes GET `/asr/settings`, PUT/DELETE `/asr/settings/{provider}` and
POST `/asr/settings/{provider}/check`. Signed account ownership overrides browser
headers. Administrator settings use a separate administrator scope. Keys are
write-only, stored atomically under the database directory's `asr-settings/`
folder with hashed filenames and Linux directory/file permissions 0700/0600.
Include this folder in private server backups. Keys never enter task JSON or
browser local storage. Identity resets clear the form and reject stale responses.

The check action performs no audio inference or audio upload. It checks the local
model, or cloud credential/network/free-policy prerequisites. Passing this check
does not prove transcription quality or quota availability. Groq personal keys
use a direct connection; only explicit server `GROQ_PROXY_URL` controls the shared
server connection, and environment proxy variables are ignored. The configured
timeout also applies to Groq.

The web configuration release `d45ae50` was pushed to GitHub before the production
cutover on 2026-11-27. Linux regression: 852 passed, 8 skipped; browser unit suite:
40 passed. The final commit only extends the frontend check timeout relative to
the Python regression image. The deployed candidate and production both passed
real 6.73-second audio transcription, segment timestamps and protected SRT/VTT
export; production processing took 7.28 seconds. A real logged-in browser passed
save, immediate selection, blank-key retention, clear, local configuration check
and reload checks. A 390px viewport had no horizontal overflow. Synthetic keys
and test tasks were removed. All 5 account rows and 29 historical task rows were
preserved, and deployed web assets match the GitHub commit.

## Offline deployment

Build with `docker build --build-arg INSTALL_OFFLINE_ASR=1 ...`. Provision
`Systran/faster-whisper-base` from its official Hugging Face repository at a
pinned revision; mount model files read-only and set
`FASTER_WHISPER_MODEL_PATH=/app/models/base`. Requests never download model weights.
Each 60-second PCM chunk runs in a bounded subprocess with one CPU thread,
int8 compute and offline Hub mode. A global semaphore serializes local inference;
the process exits after each chunk so model memory is released. Default per-chunk
timeout is 180 seconds, total transcription budget 3600 seconds. The existing
ffmpeg conversion timeout remains 1800 seconds.

The isolated model process peaked at 347.6 MiB RSS under a 512 MiB limit.
The complete production upload/transcription workflow processed a real
6.73-second Chinese recording in 7.81 seconds. A certificate-verified public
HTTPS test processed 74.06 seconds in 84.04 seconds, yielding 15 timestamped
segments through 73.68 seconds across the 60-second chunk boundary. SRT and
VTT downloads passed. These measurements do not guarantee long-video throughput.
Base Whisper may make recognition errors, including traditional Chinese output;
larger models should not be loaded on this host without a new capacity check.

Cloud adapters have transport-contract tests. No SiliconFlow or Cloudflare
credentials were supplied in the referenced conversation or project configuration;
their live inference is not claimed as verified. The deployment's actual working
default is offline Whisper, not Groq or an unauthenticated cloud endpoint.

Sources: [SiliconFlow pricing](https://www.siliconflow.cn/pricing),
[transcription API](https://docs.siliconflow.cn/docs/api/audio-transcriptions-post),
[Cloudflare model/schema](https://developers.cloudflare.com/workers-ai/models/whisper-large-v3-turbo/),
[Cloudflare free allocation](https://developers.cloudflare.com/workers-ai/platform/pricing/),
[faster-whisper](https://github.com/SYSTRAN/faster-whisper).
