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
Preparation errors release a lease if this store has not taken ownership of a
pending request. Ownership is established by loading/matching the pending
record or replacing the journal with the new request, before directory fsync.
Thus a pre-replacement failure/conflict does not monopolize an unowned slot,
while a directory-sync failure after replacement retains the ambiguous request
and its lease. Owned leases last until acknowledgement or client exit.

A valid run acknowledgement is saved with the original identity before the UI
releases its reservation. The acknowledged record stays until the next request,
avoiding an unlink/directory-sync window. If a retry sees this known ID, it
attaches without another POST. A late acknowledgement cannot replace a newer
reservation. These are offline client guarantees, not proof of Wisp's actual
idempotency-key retention or replay behavior. Old/clock-ambiguous requests stay
blocked for manual reconciliation; unsent drafts and cancel intent are not
recovered.

A restored request now loads its original session, checkpoint history and v1
active-run summary using bounded GETs only. This view supplies the title and
model/mode before retry, and retains them afterward. Neither matching history
nor a listed run is evidence that this particular request was acknowledged:
no automatic POST, SSE attachment, confirmation or reservation release follows.
An explicit `/retry` received during the read waits for completion and then
reuses the same identity/options, even if the read failed. Duplicate retry
keypresses do not create multiple submissions. Pending identity, session and
operation token fence callbacks; `/clear` invalidates history restoration but
allows metadata to update, and exit cancels any deferred retry. A successful
read also supplies the saved checkpoint baseline for later replay presentation.

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

## Checkpoint history and replay presentation

The history parser retains optional LangGraph message `id` strings, alongside
role/text. This is message identity between checkpoints, **not** a verified
mapping to the v1 run ID. The TUI does not invent such a mapping from text,
message position, model choice, or timestamps.

Discovery now precedes the history read. A run discovered live remains attached
by ID even if it finishes while history loads. The two reads are not an atomic
snapshot: a run that both starts and finishes outside that discovery window
still requires runtime guarantees/live acceptance to reason about fully.

When attaching to a known run, the saved checkpoint and streamed replay are
explicitly separated. SSE cursor/gap handling remains run-scoped. After a
successful terminal outcome, one additional history GET (5s timeout) reconciles
the display only when a progressed checkpoint contains the exact finished
answer as its trailing assistant message:

- The checkpoint appends messages without rewriting the prior snapshot; or
- The trailing assistant has the same nonempty message ID, changes its text to
  the finished answer, and the earlier messages are unchanged.

An unchanged checkpoint, missing/changing IDs on an updated message, compaction,
rewritten earlier messages, a lagging partial answer, or an answer split across
multiple checkpoint messages cannot prove coverage. Keep the saved snapshot
and labelled run output in these cases. In particular, never remove a previous
“OK” solely because a new run also replies “OK”. Genuine identical replies
remain separate when the checkpoint includes both turns. This is conservative
presentation reconciliation, not a claim of lossless cross-client/run-ID merge.

The refresh uses the existing history route, never submits work, and blocks new
submission/session changes while retaining drafts. Session/run identity, an
operation token and chat generation fence every callback. `/clear` invalidates
pending refreshes and cancels reconciliation for an active replay; failed reads
preserve the terminal result and visible answer. Rebuilding the view retains
the normal bounded chat tail and does not write the assistant answer twice to
the local session record. Failed/cancelled runs retain their error/cancel lines
and any partial replay; they do not trigger this successful-answer refresh.

The live acceptance test must establish message-ID stability, how the final
run output relates to multi-message checkpoints, checkpoint commit timing, and
whether the runtime exposes a reliable run/checkpoint association. Until then,
ambiguous overlap is intentionally visible rather than silently discarded.

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
UI behavior. `tests/test_history_replay.py` covers checkpoint progress and
identity, genuine repeated replies, ambiguous overlap, terminal-record/stream
completion, replay/gaps across reconnect, stale callbacks, failed refreshes,
draft retention, `/clear`, local recording and bounded long-history rendering.
`tests/test_pending_view.py` checks read-only recovery, model/history display,
explicit/deferred retry, stale/error/clear paths and no replayed approvals;
`tests/test_submission_leases.py` checks unowned failure/conflict cleanup and
retention of owned or post-replacement ambiguous requests.
All runtime responses are synthetic. Actual Mac SSH/inference/approval behavior
is still an owner-approved acceptance step in [Road to MVP](ROAD_TO_MVP.md).
