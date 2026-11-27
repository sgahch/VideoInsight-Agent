# Paid ASR with authorized offline fallback

User explicitly authorized paid transcription and automatic free fallback on
2026-11-27, superseding the previous free-only/no-fallback requirement for their
account. No additional external provider is authorized by this change.

Qwen3-ASR-1.7B real server probe succeeded (6.73s recording, 0.45s response), but
returns text only. Preserve actual subtitle boundaries by running the installed
offline Whisper first, cutting PCM audio at its segment boundaries, and using
Qwen to transcribe each actual interval. A failed Qwen interval retains the
offline transcript for that interval. After the first upstream failure, stop
paid requests for the remaining intervals in that task. Do not retry paid POSTs
or send audio to Groq/Cloudflare. Local speech segmentation failure fails before
incurring paid requests. This avoids invented timestamps and preserves SRT/VTT.

Add an explicit `siliconflow_qwen` provider and per-account paid/fallback consent.
Keep the legacy SenseVoice provider free. Pin consent in queued task requests;
revoking consent must block future paid uploads even for existing tasks. Do not
enable shared paid credentials for other accounts. The target account alone gets
the supplied private key, paid authorization, fallback authorization and default.
Report actual provider/model, timestamp source and fallback reason in diagnostics.

Implementation sequence (writing-plans; execute inline, user opted out of questions):

- [x] Add failing tests for paid authorization, exact audio boundaries, successful
      Qwen text, bounded free fallback, failure without fallback consent, account
      isolation, queued consent, diagnostics and retry consent preservation.
- [x] Implement focused paid adapter, config/schema/settings/catalog support and
      workflow diagnostics. Preserve existing providers and account security.
- [x] Add web consent controls, explicit paid price/fallback explanation and task
      selection/retry support; exercise Node and Python regression suites.
- [x] Review, commit and push GitHub, then build candidate from current deployed
      image. Run Linux regression and real paid/fallback audio tests on server.
- [x] Back up/drain/cut over without changing other users or histories. Configure
      the signed current account, exercise real browser controls and subtitle
      downloads, record evidence, and remove synthetic test tasks.

Tech: existing FastAPI, Pydantic, httpx, Python wave, faster-whisper, vanilla JS.
No new package/model download. Keep server limits 640MiB / 1.25CPU / 128 PIDs.
