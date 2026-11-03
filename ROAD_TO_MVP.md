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
- Session creation, browsing, and history; Pi-style fresh start per launch
  with locally saved sessions (`-c` continue, `-r`/`/resume` picker).
- Session model/mode picker.
- Explicit interactive approvals and cancellation.
- Inline terminal rendering with the terminal's own background.
- Loopback-only, no-proxy/no-redirect client with a chat-route allowlist.
- Private config/state and scripted tests without network or inference.

These are implemented features, not a claim that every failure/recovery path
is finished. Known-run reattachment covers queued/running/parked v1 runs;
unacknowledged submissions now retain their original identity across process
exit, pending verification against the installed runtime's actual contract.

## 1. Reliable interactions — in progress

### Implemented and regression-tested

- [x] Reserve a submission before starting network I/O; prevent double-send.
- [x] Keep input when no session exists, a session is loading, or a run is busy.
- [x] Clear a submitted draft only after a valid acknowledgement; do not erase
      a newer draft typed while the request was in flight.
- [x] Hold an unacknowledged submission reserved. Explicit `/retry` reuses its
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

**Current limits:** unsent drafts and cancellation intent remain in memory.
Pending request identity is saved privately before POST and restored without
automatic replay. `/retry` deliberately resends the original task, not the
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
- [x] Recover an unacknowledged submission across process exit before allowing
      fresh work. A bounded, owner-checked 0600 journal in the private 0700 state
      directory durably precedes POST; preserve session/input/options/key/age,
      restore without automatic replay, and guard the slot across processes.
      Persist acknowledgements and refuse unsafe/expired/conflicting state.
      Offline restart, lease, sync-failure, TTL/clock and privacy tests; runtime
      exactly-once semantics still require the owner-approved acceptance test.
- [x] Address PR #5 recovery findings: load the restored session's history,
      selection and active-run summary read-only, without inferring an ack or
      attaching to an unidentified run. Defer only explicitly requested retry
      until the view read ends; preserve draft, `/clear` and stale-callback
      guarantees. Release failed preparation's unowned lease while retaining
      owned/post-replacement ambiguous requests. Offline regression tests.
- [ ] Verify the recorded contract against the installed runtime during the
      owner-approved live acceptance test; cached docs are not a live proof.
- [x] Review history/replay presentation when attaching midway through a long
      conversation. Discover the run before reading history; separate replay
      from saved checkpoints and reconcile a successful answer against one
      fresh checkpoint only with visible checkpoint progress. Retain optional
      message IDs without assuming they are run IDs. Preserve genuine repeated
      replies, ambiguous overlap, drafts and `/clear`; bound rebuilt history and
      fence stale/failed refreshes. Offline regression coverage is complete.
      **Remaining contract limit:** unchanged/compacted/lagging checkpoints can
      still overlap labelled replay. Live acceptance must establish checkpoint
      freshness, multi-message output and any reliable run/message association
      before claiming lossless deduplication.

**Exit criterion:** slow requests, disconnects, cancellation, and cross-client
approvals cannot silently lose work or make the UI act on the wrong run/session.

## 2. Essential terminal usability — in progress

- [x] Tighten the single-line input frame and align chat/editor gutters. Render
      answers in the terminal's own foreground, left-align headings, and avoid
      adding message margins inside Markdown. Offline checks cover 40/100-column
      layouts, resize, light/dark themes, streamed/saved answers, and PTY output
      with lists/code/tables and approvals (default foreground/background).
- [x] Distinguish user prompts as full-width blocks filled with muted dark
      teal `#005f5f` + light-grey text (owner-approved exception to the
      unpainted background; palette cyan was too bright), keeping literal
      prompt text. Hide chat scrollbars without disabling scrolling or
      reserving their width. Test long/multiline wrapping, narrow-terminal
      resize, overflowing conversations, and that the `48;5;23` fill is the
      only painted background in PTY output.
- [x] Show approval details, tool names, titles and model IDs exactly as sent
      (no Textual markup: bracketed text was hidden/altered, `[/]` crashed the
      TUI). Regression tests cover approvals, chat lines, status and pickers.
- [x] Treat quiet SSE windows during long tool calls as liveness re-checks, not
      disconnects; only real failures exhaust retries.
