# Runtime contract used by the MVP

This is a narrow implementation note, not a new API or permission model.
It records the relevant shapes from locally cached Wisp documentation and the
OpenAPI document inspected during development. No live runtime requests or
inference were used to establish these notes. Recheck against the installed
release before the owner-run live acceptance test.

## Evidence

- `/tmp/wisp-adapter-contract.md`: approvals, idempotency, cancellation, errors.
  SHA-256: `70f53b05b56b93219ee14cd3928ab6907e6b03f2f839f94b82564c0ef7448cf6`
- `/tmp/wisp-reference.md`: event replay, gaps, late runs, retention defaults.
  SHA-256: `ea7ff6f55e46a9c8e84d0bd4794a9eeff729a650fac1e0d8949c0d00516c395d`
- `/tmp/wisp-openapi.json`: run listing, records, event and confirmation schemas.
  SHA-256: `0a922d80df32dc0e9c3fc1d16bd19b4bbdc67e02ee5a8fd19d41ca12997711ee`

These temporary files are not required to run the client or tests. Their hashes
identify the evidence inspected; they do not prove the currently running release
matches it. The durable summary and synthetic regression fixtures live here.

## Submission and retry

`POST /v1/sessions/{id}/runs` with `wait: false` returns `{runId, status}`.
Repeating the same input/options/idempotency key returns the existing run ID and
current status; it must not be replaced by a fresh key on an ambiguous failure.

The documented default key TTL is 24 hours, configurable at the runtime. The TUI
refuses replay at 23 hours, but cannot guarantee this margin against a runtime
configured with a shorter TTL. It does not inspect administrative configuration.
A 409 can mean that work may already have acted; never silently start over.

### Durable client request identity

Before any POST, the TUI writes the original session, unmodified draft, max-turns
option, key and wall-clock creation time into a bounded, owner-only journal
under the private state directory. It stores an endpoint fingerprint, never a
credential or config contents. `confirmationMode: interactive` and `wait: false`
are fixed by the versioned format. File fsync, atomic replacement and directory
fsync must succeed before network submission. A persisted wall-clock age and
process-local monotonic age both bound retries; a future creation time is
refused rather than refreshing the TTL after clock reversal/restart.

A lease protects the slot across Wi processes. The UI restores an orphaned
request before normal startup/session selection, without posting or changing
its identity. Only explicit `/retry` can resend it. Reservation checks repeat
under the lease immediately before POST, so an already-open client cannot
replace another process's ambiguous request. A different configured endpoint,
unsafe or corrupt storage, I/O failure, or an occupied slot blocks new work.

A valid run acknowledgement is saved with the original identity before the UI
releases its reservation. The acknowledged record stays until the next request,
avoiding an unlink/directory-sync window. If a retry sees this known ID, it
attaches without another POST. A late acknowledgement cannot replace a newer
reservation. These are offline client guarantees, not proof of Wisp's actual
idempotency-key retention or replay behavior. Old/clock-ambiguous requests stay
blocked for manual reconciliation; unsent drafts and cancel intent are not
recovered. Older checkpoint history is not automatically loaded for a restored
unacknowledged request; history/replay presentation remains a separate item.

## Discovery and records

- `GET /v1/runs?sessionId=…&status=queued,running,parked&limit=200` returns an
  **object**: `{runs: [...], nextCursor: string | null}`. It is not a bare list.
- Records have `runId`, `sessionId`, and `status`. Terminal records carry
  `output`, `failure`, `usage`, etc. at the **top level**, not inside `outcome`.
- A parked record may carry `confirmations`, each with `id`, `toolName`, `args`,
  and optional `askedEveryTime`/`escalatedBy` metadata.
- Listing is newest first. Prefer an executing/parked run over newer queued
  ones; otherwise attach to the oldest queued item. Refuse an incomplete page
  or multiple executing records rather than silently enabling fresh input.
- Default listing origin is `v1`. This MVP discovers runs through this interface,
  not every internal/desktop-origin run. No query/route allowlist expansion was
  made for other origins.

