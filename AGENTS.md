# wisp-tui conventions

Terminal chat client for the local Wisp runtime. Python 3.11+, stdlib + textual
(project venv only — never install system-wide).

## Scope and plan

Read [Road to MVP](ROAD_TO_MVP.md) before feature work and update its checklist
as work lands. The only target workflow is Mac → SSH → homeserver → Wisp:
reliable task submission, progress, explicit approvals, cancellation, and
reattachment. Do not expand this into a general-purpose Pi replacement.

## Rules

- **Never** put credentials, tokens or the config contents in code, tests,
  docs, logs or chat output. Config/state live only under `~/.config/wisp-tui`
  and `~/.local/state/wisp-tui` (0600/0700).
- The runtime URL is always explicit loopback http; keep the no-redirect /
  no-env-proxy client behavior.
- The client-side route allowlist in `src/wisp_tui/client.py` is deliberate:
  chat routes only. Extending it (e.g. DELETE sessions) needs a conscious
  decision — billing/tokens/update/connector/permission routes stay out.
- Interactive approval is the only privileged action; it must stay explicit
  per prompt. No auto-approval paths.
- The terminal background must stay unpainted: `App { background: ansi_default; }` +
  `Screen { background: transparent; }` in the CSS and `ansi_color=True` in
  `WispTuiApp.__init__` work together to emit SGR 49. Changing any of these
  (or re-enabling the ANSI→truecolor filter) paints a hardcoded background.
  Verify with a pty capture that no other `48;...` SGR is emitted.
  Sole owner-approved exception: user prompt blocks (`#chat > .user`) are
  filled with muted dark teal `#005f5f` (xterm-256 #23, `48;5;23`) with fixed
  light-grey text. Palette cyan was rejected as too bright. Don't change the
  shade or extend fills to other elements without asking.
- Tests must not use network, sockets or real inference. Use the scripted
  client in `tests/helpers.py`.
- Error strings shown to the user are safe codes only; never surface raw HTTP
  bodies, headers or exception text from the runtime.

## Verification

```sh
.venv/bin/python -m pytest tests/
.venv/bin/python -m mypy   # strict; must pass with no issues
```

Both must pass before any change is considered done. No formatter/linter is
configured; keep the existing style (dataclasses, explicit types, no star
imports).
