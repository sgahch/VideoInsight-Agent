# Performance and recovery

## History

The UI shares `GET /queue?limit=200` between the dashboard and sidebar, which
shows the latest 50 items. `GET /tasks?limit=50&view=summary` remains available
for other clients. Both summary views return task metadata,
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
30 seconds. `/view/{task_id}` restores the selected task on refresh, scoped to
the same browser client ID. `/` always opens the queue dashboard. No model
credentials enter task records or events.

## Bounded queue dashboard

The executor is a single-process `ThreadPoolExecutor` with application-level
admission control. Core slots (default 2) run first, then a bounded FIFO
waiting queue (default 32), then overflow slots up to the maximum (default 4).
Once saturated, submissions return HTTP 429 with `Retry-After: 3`; the HTTP
request never waits for a video to finish. Admission failures remove the
new database row and, for uploads, its files.

`GET /queue?limit=200` returns server-wide slot occupancy and capacity, exact
client-scoped status counts, and lightweight task summaries. Running tasks
include the existing progress snapshot, not transcripts or comments. Active
tasks are included even when outside the latest history window. Global pool
metrics contain no task IDs, titles, URLs or credentials.

The dashboard and sidebar use one non-overlapping refresh every 2 seconds
on visible pages (including details), or 15 seconds while the document is
hidden. Detail events also update sidebar status immediately. Unchanged
sidebar contents retain their DOM nodes; temporary network failures preserve
the list and automatically recover. Its list supports status filters, text search,
paging, pending-task removal and failed-link resubmission. Running tasks
cannot be deleted. Detail routes preserve browser navigation, and chat DOM
is cached per task in memory (at most 20 tasks), not shared across videos.

Shutdown preserves pending database records; restart replays them as capacity
becomes available. Previously running tasks still fail explicitly, because
the analysis pipeline does not support checkpoint-based restart.

All videos share `DASHSCOPE_ASR_GLOBAL_WORKERS` recognition-call slots
(default 8). This is independent of the per-video chunk worker count.
The current pipeline uses cloud ASR and an HTTP model provider. Local GPU
inference is not enabled; no local model or CUDA packages are installed by
this change.

## Copied task markers

After a successful clipboard write, the UI calls `POST /tasks/{id}/used`.
The endpoint records the first `used_at` timestamp, is idempotent, and uses
the same strict client ownership and authentication as task reads. The
nullable column is migrated automatically in existing SQLite databases,
independent of analysis status and result updates. Both full records and
lightweight summaries include the marker.

The detail toolbar, dashboard rows and sidebar show "used" independently
of processing status. Clipboard failure never marks a task. If copying
succeeds but saving fails, the UI reports that distinction and allows a
retry. A pending copy retains its original task ID when the user navigates
to another video.

## Verification

`tests/test_streaming.py` covers history size and scoping, durable partial
results, reconnect snapshots, protocol parsing, interrupted streams and
compatibility with the existing endpoints.

With an isolated development server on port 8001, run
`playwright-cli run-code --filename tests/browser_performance_smoke.js`
in a fresh browser session. It simulates incremental output, network loss
and chat failures without contacting model providers. Screenshots are saved
under `output/playwright/`.

`tests/test_queue_dashboard.py` covers FIFO ordering, overflow, rejection,
scoping, safe deletion, shutdown recovery, upload cleanup and shared ASR
limits. `tests/browser_queue_smoke.js` submits two actual mock analyses to an
isolated server on 8001, tests deep links and navigation, then uses explicit
browser-only fixtures to exercise all queue states and responsive layouts.
It never calls real ASR or model providers.

`tests/test_task_usage.py` covers legacy migration, ownership, idempotency,
concurrent writes and persistence across pipeline updates and restarts.
`tests/browser_usage_smoke.js` verifies real clipboard copying, persistent
markers, failure handling, task-switch races, shared automatic sidebar
updates, network recovery and desktop/mobile badge layouts on the isolated
server.
