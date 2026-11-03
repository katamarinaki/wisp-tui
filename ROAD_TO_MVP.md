# Road to MVP

## One use case

SSH from a Mac into the homeserver, launch `wisp-tui`, ask Wisp to operate the
homeserver, watch progress, answer explicit approvals, and return to the work
later. This is a terminal front end for the existing Wisp runtime, not a new
agent harness or a general-purpose Pi replacement.

```text
Mac terminal → SSH → wisp-tui on homeserver → local Wisp runtime
```

Wisp owns inference, tools, execution, instructions, and permission decisions.
The TUI owns input, display, interaction, and reliable attachment to runtime
sessions/runs. Its local HTTP runtime endpoints are an integration dependency;
do not assume they are a stable public API or invent unsupported capabilities.

## Already available

- Direct runtime chat with streamed Markdown and basic tool progress.
- Session creation, browsing, history, and last-session restore.
- Session model/mode picker.
- Explicit interactive approvals and cancellation.
- Inline terminal rendering with the terminal's own background.
- Loopback-only, no-proxy/no-redirect client with a chat-route allowlist.
- Private config/state and scripted tests without network or inference.

These are implemented features, not a claim that every failure/recovery path
is finished. Known-run reattachment now covers queued/running/parked v1 runs;
unacknowledged submissions still need cross-process recovery.

## 1. Reliable interactions — in progress

### Implemented and regression-tested

- [x] Reserve a submission before starting network I/O; prevent double-send.
- [x] Keep input when no session exists, a session is loading, or a run is busy.
- [x] Clear a submitted draft only after a valid acknowledgement; do not erase
      a newer draft typed while the request was in flight.
- [x] Hold an unacknowledged submission in memory. Explicit `/retry` reuses its
      original session, input, options, and idempotency key; never auto-retry
      a POST with a new key. Block session/model changes until acknowledged.
- [x] Serialize session loads/creation/model updates; release reservations on
      errors. Ignore model-picker responses for a different current session.
- [x] Prevent a slow startup health check from replacing a user-selected session.
- [x] Display validated run failure codes, not arbitrary runtime failure text
      or health-status content.
- [x] Bound SSE line reads and frame accumulation incrementally in bytes.
- [x] Share the scripted client in `tests/helpers.py`; exercise delayed requests,
      failures, malformed replies, retry identity, and stale callbacks.
- [x] Automated PTY check for inline rendering/default background, including a
      scripted transcript and approval dialog (no runtime contact).

**Current limits:** drafts and pending request identity are in memory only.
Quitting loses them. `/retry` deliberately resends the original task, not the
current editor contents. Exactly-once behavior depends on Wisp's idempotency
contract; client-side request-identity tests alone do not prove server behavior.
Retries older than 23 hours are refused (the documented default key TTL is 24
hours, but the runtime can be configured differently). See
[Runtime contract](RUNTIME_CONTRACT.md) for evidence and limitations.
An unacknowledged request stays reserved even after an HTTP error; this is
conservative until rejection/reconciliation semantics are established.

### Run recovery and approvals — implemented offline

- [x] Check cached runtime documentation/OpenAPI for run-status, replay,
      idempotency, and confirmations; record shapes in `RUNTIME_CONTRACT.md`.
      In particular, fix discovery to read the `{runs, nextCursor}` envelope.
- [x] Keep run identity/cursor when a stream fails. Reconnect automatically with
      bounded retries; `/reconnect` retries explicitly without submitting work.
- [x] Reconcile server status and reattach to queued/running/parked v1 runs when
      reopening a session. Terminal records finish the UI even without SSE.
- [x] One follower owns SSE and parked polling; ignore stale callbacks and
      duplicate events/prompts. Interruptible poll/backoff waits, bounded HTTP
      reads (socket reads are not instantly cancellable).
- [x] Reconcile approvals answered elsewhere while the TUI dialog is open;
      dismiss stale prompts without posting a decision. No automatic decision
      replay on 404/timeouts; fetch current prompts before offering another try.
- [x] Handle cancellation failures/retries and cancellation during submission
      explicitly. Do not claim the task stopped until the runtime confirms it.
- [x] Preserve long gap text and avoid pre-mount Markdown updates losing deltas.
- [x] Regression tests for multiple prompts, timeouts, restart failure records,
      missing terminal events, stale callbacks, cancellation, and reattachment.

