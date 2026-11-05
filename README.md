# wisp-tui

A terminal chat client for the [Wisp runtime](https://docs.usewisp.io/runtime).

Run `wi` on the machine that runs Wisp, at its keyboard or over SSH, and chat
with your agent. wisp-tui connects to the runtime's local API on `127.0.0.1`
and nothing else.

```
terminal  ──▶ wisp-tui ──▶ Wisp runtime (127.0.0.1) ──▶ its model
```

## What it does

- Streams answers and live tool calls.
- Shows Wisp's approval prompts and waits for your key.
- If you quit or lose SSH, reopen the session and wisp-tui reattaches to the
  runs in progress.
- Browses and resumes Wisp sessions, including ones you started from Telegram
  or Desktop, and picks a model per session.

## Install

You need Python 3.11+ on the machine that runs Wisp.

```sh
git clone https://github.com/katamarinaki/wisp-tui
cd wisp-tui
scripts/install    # venv with hash-checked wheels, links ~/.local/bin/wi
wi setup           # asks for the runtime URL and token, checks them, saves
```

`wi setup` hides the token as you type and writes an owner-only config to
`~/.config/wisp-tui/`. Give it a `run` token, not an admin one. wisp-tui needs
chat routes alone, and a narrow token limits what a leak exposes.

To use another link name, run `scripts/install NAME`. Avoid `wisp`, which is
the runtime's own CLI.

## Usage

```sh
wi                # new session
wi -c             # continue the last session
wi -r [ID]        # pick a saved session, or resume one by ID prefix
wi update         # show new commits on main, switch after you confirm
```

Inside the app: **Enter** sends, **Ctrl+J** adds a newline, **F1** lists keys,
**F2** opens sessions, **F3** picks a model. Commands: `/new`, `/resume`,
`/sessions`, `/model`, `/cancel`, `/retry`, `/reconnect`, `/status`, `/exit`
(or `/quit`); typing `/` lists them and Tab completes. Finished output goes into
the terminal's normal scrollback, so scrolling, selecting and copying work as
in any shell, and the conversation stays there after you quit.

Wisp stores your sessions. wisp-tui keeps a private log under
`~/.local/state/wisp-tui/` so `-c` and `-r` know which ones you used.

## Development

```sh
.venv/bin/pip install -r requirements-dev.txt
git config core.hooksPath .githooks    # ruff + mypy before each commit
.venv/bin/python -m pytest tests/
.venv/bin/python -m mypy
.venv/bin/python -m ruff check src tests && .venv/bin/python -m ruff format src tests
```

Pins carry wheel hashes, and the app depends on Textual 8.2.x rendering
details. Before you bump anything, read
[Maintaining](MAINTAINING.md#upgrading-dependencies). It also lists the known
limits.

For MVP status, see [Road to MVP](ROAD_TO_MVP.md). The
[runtime contract notes](RUNTIME_CONTRACT.md) cover the API the client relies
on.
