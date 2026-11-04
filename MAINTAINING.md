# Maintaining wisp-tui

## Known limits

- Every question you send spends Wisp account balance, the same as a Telegram
  or Desktop session.
- Safe multiline paste needs your terminal's **bracketed paste**, which Textual
  turns on. Without it, a pasted carriage return looks identical to pressing
  Enter, and no timing heuristic can tell them apart. Mac/SSH acceptance
  testing is pending. Draft recovery and prompt recall cover input you typed
  in this TUI. Prompts you sent from other clients don't appear in recall.
- Scripted responses cover recovery in the tests. The owner has not run a
  funded end-to-end SSH task. Discovery covers v1 runs, and the runtime's
  default run listing leaves out internal and Desktop-origin runs.
- A disconnect doesn't cancel a task. Check the run before you assume it
  stopped.
- When a reattached run overlaps saved history in an ambiguous way, you see
  the saved text and a replay of the current run under its own label. The
  runtime contract doesn't map checkpoint messages to run IDs or promise a
  fresh checkpoint at completion. Hiding equal text could drop a real repeated
  reply, so wisp-tui keeps both.
- An unacknowledged submission stays reserved until `/retry` succeeds, HTTP
  failures included. Until then wisp-tui blocks session and model changes,
  even across restarts. It never discards the reservation or replays an aged
  key on its own. If a record expired or can't be reconciled, inspect Wisp
  from another client. `/retry` rechecks a busy journal. If an open client
  detects a conflicting request, quit and restart to restore the original
  record.
- wisp-tui refuses a state directory that other users can read, and leaves its
  permissions alone. Restore owner-only permissions yourself.
- While a run is parked, the follower polls run records instead of holding an
  SSE stream open. Answering the prompt here or in another client resumes
  following. Backoff and poll waits wake at once. An HTTP stream read can block
  for up to 20 s, and Ctrl+C can't cut it short. A quiet 20 s window triggers a
  run-record check and doesn't mean you disconnected.
- `scripts/install` rebuilds the venv from the lock files. It also repairs a
  venv without pip, such as one uv created.

## Upgrading dependencies

`requirements.txt` pins the runtime packages to exact versions, and
`requirements-dev.txt` adds the test and mypy tooling. `pyproject.toml` allows
Textual 8.2.x. Every pin lists the sha256 of each wheel PyPI publishes for it,
and `--only-binary :all:` refuses source builds. `tests/test_dependencies.py`
fails when your venv drifts from the pins or a pin lacks hashes.

Textual carries the most risk: wisp-tui relies on rendering details that
Textual has changed before. To upgrade:

1. Bump the pins, swap in the wheel hashes PyPI lists for the new releases,
   reinstall, and run `pytest` and `mypy`.
2. Read these results first:
   - `test_terminal.py` (PTY): SGR 49 everywhere except prompt blocks
     (`48;5;23`), and answers in the default foreground (SGR 39).
   - `test_rendering.py`: runtime text appears as plain text, with no markup.
     Streamed answers match a full render without per-delta updates.
   - `test_terminal_updates.py` and the quiet-work PTY fixture: the adapter
     relies on Textual's `InlineUpdate`/`App._display` cursor contract.
     Changed-row updates must return to the inline origin without scrolling,
     and resize or height changes must use full updates.
   - `test_layout.py`: gutters and spacing. In 8.2, setting `margin-bottom`
     alone resets the other margins.
3. Launch `wisp-tui` over SSH and look at a long answer and a prompt.
