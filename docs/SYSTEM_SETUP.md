# System setup reference

The reference setup for a Wisp machine — the architecture `scripts/bootstrap`
installs, `wi audit` checks for drift, and `scripts/permissions` applies
profiles to. It exists so the whole system, not just the chat client, is
reproducible from this repo.

Everything below describes **loopback-only, single-user machines**: the box
that runs Wisp is the box you chat from (locally or over SSH). Wisp's API
binds to `127.0.0.1` (or `::1`) and is never exposed to a network.

## Layout

| Piece | Detail |
|---|---|
| Service account | dedicated `wisp` user, system account (`useradd -r`), `/usr/sbin/nologin` shell, home `0700`, linger enabled so its user services run without a login session |
| Runtime | `/home/wisp/.wisp/bin/wisp`, owned by the service account, never writable by anyone else |
| API | `wisp serve` on `127.0.0.1:3008`, a systemd **user** unit (`wisp.service`) of the service account |
| Telegram bot (optional) | `wisp telegram`, second user unit (`wisp-telegram.service`) |
| Client | `wi` (this repo) installed per login user via `scripts/install`; config and state under that user's `~/.config/wisp-tui` / `~/.local/state/wisp-tui`, owner-only |
| Credential | a **run-scoped token** for `wi` — never the runtime's admin token. `wi setup` also accepts `--token-command` so the secret can live in a password manager and never touch disk |

The unit files ship in `systemd/`; permission profiles in `profiles/`.

## Why these choices

- **Dedicated nologin account.** The runtime executes model-chosen tools; it
  gets its own identity, own home, and no login shell, so a compromise of the
  runtime is not a compromise of an interactive account. Linger keeps its
  services alive without anyone logged in.
- **Loopback only.** The API carries the token and conversation content.
  `wi` hard-refuses non-loopback URLs, and `wi audit` fails if the runtime
  listens on anything beyond loopback. This is deliberate; do not put a
  reverse proxy in front of it on a multi-user machine.
- **Run token, not admin token.** The client needs to start runs and answer
  approvals on its own runs — that is the documented capability of a run
  token. Admin/token-management routes stay out of the client's route
  allowlist entirely.
- **User units, not a system unit.** The runtime's state, credentials and
  tools all live under the service account's home; a system unit would invite
  root-owned files inside it. Managing them requires the account's own bus
  (`runuser` + `XDG_RUNTIME_DIR`), which `scripts/bootstrap` does for you.
- **No sudoers, ever.** The original hand-rolled setup installed the runtime
  as the nologin user through a temporary sudoers file. `scripts/bootstrap`
  instead installs as root and `chown`s, then configures via `runuser`. There
  is no privileged path left behind.

## Sandbox and permissions

The reference setup runs with the sandbox **on** (bubblewrap), auto-mode on,
and a small explicit permission profile. Profiles are JSON files
(`profiles/pi-like.json`, `profiles/safe.json`) shared by three tools:

- `scripts/bootstrap --profile NAME` applies one while installing;
- `scripts/permissions --profile NAME [--apply]` prints (or runs) the
  `wisp permissions set` commands for one;
- `wi audit --profile FILE` diffs the live state against one and fails on
  drift.

Profiles list the rules observed on the reference runtime (`fs_write`,
`fs_edit`, `bash_run`). List your runtime's actual tools with
`wisp permissions get` and extend the profile to match.

### Pi → Wisp permission mapping

Wisp's rule names are not Pi's; the philosophical mapping is:

| Pi concept | Wisp rule |
|---|---|
| File edits inside the project folder run without asking | `fs_write`/`fs_edit` = `allow` (Wisp scopes file tools to the session's project root) |
| Shell commands prompt by default | `bash_run` = `ask` (Pi-like profile sets `allow`; understand the trade-off) |
| Hardcoded refusals (secrets, destructive commands) | keep as `deny` rules; the runtime enforces them before any approval flow |
| Project folder = privilege grant | the Project Session root; where you launch `wi` is what the file tools may touch |

Anything not listed keeps the runtime's default (ask). `pi-like` is
"close to the Pi experience inside a project"; `safe` asks for every
side-effecting tool. Both are starting points to edit, not doctrine.

### Hardcoded refusals

The original unversioned `wisp-max-freedom.sh` carried a header rule worth
keeping: even a max-freedom setup refuses to grant blanket allow to
credential paths and destructive commands — those stay `deny`/`ask` no matter
the profile. If you write your own profile, keep that property.

## Diagnostics pattern (generic)

Node-specific backends (like a staking-node diagnostics suite) stay out of
this repo. The generic pattern that does belong here, for any machine: run a
read-only diagnostics backend as root, expose it to Wisp through an
MCP adapter over a permissioned socket with a peer-credential check, and
register it as a global read-only skill. Nothing in this repo implements
that; it is the shape to copy.

## Drift checking

`wi audit` compares a machine against this document: service account shape,
home permissions, linger, loopback-only listener, live services, owner-only
client files, and — with `--wisp-cli` — live policy. Exit code 1 on any
failure, so it can run from cron or an agent. See README "Audit the setup".
