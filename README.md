# wisp-tui

A terminal chat client for the [Wisp runtime](https://docs.usewisp.io/runtime).

Run `wi` on the machine that runs Wisp, at its keyboard or over SSH, and chat
with your agent. wisp-tui connects to the runtime's local API on `127.0.0.1`.
At startup it also checks the checkout's Git `origin` for new release tags.
It makes no other requests. See [Security](SECURITY.md).

```
terminal  ──▶ wisp-tui ──▶ Wisp runtime (127.0.0.1) ──▶ its model
```

## What it does

- Streams answers and live tool calls.
- Shows Wisp's approval prompts and waits for your key.
- If you quit or lose your connection, reopen the session and wisp-tui
  reattaches to the runs in progress.
- Browses and resumes Wisp sessions, including ones you started from Telegram
  or Desktop, and picks a model per session.

## Install and connect

Before you start, have a running [Wisp runtime](https://docs.usewisp.io/runtime)
and a `run` token for it. Wisp's documentation covers installing the runtime
and creating tokens.

Install `wi` on the machine that runs Wisp, from a local shell or over SSH.
You need Git and Python 3.11+ with `venv` and pip.

### Install wi

```sh
git clone https://github.com/katamarinaki/wisp-tui
cd wisp-tui
scripts/install    # venv with hash-checked wheels, links ~/.local/bin/wi
```

On first run the installer also pins the release signing keys, which
`wi update` checks. Compare the fingerprints it prints with the ones in
[Security](SECURITY.md). Checking signatures needs Git 2.34 or newer.

Keep the checkout. The `wi` launcher links into it. If the installer asks you
to add `~/.local/bin` to your `PATH`, run:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Add that line to your shell's startup file so new shells pick it up. To choose
the interpreter, set `PYTHON` to any installed 3.11+:
`PYTHON=python3.11 scripts/install`.

To use another link name, run `scripts/install NAME` and use that name wherever
this guide says `wi`. The installer rejects `wisp`, the runtime's own CLI.

### Connect to Wisp

```sh
wi setup
```

At the prompts:

1. Enter the runtime URL. Press Enter to accept `http://127.0.0.1:3008`, or
   type your runtime's URL with its port.
2. Paste your Wisp `run` token at the hidden prompt. Skip admin tokens: `wi`
   calls chat routes and nothing else.

`wi` accepts `http://127.0.0.1:PORT` or `http://[::1]:PORT` and rejects
`localhost` or any non-loopback address.

`wi setup` checks that the runtime is ready and the token works, without
starting inference. Then it saves an owner-only config under
`~/.config/wisp-tui/`.

For scripts and agents, `wi setup` also runs non-interactively:

```sh
wi setup --url http://127.0.0.1:3008 --token-stdin          # token on stdin, one line
wi setup --url http://127.0.0.1:3008 --token-command '["pass", "show", "wisp/wi"]'
```

`--token-command` stores the argv list (JSON, run without a shell on every
load) instead of the secret itself. See BOOTSTRAP.md for the full agent
procedure.

### Set up a whole machine

On the machine that runs Wisp, `scripts/bootstrap` installs the reference
system setup of `docs/SYSTEM_SETUP.md`: dedicated nologin service account,
loopback-only runtime, systemd user units, sandbox and a permission profile.
It is owner-run, two-stage (print the plan and its digest, then confirm the
digest with `--activate`), idempotent where safe and refuses existing paths
otherwise — it never repairs blindly and never creates sudoers entries.

```sh
sudo scripts/bootstrap --runtime-tarball wisp.tar.gz --profile safe
sudo scripts/bootstrap --runtime-tarball wisp.tar.gz --profile safe \
    --expected-sha256 <printed-digest> --activate
```

Permission profiles (`profiles/pi-like.json`, `profiles/safe.json`) are the
same JSON `wi audit --profile` diffs against. `scripts/permissions` prints
(or with `--apply` runs) the `wisp permissions set` commands for one:

```sh
scripts/permissions --profile safe --wisp-cli "sudo -u wisp -H /home/wisp/.wisp/bin/wisp"
```

### Check and start

```sh
wi --check        # check runtime health without starting inference
wi                # open a new chat
wi audit          # read-only drift check of the whole setup
```

### Audit the setup

`wi audit` compares the machine with the reference system setup for Wisp:
a dedicated service account (nologin shell, owner-only home, lingering user
services), a runtime listening on loopback only, live `serve` and optional
`telegram` processes, and owner-only `wi` config and state files. It is
strictly read-only, changes nothing, runs no inference, and never prints the
token or config contents. Each line is `pass` / `fail` / `warn` / `skip` with a
safe code; the exit code is 1 when anything failed, so it fits cron and
scripts. `--skip GROUPS` drops groups, `--json` prints machine-readable output.

The runtime-policy group (runtime version vs RUNTIME_CONTRACT.md, sandbox,
auto-mode, permissions) needs the runtime CLI, which usually belongs to the
service account. Pass the command you use to reach it:

```sh
wi audit --wisp-cli "sudo -u wisp -H /opt/wisp/bin/wisp"
```

To diff live permissions against the setup you intend, give it a profile
file (`--profile FILE`), JSON with any of `sandbox` (`"enabled"`/`"disabled"`),
`auto_mode` (bool) and `permissions` (tool → `"allow"`/`"ask"`/`"deny"`):

```json
{"sandbox": "enabled", "auto_mode": true,
 "permissions": {"fs_write": "allow", "bash_run": "ask"}}
```

Rules the CLI prints in another format are reported as warnings, not silent
passes; `wisp permissions get` output shapes vary between runtime releases.

Each message you send starts a run on Wisp. The run keeps going if you quit
`wi` or lose your connection. Back on the same machine (reconnect SSH first if
you use it), run `wi -c` to reopen your last saved session or `wi -r` to pick
one. Type `/cancel` to ask Wisp to stop the run.

### Setup troubleshooting

- **`wi: command not found`:** add `~/.local/bin` to `PATH` as above, or invoke
  `~/.local/bin/wi`.
- **Python or venv setup fails:** confirm your Python is 3.11+ with `venv` and
  pip, then rerun `scripts/install`.
- **`runtime_must_be_explicit_loopback_http`:** enter `http://127.0.0.1:` plus
  your runtime's port.
- **`runtime_unreachable`, `runtime_timeout` or `runtime_not_ready`:** confirm
  Wisp runs on this machine and listens on the port you entered. Then rerun
  `wi setup`, or `wi --check` if setup already saved a config.
- **`invalid_token`, `runtime_http_401` or `runtime_http_403`:** confirm you
  pasted a `run` token for this runtime, without quotes or spaces, then rerun
  `wi setup`.

`wi setup` won't overwrite an existing config. To change the URL or token,
delete `~/.config/wisp-tui/config.json` and run `wi setup` again.

## Usage

```sh
wi                # new session
wi -c             # continue the last session
wi -r [ID]        # pick a saved session, or resume one by ID prefix
wi update         # verify the newest signed release, switch after you confirm
```

Like Pi, new sessions work in the folder you launch from: Wisp's file tools
work there and ask before going outside it. Launched from your home folder or
`/`, sessions get no project folder. A resumed session keeps its own folder.

Inside the app: **Enter** sends, **Ctrl+J** adds a newline, **F1** lists keys,
**F2** opens sessions, **F3** picks a model. Type in a picker to filter it. New
sessions start with saved defaults, like Pi's: in the model picker **Ctrl+S**
picks the model and keeps it for new sessions, and `/thinking` keeps its
setting too. They are saved under `~/.local/state/wisp-tui/`; `/status` shows
them. Commands: `/new`, `/resume`,
`/sessions`, `/model`, `/thinking [on|off]`, `/cancel`, `/retry`, `/reconnect`,
`/status`, `/exit` (or `/quit`); typing `/` lists them and Tab completes. Any
other `/name args` runs the Wisp skill `name`, as in Wisp's other clients; the
commands above take priority, and a lone word that looks like a mistyped
command is not sent. Porting a Pi skill is mostly mechanical: a Pi skill is a
folder with `SKILL.md` (name + description + instructions); give Wisp the same
`SKILL.md` content as a skill, keep the description line (that is what the
model routes on), and replace Pi-only tooling references with Wisp's file and
shell tools. The app runs full screen:
the mouse wheel or PageUp/PageDown scrolls the chat while the editor and footer
stay put. Drag over the chat to select text; releasing the mouse copies it,
through `pbcopy` on a local Mac and through your terminal (OSC 52) otherwise,
for example over SSH. On quit, the conversation is printed to the terminal's
scrollback.

Wisp stores your sessions. wisp-tui keeps a private log under
`~/.local/state/wisp-tui/` so `-c` and `-r` know which ones you used.

When a newer release exists, the chat and a line under the editor say so at
startup. `wi update` installs it only if the release tag carries a valid
signature from the pinned key.

### Optional settings

Add these keys to `~/.config/wisp-tui/config.json` by hand:

| Key | Effect |
|---|---|
| `token_command` | Replaces `token`. A command as a list, run without a shell, that prints the token. Example: `["pass", "show", "wisp/run-token"]` |
| `session_retention_days` | Deletes local session logs older than this many days at startup. Unset keeps them. |
| `log_approval_args` | `true` also logs approval arguments, which can contain commands, paths and file contents. Off by default. |
| `accent_color` | Color of the editor rules and the "Jump to latest message" label: one of the terminal's 16 colors, `black`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, or a `bright_` variant such as `bright_magenta`. Default `bright_blue`. |

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

The [runtime contract notes](RUNTIME_CONTRACT.md) cover the API the client
relies on.
