# Runtime contract used by wisp-tui

This is a narrow implementation note, not a new API or permission model.
It records the relevant shapes from locally cached Wisp documentation and the
OpenAPI document inspected during development. No live runtime requests or
inference were used to establish these notes. Recheck against the installed
release before the owner-run live acceptance test.

## Compatibility

| Runtime | API version | Basis | Result |
|---|---|---|---|
| 0.1.8 | 2 | Cached docs and OpenAPI, a static inspection of the installed executable, and the 2026-11-05 live acceptance run | Supported |

Earlier notes cited temporary files by hash. Those files are gone, so the
hashes can't be checked. This document and the synthetic regression fixtures
are the durable record. After testing a new runtime release, add a row and
note what changed below.

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
blocked for manual reconciliation; cancel intent is not
recovered.

Unsent editor text is saved separately from this request journal. Composer
snapshots can tag an unchanged draft with its in-flight request ID; startup
discards that snapshot only when the validated journal acknowledges the same
request, session and text. Editing clears the tag. A newer cached draft never
changes the original request used by `/retry`, and recall/recovery adds no
runtime requests or inferred acknowledgements.

A restored request now loads its original session, checkpoint history and v1
active-run summary using bounded GETs only. This view supplies the title and
model/mode before retry, and retains them afterward. Neither matching history
nor a listed run is evidence that this particular request was acknowledged:
no automatic POST, SSE attachment, confirmation or reservation release follows.
An explicit `/retry` received during the read waits for completion and then
reuses the same identity/options, even if the read failed. Duplicate retry
keypresses do not create multiple submissions. Pending identity, session and
operation token fence callbacks; a newer view invalidates history restoration
but allows metadata to update, and exit cancels any deferred retry. A successful
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
- Default listing origin is `v1`. wisp-tui discovers runs through this interface,
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
explicitly separated: the checkpoint is printed first, then the replay below a
"Current run replay — may overlap the saved checkpoint." label. Finished output
is never rewritten, so a successful
terminal outcome no longer triggers a checkpoint re-read to replace the replay
(the earlier coverage heuristic was removed with the in-app transcript). The
overlap therefore always stays visible and labelled; nothing is removed because
texts match. Failed/cancelled runs keep their error/cancel lines and any partial
replay.

Known limit: a streamed answer prints block by block. If a gap snapshot or the
final `output.text` does not extend the blocks already printed for the current
segment (for example, it rewords an earlier paragraph), the whole new text is
printed below the earlier blocks, so both versions stay visible. Whether the
runtime's final text can differ from its deltas this way is part of the live
acceptance test.

The 2026-11-05 acceptance run established live↔stored tool-call ID identity
and checkpoint commit timing (the final checkpoint can land just after
`run.completed`; the client retries around this). Message-ID stability across
multi-message checkpoints and a reliable run/checkpoint association remain
unverified, so ambiguous overlap is intentionally visible rather than silently
discarded.

## Tool detail contract — installed 0.1.8

The owner-provided static report establishes these surfaces:

- `tool_call.started`: `name`, optional `toolCallId`; no arguments or results.
- `tool_call`: `name`, sanitized `args`, `ok`, `durationMs`, optional `toolCallId`,
  and optional `ruledBy`. No result/output fields. `ok` describes the registry
  success/error hook, not every tool's domain-specific result semantics.
- Terminal `outcome.toolCalls` and run-record `toolCalls`: the same summaries in
  completion order, explicitly **without call IDs or results**. Never correlate
  these records with history by names, arguments or ordering. The UI only uses
  them as labelled run-summary fallback when no live tool events were observed,
  preventing a second copy of the live trace.
- `/v1/sessions/{id}/history` returns flattened LangChain dictionaries under
  `values.messages`: AI `tool_calls[].id/name/args` and tool-result
  `tool_call_id/name/content/status`. This is distinct from the camelCase
  representation in the session-detail endpoint. Join only by the call ID.

Evidence anchors in the supplied report: `src/common/effect/outcome.ts:8–23,54–74`,
`src/kernel/turn/run-turn.ts:246–265`, `src/server/routes/run-events.ts:66–72`,
`src/kernel/turn/outcome.ts:61–92`, `src/server/routes/sessions.ts:137–179`, and
`src/kernel/tools-adapter.ts:55–65`. These are generated module-section line
numbers in the installed executable, not original TypeScript line numbers.
This is static package evidence, not a live trace or a universal ID guarantee.

Live arguments may already be truncated, redacted, parsed or changed after
approval. History arguments are the model request and can differ from actual
execution inputs. A tool entry's title comes from the live arguments when the
runtime sent them, else from history; the full input is not shown. The printed
tail is the stored result as returned, which may be a summary
(`additional_kwargs.wisp_tool_output_pruned`, not shown) or otherwise limited
or transformed by tools/connectors. JSON strings remain strings, and
content-block arrays are shown literally within the normal display limits.
Registry `ok` decides the entry's ok/failed state; stored tool-message `status`
is used only when no live `ok` was seen.