## Following and reconnecting

Every SSE event has `seq` and `kind`. Send the last accepted sequence as
`Last-Event-ID`; discard repeated sequences. Unknown kinds are ignored.

- `text.delta` appends text; `gap.text` replaces the accumulated prefix. Do not
  truncate the prefix to a display-label limit.
- The runtime retains a limited event window (documented as 1000 events).
  Replays/gaps can repeat prompts. Only a fresh run-record snapshot opens an
  actionable prompt; do not show a historical approval just because it replays.
- `run.completed`, `run.failed`, and `run.cancelled` carry an `outcome`.
  Late/pre-restart streams are documented to supply a terminal event, but the
  client also reconciles records so it does not depend on that event arriving.
- EOF, timeouts, malformed responses, and connection failures are **not** proof
  of run completion. Keep the active ID/cursor and block duplicate task input.
- One `RunFollower` owns the stream and parked polling. A read timeout with no
  events is *not* a failure (tools can be silent for minutes, and keepalives are
  not documented): it re-reads the run record and reopens from the cursor.
  After five consecutive real failures (unreachable, invalid response,
  unexpected EOF) it stops retrying; `/reconnect` starts a replacement with the retained
  cursor. Reconnection only reads/follows; it never starts another task.
- Poll/retry waits are interruptible. HTTP requests are bounded (record reads:
  5 seconds; SSE socket reads: at most 20 seconds). Socket reads are not instantly
  cancellable. Quitting stops local following, not server-side work.

## Approvals and cancellation

`POST /v1/runs/{runId}/confirmations/{promptId}` sends an **explicit** decision
and `wait: false`. A settled/missing prompt returns 404: read the run again.
Never automatically resend a decision after an error or interpret programmatic
modal dismissal as a denial. Esc remains an explicit deny action.

While parked, read current confirmations even when a local dialog is open.
Another client or the runtime's timeout may settle them. Multiple prompt answers
must not create multiple stream workers. Token capabilities still apply: the
adapter documentation limits a `run` token to approve/deny on its own runs;
project/global rule decisions can be refused. The TUI never escalates privileges
or falls back to another credential.

`POST /v1/sessions/{id}/cancel` affects the current run **and its queued runs**.
A successful cancellation request is not itself terminal confirmation. If the
POST fails, keep the run and allow an explicit retry. During submission, remember
the cancel intent and send it after the run ID is acknowledged; do not claim the
run cannot act before acknowledgement. Previously completed actions are not undone.

## Session metadata display boundaries

The footer uses the session response's `effectiveSelection` for the reported
model/mode and boolean thinking setting, and terminal outcome `usage` fields
`inputTokens`, `outputTokens`, `totalTokens` for the last finished run observed
in this process. Only nonnegative integer counts are displayed. Opening a
different session clears the observed usage; no lifetime totals are inferred.

An automatic mode is not evidence of the concrete model serving a run, and a
thinking setting is not a live reasoning stream. This client has no verified
contract for live token counters, current context occupancy/limit, or the
runtime's working directory: those are not inferred from token sums, model IDs,
or the client's launch directory. Unknown values remain explicitly unavailable.
No routes, telemetry event kinds, or inference requests were added for the footer.

## Tests and remaining validation

`tests/test_runs.py` checks parsing, replay, polling, terminal records, restart
failure, bounded retry, and stop behavior. `tests/test_recovery.py` exercises
reattachment, open-dialog external resolution, multiple prompts, failed approval
POSTs, cancellation, stale callbacks, long gaps, and old request replay refusal.
`tests/test_submissions.py` additionally covers durable-before-POST ordering,
restart identity/options, no automatic replay, lease contention (including a
separate process), failed file/directory syncs, stale acknowledgements, clock
and TTL refusal, endpoint binding, bounded/unsafe/malformed files, and fail-closed
UI behavior. All runtime responses are synthetic. Actual Mac SSH/inference/approval behavior
is still an owner-approved acceptance step in [Road to MVP](ROAD_TO_MVP.md).
