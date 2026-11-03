# wisp-tui

A terminal chat client (Pi-style TUI) for the **Wisp runtime** on this
homeserver. Your messages go to Wisp on loopback and its answers render
locally — **no other LLM or service is in the path**. Streaming text, live
tool calls, session browsing and interactive approval prompts are supported.

The MVP targets one workflow: SSH from a Mac into the homeserver and use Wisp
to operate it. See [Road to MVP](ROAD_TO_MVP.md) for progress, remaining work,
and acceptance checks—not a feature-parity roadmap for Pi.

```
Terminal (SSH) ← TUI ←→ Wisp runtime (127.0.0.1:3008) ←→ its inference
```

## Running

```bash
wisp-tui                 # new session (from anywhere, via the launcher symlink)
scripts/wisp-tui         # same, from the checkout
wisp-tui -c              # --continue: the most recent locally saved session
wisp-tui -r              # --resume: pick a locally saved session
wisp-tui -r 3bf1ba87     # resume the saved session whose ID starts with this
wisp-tui --session UUID  # open a specific Wisp session
wisp-tui --check         # authenticated health check, no TUI, no inference
```

Like Pi, every launch starts a **new session**. Nothing is created on the
runtime until you send the first message; its first line becomes the session
title (`/new Title` sets one explicitly).

Shorter command: symlink the launcher under any name, e.g.
`ln -s "$PWD/scripts/wisp-tui" ~/.local/bin/wi`; help and errors then use
that name. Avoid `wisp`: that is the Wisp runtime's own CLI.

`--resume ID` (and `/resume ID` inside the app) takes a full session ID or a
prefix of 4+ characters, such as the 8 shown in the pickers. One match opens
directly; several show a picker of just those; a full ID that is not saved
locally still opens the Wisp session.

First-time setup: `scripts/setup` copies the credential from the ask-wisp
config into `~/.config/wisp-tui/config.json` (0600). Both tools currently
share the `pi-ask-wisp` run token — revoking it breaks both (see
wisp-homeserver-report README for revocation). A dedicated token can be minted
later with the Wisp CLI if desired.

## Features

- **Saved sessions (Pi-style).** Every session you use here is recorded
  locally as JSONL: `~/.local/state/wisp-tui/sessions/<time>_<id>.jsonl`
  (0600 file, 0700 dir, owner-checked, no symlinks, 16 MiB cap per file). It
  holds your prompts, final answers, tool results, approval decisions (with
  the tool and arguments you approved) and run outcomes — not every streamed
  delta. `-c`/`-r`/`/resume` use these records; the conversation itself is
  still loaded from the Wisp runtime, which remains the source of truth (if a
  session was deleted there, the TUI says so and shows the local file path).
  Unlike Pi, sessions are not grouped by directory: Wisp does not run in your
  working directory.
- All Wisp sessions (F2 / `/sessions`) across origins (cli/telegram/desktop),
  resume with history.
- Pi-style chrome: no header/footer bars — just the chat, a text input
  with tight, thin top/bottom rules, and a transparent contextual line under it
  (state + `F1 help · F2 sessions · F3 model · Ctrl+C cancel`). Chat and input
  share a one-column gutter; answers use your terminal's default text color
  rather than a fixed dark/light theme foreground. Your prompts appear as
  full-width blocks in a muted dark teal (`#005f5f`) with light-grey text,
  including in history. Chat scrollbars are hidden without disabling scrolling.
- Long sessions stay fast: only the newest 60 chat items are kept on screen
  (opening a session mounts just that tail); a dim marker at the top counts
  what is hidden. Wisp keeps the full history — older turns are still in the
  session context, just not rendered here.
- New sessions (`/new [title]`), model/mode picker (F3) per session (F3 on a
  fresh session creates it first).
- Runs submitted with `confirmationMode: "interactive"`, `wait: false`, then
  followed over SSE (`GET /v1/runs/{id}/events`): `text.delta` streaming,
  `tool_call` lines, `gap` recovery (server text wins), `Last-Event-ID` resume
  after timeouts and after answering a parked prompt.
- Approval dialog for `confirmation.required`: **y** approve, **n/Esc** deny,
  **p** allow for project, **g** allow globally. Answers are sent to
  `POST /v1/runs/{id}/confirmations/{promptId}` and the stream resumes.
- One follower owns each run's stream and parked polling. Current run records
  reconcile approvals answered elsewhere—even with a local dialog open—and
  completion after a lost terminal event or runtime restart.
- Reopening a session discovers queued/running/parked v1 runs and reattaches.
  Stream failure retains the run ID/cursor and blocks duplicate task input;
  after bounded automatic retries, `/reconnect` retries following without
  starting another task.
- Ctrl+C cancels the active run; press it **twice** (within 3s) to leave.
  `/cancel` cancels without exiting, `/quit` always exits. Cancellation failures
  can be explicitly retried; cancellation during submission waits for the run
  ID to be acknowledged. The runtime must confirm the terminal status.
  Quitting leaves server-side runs running (re-open the session to reattach).
  Runtime cancellation affects the current run and its queued work.
- Commands: `/help /new /resume /sessions /model /cancel /retry /reconnect /status /clear /quit`.
- Submission is reserved before the request starts: double-send and concurrent
  session/model changes are blocked. Input rejected while busy is kept. The
  acknowledged draft is cleared only if you have not edited it in the meantime.
- If submission is not acknowledged, `/retry` resends the **original task** with
  the same request ID and options, not your newer draft. There is no automatic
  POST retry. Pending submissions and drafts are in memory only; quitting loses
  them. Retries older than 23 hours are refused. Server-side deduplication and
  key expiry depend on the runtime's idempotency contract; see
  [runtime contract notes](RUNTIME_CONTRACT.md).
