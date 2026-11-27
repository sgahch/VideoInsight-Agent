# ASR providers and free-only operation

Every task names one provider. There is no automatic provider failover or
network-error upload retry. An explicit selection authorizes that destination;
retrying a task retains its stored selection. Existing task JSON remains readable.

| Provider | Timestamps | Free policy | Readiness |
| --- | --- | --- | --- |
| faster_whisper | Actual segment times | Offline CPU; no API bill | Preinstalled base int8 model and optional dependencies |
| siliconflow | Text only | Exact SenseVoiceSmall allowlist plus official pricing preflight | SILICONFLOW_API_KEY |
| cloudflare | Actual segment times | Subscription API must positively identify Workers Free; unknown/paid plans rejected | Account ID and token with Workers AI and subscription-read access |
| groq | Actual segment times | In strict mode, operator must confirm a free account | Existing key; proxy optional |

Production uses `ASR_FREE_ONLY=true`. Paid Paraformer and unconfirmed Groq
accounts are blocked. There is no endpoint that upgrades a plan. Qwen3-ASR-1.7B
is deliberately blocked: the official price page checked on 2026-11-27 lists
¥0.00022/second, and the public transcription contract currently enumerates
SenseVoiceSmall and TeleSpeechASR. Sending Qwen test audio would violate the
free-only requirement. Its request shape is compatible with the multipart
adapter, but paid audio inference was not tested or enabled.

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

A real 9.278-second Chinese recording on the 2 GiB production server took
5.64 seconds in an isolated benchmark, with peak RSS 347.6 MiB under a 512 MiB
limit. These are short-sample measurements, not long-video throughput guarantees.
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