A live completion of an identified entry performs one bounded (5s) GET through
the existing history allowlist. Concurrent requests are coalesced. While the
run is active, a result not stored yet is read once more after 1 s, and a miss
is never final while the run is live. Terminal completion re-reads every entry
still waiting, and bounded post-run retries (1 s, 3 s, 6 s) cover a final
checkpoint that commits just after `run.completed`; only then is a result
reported missing. This does not reserve the submission slot or block typing,
cancellation or fresh work. A tool entry (and everything after it) stays in the
live area until its reads finish, then is printed once with the output tail or
a safe error code; there is no expansion or manual retry.
Session/chat generation, request identity and exact target entries fence
replies after switching or exit. No automatic POST, tool execution or approval
is added.

Only uniquely matched call IDs fill outputs; conflicting/reused IDs remain
ambiguous. An unidentified run summary points to reopening stored session
history instead of guessing which result belongs to it. Missing/late history
results remain explicit. Checkpoint-only calls are labelled recorded rather
than shown as live work. Nested/subagent coverage is limited by what the parent
history exposes and by the runtime's optional IDs.

Final `output.text` normally selects the last assistant message; it is not the
concatenation of all `text.delta` rounds. A supplied final string, including an
empty string, replaces the streamed aggregate. The stream ends at its terminal
event; later tool completions for that same run are not emitted. The earlier
“logs after the answer” symptom was a UI insertion-order issue, not evidence of
post-terminal tool execution.

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
contract for live token counters or current context occupancy/limit: those are
not inferred from token sums or model IDs. Unknown stats are omitted from the
compact footer and explained as unavailable in `/status`.
No routes, telemetry event kinds, or inference requests were added for the footer.

## Project sessions

Source: docs.usewisp.io (`/adapter-contract`, "Naming the Project again";
`/reference`), read 2026-11-10; not yet checked against `/v1/openapi.json` or a
live runtime.

- `POST /v1/sessions` takes `projectRoot`, the folder the session works in for
  its whole life. Wisp's file tools work there; a call outside it asks first.
  Without it the session is a Desk Session.
- The runtime does not remember a Project Session's folder once it drops the
  session from memory (e.g. after a restart). Then `PATCH /v1/sessions/{id}`,
  `POST /v1/sessions/{id}/runs` and `POST /v1/sessions/{id}/cancel` answer 404
  unless the query names it again: `?projectRoot=<folder>`. A wrong folder gets
  the same 404. `GET /v1/sessions/{id}` and `/history` still answer without it;
  the session's `projectRoot` is then `null`.

wisp-tui, like Pi and the Wisp CLI, creates sessions with the realpath of the
launch folder, except the home folder, `/`, and any folder that contains or
sits inside the client's config or state folders (Desk Session instead). A 400
for the folder retries once as a Desk Session and says so; the message or
command that started the session is not sent until it is repeated, and later
sessions of that launch are Desk Sessions. Each session's folder is
saved in its local record header and in the pending-request journal (version 2;
version 1 records load as Desk), and sent as `?projectRoot=` on the three routes
above. A resumed session uses its own saved folder, or the one `GET` reports,
never the current launch folder. A Project Session created elsewhere whose
folder is unknown after a restart can be read, but not changed or sent to.
The allowlist accepts only a single absolute, printable `projectRoot` query
on the session, runs and cancel routes, and no query on other non-`/v1/runs` routes.

## Tests and remaining validation

`tests/test_runs.py` checks parsing, replay, polling, terminal records, restart
failure, bounded retry, and stop behavior. `tests/test_recovery.py` exercises
reattachment, open-dialog external resolution, multiple prompts, failed approval
POSTs, cancellation, stale callbacks, long gaps, and old request replay refusal.
`tests/test_submissions.py` additionally covers durable-before-POST ordering,
restart identity/options, no automatic replay, lease contention (including a
separate process), failed file/directory syncs, stale acknowledgements, clock
and TTL refusal, endpoint binding, bounded/unsafe/malformed files, and fail-closed
UI behavior. `tests/test_history_replay.py` covers message identity, labelled
replay overlap, replay/gaps across reconnect and bounded long-history output.
`tests/test_pending_view.py` checks read-only recovery, model/history display,
explicit/deferred retry, stale/error paths and no replayed approvals;
`tests/test_submission_leases.py` checks unowned failure/conflict cleanup and
retention of owned or post-replacement ambiguous requests.
All runtime responses are synthetic. The 2026-11-05 owner acceptance run
additionally exercised the live SSH workflow against runtime 0.1.8
(apiVersion 2) with Textual 8.2.8: launch, multiline submission, streamed
text/tool progress, a live approval, cancellation, disconnect/reconnect
recovery, a cross-client parked approval, and exit restoration. The **deny**
decision was not exercisable live (the runtime's permission rules auto-decide
the tested action classes, so no prompt was available to deny); it shares the
approve path and is covered by the offline regression tests.
`scripts/tool_ids.py` is the read-only diagnostic that verified live↔stored
tool-call ID identity; run it with the project venv (`--list`, a session ID,
or `--watch` to follow the next live run).