- **Inline mode**: no alternate screen — the app renders under your prompt and
  the transcript stays in the terminal scrollback after you quit, like Pi.
- **True terminal background**: apart from prompt blocks (below), the app never
  paints a background. This needs
  two things together (regressing either reintroduces a painted background):
  `App { background: ansi_default; }` + `Screen { background: transparent; }`
  in the CSS, and `super().__init__(ansi_color=True)` so Textual's
  ANSI→truecolor filter doesn't rewrite "default" into a hardcoded theme
  color. The result is SGR 49 (terminal default background) on every cell —
  the user's terminal background, background images included.
  **Sole exception (owner-requested):** user prompt blocks are filled with a
  muted dark teal, `#005f5f` (exactly xterm-256 #23: `48;5;23`, or
  `48;2;0;95;95` on truecolor terminals), with fixed light-grey text so it
  reads on light and dark themes. Palette cyan (SGR 46) was too bright.

## Security model

Mirrors the ask-wisp proxy hardening:

- Runtime URL must be **explicit loopback http**; no redirects, no env
  proxies, no credentials in URLs; token validated by shape.
- **Client-side route allowlist**: only chat routes (health, models, sessions
  CRUD-lite, history, runs, events, confirmations, cancel). Billing, tokens,
  updates, schedules, connectors, permission rules, sandbox, instructions,
  auth and attestation-refresh routes are never issued. This is a scope guard
  for this tool, **not** a server-side restriction on the credential — the
  underlying run token can read other sessions and more (see the
  wisp-homeserver-report README). Keep the config private.
- Config `~/.config/wisp-tui/config.json` and state
  `~/.local/state/wisp-tui/` are 0600/0700, owner-checked on every read
  (O_NOFOLLOW, single link, size-bounded). They never live in the
  group-readable project tree.
- Bounded JSON responses and SSE frames; error diagnostics carry safe code
  strings only — no raw bodies, headers or tokens.
- Interactive approvals are the *only* privileged action, and they are always
  explicit per prompt (no auto-approve, no bypass toggles from the TUI).

## Verification

```sh
cd wisp-tui   # the checkout
.venv/bin/python -m pytest tests/    # 184 tests, no network/sockets
.venv/bin/python -m mypy             # strict, src + tests
```

Runtime dependency: `textual` (project venv `.venv/`, nothing installed
system-wide). Tests use a scripted client; no real inference is ever started
by the test suite. A scripted subprocess/PTY test checks inline rendering at
40 and 100 columns, including Markdown lists/code/tables and an approval
dialog: the dark-teal prompt fill (`48;5;23`) must be the only painted
background, and answers must use the terminal-default foreground and
background (SGR 39/49). It does not load private config or contact Wisp. Live smoke: `wisp-tui --check` (no inference).

## Layout

```
src/wisp_tui/config.py   private config loading (modeled on ask-wisp)
src/wisp_tui/sessions.py local session records (JSONL), continue/resume
src/wisp_tui/client.py   loopback HTTP client, route allowlist, SSE parser
src/wisp_tui/history.py  LangGraph checkpoint history → chat messages
src/wisp_tui/runs.py     run-record validation, single-owner following/recovery
src/wisp_tui/app.py      Textual application, workers, approval modal
src/wisp_tui/__main__.py CLI entry point (--check, --session)
scripts/wisp-tui         launcher (project venv)
scripts/setup            first-time config copy from ask-wisp
tests/                   pytest suite incl. Textual pilot app tests
```

## Notes and limits

- Normal questions consume the Wisp account balance, exactly like
  Telegram/Desk sessions.
- Input is single-line for now; multiline/paste handling is an MVP next step.
- Recovery is tested with scripted responses, not yet a funded end-to-end SSH
  task. Discovery covers v1 runs; internal/desktop-origin runs are not included
  by the runtime's default run listing. Do not assume a disconnect cancelled
  a task. Reattaching may repeat text already present in saved history.
- Unacknowledged submissions stay reserved until `/retry` succeeds (including
  HTTP failures); session/model changes are blocked meanwhile. Cross-restart
  reconciliation of these requests is still planned.
- While parked, the follower polls current run records rather than holding an
  SSE stream open. Answering here or elsewhere resumes following. Backoff/poll
  waits wake immediately; an outstanding HTTP stream read is bounded to 20s
  (a quiet 20s window just re-checks the run record; it is not a disconnect),
  not instantly interruptible.
- The venv is regenerable from the lock files (Python 3.13):
  `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
  Source and config are within existing restic backup scope (projects on
  NVMe; config/state in your home directory).

## Upgrading dependencies

Versions are pinned exactly in `requirements.txt` (runtime) and
`requirements-dev.txt` (tests/mypy); `pyproject.toml` only allows Textual
8.2.x. `tests/test_dependencies.py` fails if the venv drifts from the pins.

Textual is the risky one: this app relies on rendering details that have
changed behavior before. To upgrade:

1. Bump the pins, reinstall, and run the full suite (`pytest` + `mypy`).
2. Watch especially:
   - `test_terminal.py` (PTY): still SGR 49 everywhere except prompt blocks
     (`48;5;23` only), answers in default foreground (SGR 39).
   - `test_rendering.py`: runtime text still shown literally (no markup);
     streamed answers still match a full render without per-delta updates.
   - `test_layout.py`: gutters/spacing (e.g. `margin-bottom` alone resets
     the other margins in 8.2).
3. Launch `wisp-tui` once over SSH and eyeball a long answer and a prompt.
