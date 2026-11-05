# wisp-tui conventions

Terminal chat client for the local Wisp runtime. Python 3.11+, stdlib + textual
(project venv only — never install system-wide).

## Scope and plan

Read [Road to MVP](ROAD_TO_MVP.md) before feature work and update its checklist
as work lands. The only target workflow is Mac → SSH → homeserver → Wisp:
reliable task submission, progress, explicit approvals, cancellation, and
reattachment. Do not expand this into a general-purpose Pi replacement.

## Git workflow

- Repo: `github.com/katamarinaki/wisp-tui` (**public**). Do not commit or push
  unless the user explicitly requests that action. Requests to implement, fix,
  or update something do not authorize commits or pushes. A request to commit
  does not authorize pushing. When explicitly requested, commit and push only
  to `develop`; no PRs for routine work. The owner merges `develop` → `main`.
  Never push to or merge into `main` yourself.
- CI (`.github/workflows/ci.yml`) runs pytest + strict mypy on pushes to
  `main`/`develop` and on PRs, on GitHub-hosted runners only. Never use the
  homeserver's self-hosted runner here: fork PRs would execute code on it.
- Because the repo is public, keep infrastructure identifiers out of code,
  tests and docs: no real IPs/hostnames/usernames or private ops notes. Use
  documentation addresses (e.g. `192.0.2.10`, `100.64.0.1`) in tests.

## Rules

- **Never** put credentials, tokens or the config contents in code, tests,
  docs, logs or chat output. Config/state live only under `~/.config/wisp-tui`
  and `~/.local/state/wisp-tui` (0600/0700). Local session records
  (`state/sessions/*.jsonl`) contain conversation content: keep them there,
  bounded and owner-checked; tests must use the temp state dir
  (`tests/conftest.py`), never the real one.
- The runtime URL is always explicit loopback http; keep the no-redirect /
  no-env-proxy client behavior.
- The client-side route allowlist in `src/wisp_tui/client.py` is deliberate:
  chat routes only. Extending it (e.g. DELETE sessions) needs a conscious
  decision — billing/tokens/update/connector/permission routes stay out.
- Interactive approval is the only privileged action; it must stay explicit
  per prompt. No auto-approval paths.
- The terminal background must stay unpainted: `App { background: ansi_default; }` +
  `Screen { background: transparent; }` in `src/wisp_tui/app.tcss` and
  `ansi_color=True` in `AppBase.__init__` (`app_base.py`) work together to emit SGR 49. Changing any of these
  (or re-enabling the ANSI→truecolor filter) paints a hardcoded background.
  Verify with a pty capture that no other `48;...` SGR is emitted.
  Sole owner-approved exception: user prompt blocks (`USER_STYLE` in
  `src/wisp_tui/scrollback.py`) are filled with muted dark teal (xterm-256
  #23, `48;5;23`) with fixed light-grey text. Palette cyan was rejected as too
  bright. Don't change the shade or extend fills to other elements without
  asking.
- Finished chat items are printed once into the terminal's own scrollback
  (`scrollback.py`, printed by `ChangedRowsUpdate` in `terminal.py`); only live
  items stay in the app. The app runs with `mouse=False` so the terminal keeps
  native scrolling, selection and copy. Don't reintroduce mouse capture or an
  in-app transcript scroll without asking.
- Tests must not use network, sockets or real inference. Use the scripted
  client in `tests/helpers.py`.
- Error strings shown to the user are safe codes only; never surface raw HTTP
  bodies, headers or exception text from the runtime.

## Verification

```sh
.venv/bin/python -m pytest tests/
.venv/bin/python -m mypy   # strict; must pass with no issues
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
```

All four must pass before any change is considered done. ruff owns formatting
and import order (config in `pyproject.toml`); keep dataclasses, explicit types
and no star imports. `.githooks/pre-commit` runs ruff and mypy; enable it with
`git config core.hooksPath .githooks`.

The chat app is split into layers (`app_base` → `app_chat` → `app_composer` →
`app_runs` → `app_submission` → `app_sessions` → `app`). A layer calls only
methods of itself or of layers below it; shared state is declared in
`AppBase.__init__`.