- [x] Stream answers incrementally (serialized `Markdown.append`, coalesced
      deltas) instead of re-rendering the whole answer per delta; avoid
      structural pseudo-class selectors that restyle whole answers. ~8k chars:
      97s → ~3s in the headless benchmark. Tests compare streamed vs full render.
- [x] Bound the rendered chat to the newest 60 items (history loads only the
      tail; marker counts hidden items; the streaming answer is never dropped;
      `/clear` mid-answer continues in a fresh widget). 500-exchange session:
      load 22s → ~1s, streaming 2k chars 27s → ~2s (headless benchmark).
- [x] Fresh session per launch (created lazily by the first message, titled
      from it); `/new` is lazy too. Sessions used here are recorded locally
      as private JSONL (prompts, answers, tools, approval decisions, outcomes)
      and drive `-c`/`-r`/`/resume` (`-r ID`/`/resume ID` accept an ID prefix);
      the runtime stays the source of truth.
- [x] On normal exit, print a copyable `<launcher> --resume <full-session-id>`
      command after terminal restoration; use the final active session and
      launcher alias, with no hint for an unused fresh session. CLI and PTY
      tests cover the hint without runtime contact or private config access.
- [x] Finish successful runs silently on the answer, without a completion
      announcement; keep session/model or automatic mode, thinking setting,
      last finished run's reported tokens and client launch directory visible
      independently of run status. Label unavailable context usage and unreported values
      honestly; do not infer context fullness, live reasoning or the runtime's
      working directory. Bounded, transparent footer and `/status` details;
      offline tests cover same-session follow-ups, selection acknowledgements,
      session resets, malformed usage, literal metadata, resize and PTY output.
- [x] Small, transparent “Working” spinner above the input's top-left corner
      during submission and active work. Hide it on terminal outcomes, without
      a completion announcement; distinguish approval waits, disconnects and
      cancellation requests. Offline animation/state/layout and PTY checks.
- [x] Improve composer/footer readability: terminal-palette blue input rules,
      normal-contrast metadata, compact exact token counts, and model/thinking
      aligned on the right. Stack the summary and shorten hints on narrow
      terminals. Hide unreported stats in the footer; retain explanations in
      `/status`. Reserve space for the session ID and thinking setting so long
      paths/model IDs cannot hide them. Offline rendered-line checks at 40/80/100
      columns, theme/resize checks and PTY foreground/background checks.
- [x] Multiline composer: Enter sends the whole prompt; Ctrl+J inserts a newline
      over ordinary SSH, with Shift+Enter only where the terminal distinguishes
      it. Soft-wrap, grow to six text rows then scroll, preserve busy/newer
      drafts, undo/selection, Ctrl+C cancel and UTF-8 size limits. Offline pilot
      and actual PTY-key checks; owner-run Mac/SSH acceptance remains below.
- [x] Bracketed multiline paste is one literal edit, never send keys or a
      sequence of slash commands. Only single-line input dispatches commands
      on explicit Enter. Cover CRLF/CR, Unicode, tabs/blank lines, undo, pending
      retry identity, modals, resize and default-background selections/brackets.
      **Limit:** unbracketed carriage returns cannot be distinguished from Enter.
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
  Current baseline: **364 passing tests**, strict mypy passing.
- Keep this checklist accurate after each implementation slice.
- [x] Local Git repository (`main` baseline, work on branches). No remote yet.
- [x] Reproducible installs: exact pins in `requirements*.txt`, Textual held
      to 8.2.x, drift test, documented upgrade checklist (README).
- [x] Public remote `katamarinaki/wisp-tui`; CI runs pytest + strict mypy on
      GitHub-hosted runners (`main`, `develop`, PRs). Work lands on `develop`.
- [x] `wi update` / `wisp-tui update`: fetch fresh `origin/main`, safely
      switch/fast-forward to `main`, refresh pinned project-venv dependencies.
      Refuse dirty/divergent checkouts and in-progress Git operations; no
      reset/stash, private config/state access, or runtime calls. Offline
      regression tests use temporary local Git repositories and stub pip.

## Explicitly out of scope

Pi connectors, a new execution engine, workspace discovery, attachment systems,
session trees/forks, extensions, themes, advanced usage dashboards, steering,
queue orchestration, and a generic connector framework. Add these only after
an actual need in the single SSH/homeserver workflow is demonstrated.