### Remaining reliability work
- [ ] Recover an unacknowledged submission across process exit, or reconcile it
      safely before allowing a fresh task. Any persisted state must use the
      existing private state directory, never the project tree.
- [ ] Verify the recorded contract against the installed runtime during the
      owner-approved live acceptance test; cached docs are not a live proof.
- [ ] Review history/replay presentation when attaching midway through a long
      conversation, including output already present in saved checkpoints.

**Exit criterion:** slow requests, disconnects, cancellation, and cross-client
approvals cannot silently lose work or make the UI act on the wrong run/session.

## 2. Essential terminal usability — in progress

- [x] Tighten the single-line input frame and align chat/editor gutters. Render
      answers in the terminal's own foreground, left-align headings, and avoid
      adding message margins inside Markdown. Offline checks cover 40/100-column
      layouts, resize, light/dark themes, streamed/saved answers, and PTY output
      with lists/code/tables and approvals (default foreground/background).
- [x] Distinguish user prompts as full-width blocks filled with palette cyan
      (SGR 46, owner-approved exception to the unpainted background), keeping
      literal prompt text. Hide chat scrollbars without disabling scrolling or
      reserving their width. Test long/multiline wrapping, narrow-terminal
      resize, overflowing conversations, and the SGR 46 fill in PTY output.
- [x] Show approval details, tool names, titles and model IDs exactly as sent
      (no Textual markup: bracketed text was hidden/altered, `[/]` crashed the
      TUI). Regression tests cover approvals, chat lines, status and pickers.
- [x] Treat quiet SSE windows during long tool calls as liveness re-checks, not
      disconnects; only real failures exhaust retries.
- [x] Stream answers incrementally (serialized `Markdown.append`, coalesced
      deltas) instead of re-rendering the whole answer per delta; avoid
      structural pseudo-class selectors that restyle whole answers. ~8k chars:
      97s → ~3s in the headless benchmark. Tests compare streamed vs full render.
- [ ] Multiline prompt editor with predictable send/newline keys over Mac SSH.
- [ ] Pasting multiple lines does not accidentally submit partial commands.
- [ ] Minimal input history/draft recovery; no elaborate editor framework.
- [ ] Show useful tool arguments/results where the runtime actually supplies
      them. Keep long output collapsible/bounded and approval details legible.
- [ ] Stop auto-follow when the user scrolls up; offer an obvious way to resume.
- [ ] Check long streamed output, narrow terminals, resize, and modal key handling.
      Preserve the default-background and inline-mode guarantees.

**Exit criterion:** composing a real homeserver task and inspecting Wisp's work
is comfortable over SSH. Keep the existing pickers and commands unless a
specific usability problem requires a change.

## 3. Prove the actual workflow — pending

First run the full offline suite and PTY checks. Then agree on a harmless live
smoke task with the owner; tests must never invoke inference or real services.

- [ ] SSH in from the Mac and launch from an ordinary shell prompt.
- [ ] Create/resume a conversation and send a multiline homeserver question.
- [ ] Observe streamed text and tool progress with the expected model.
- [ ] Exercise approve/deny and cancellation using an explicitly agreed safe
      action. Do not restart services just to test the UI.
- [ ] Disconnect while work is active; SSH back in, launch, and recover it.
- [ ] Resolve a parked approval through another client and verify TUI recovery.
- [ ] Verify terminal scrollback, background, and shell restoration on exit.
- [ ] Record runtime/Textual versions and results, not transcripts or private
      config. Document remaining limitations in `README.md`.

**MVP done:** this exact SSH workflow works reliably. Feature parity with Pi is
not a release criterion.

## Maintenance alongside the MVP

- Keep `.venv/bin/python -m pytest tests/` and `.venv/bin/python -m mypy` green.
  Current baseline: **132 passing tests**, strict mypy passing.
- Keep this checklist accurate after each implementation slice.
- Establish source control/CI and reproducible dependency installation before
  treating the directory as a maintained release. This checkout currently has
  no Git metadata; no repository or remote has been created as part of this work.

## Explicitly out of scope

Pi connectors, a new execution engine, workspace discovery, attachment systems,
session trees/forks, extensions, themes, advanced usage dashboards, steering,
queue orchestration, and a generic connector framework. Add these only after
an actual need in the single SSH/homeserver workflow is demonstrated.
