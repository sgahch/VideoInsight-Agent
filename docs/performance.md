# Performance and recovery

## History

The UI uses `GET /tasks?limit=50&view=summary`. It returns task metadata,
`title` and `media_title`, without transcripts, segments or comments.
SQLite projects these fields without building full result models. The
default `/tasks` response remains unchanged for existing clients.

## Analysis

After the complete audio transcript is ready, the worker saves a partial
result before calling the model. Model output is checkpointed about every
400 ms and flushed on completion or failure. A failed summary does not
discard its transcript or already generated text.

`GET /tasks/{id}/events` sends SSE events:

- `snapshot`: full current task on connection or transcript availability.
- `delta`: newly generated summary text in `text`.
- `progress`: lightweight status and progress fields.
- `complete`: final task, either succeeded or failed.
- `error`: task access or deletion error.

The stream checks progress every 500 ms. Reconnecting starts with a fresh
snapshot, so clients replace prior text instead of replaying it twice.
Browser disconnection does not cancel the background analysis.

## Chat

`POST /tasks/{id}/chat/stream` accepts the same body as `/chat`, sending
`delta`, `done` or `error` events. Chat Completions, Responses and Messages
protocols are supported. Gateways returning ordinary JSON are also accepted.
An interrupted provider stream is reported as an error, not successful
completion. Chat is not automatically resubmitted, to avoid duplicate charges.
The original non-streaming `/chat` endpoint remains available.

## Browser recovery

Streams use `fetch` with the existing client ID and authorization headers.
When event delivery fails, the UI falls back to sequential polling with
exponential backoff capped at 15 seconds. It retries event delivery after
30 seconds. The last viewed task ID is saved locally and restored on refresh,
scoped to the same browser client ID. No model credentials enter task records
or events.

## Verification

`tests/test_streaming.py` covers history size and scoping, durable partial
results, reconnect snapshots, protocol parsing, interrupted streams and
compatibility with the existing endpoints.

With an isolated development server on port 8001, run
`playwright-cli run-code --filename tests/browser_performance_smoke.js`
in a fresh browser session. It simulates incremental output, network loss
and chat failures without contacting model providers. Screenshots are saved
under `output/playwright/`.
