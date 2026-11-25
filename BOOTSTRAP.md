# BOOTSTRAP.md — agent-facing setup procedure

Written for coding agents (Claude Code, Pi, …) asked to set up a Wisp machine
from this repo. Read `docs/SYSTEM_SETUP.md` for what the target looks like
and why. Follow this top to bottom; **stop at the first failure** and report
the safe error code — never repair blindly, never skip a failed step.

## Prerequisites (verify before starting)

- A Linux machine (systemd, apt) you intend to run Wisp on, **loopback-only**.
- Root via `sudo` for the bootstrap step only; everything after runs as a
  normal user.
- A runtime tarball (`.tar.gz`) whose tree contains `bin/wisp` at its root —
  from the owner. Do not download it yourself unless the owner named the
  source.
- This repo checked out on the machine.

If any prerequisite is missing, stop and ask the owner.

## Procedure

### 1. Bootstrap the system side (root)

`scripts/bootstrap` is two-stage: it prints the plan and its SHA-256 digest,
then executes only when the digest is confirmed. Never edit the plan between
the two runs; the digest must match.

```sh
sudo scripts/bootstrap --runtime-tarball <path.tar.gz> --profile safe
# review the printed plan and digest, then:
sudo scripts/bootstrap --runtime-tarball <path.tar.gz> --profile safe \
    --expected-sha256 <digest> --activate
```

Options: `--profile pi-like` (file tools and bash allowed — Pi-like inside a
project) vs `--profile safe` (everything asks; the default recommendation).
`--with-telegram` adds the bot unit. It refuses existing users/homes/units
that don't match, so rerunning after a partial failure is safe.

### 2. Token and client (normal user, not root)

Ask the owner to create a **run-scoped token** for this client with the
runtime's own token command, run as the service account — never use an admin
token. Then, as the user who will chat:

```sh
scripts/install                       # creates the venv and the `wi` link
wi setup --url http://127.0.0.1:3008 --token-stdin   # paste the token, one line
```

Non-interactive variants (see `wi setup --help`): pass the token on stdin
from a script, or store a password-manager lookup instead of the secret:

```sh
wi setup --url http://127.0.0.1:3008 --token-command '["pass", "show", "wisp/wi"]'
```

### 3. Verify (stop on any FAIL)

```sh
wi --check                            # runtime health + token
wi audit                              # whole-machine drift check
wi audit --wisp-cli "sudo -u wisp -H /home/wisp/.wisp/bin/wisp"   # + live policy
wi                                    # open a chat to finish acceptance
```

All lines must be `pass`/`warn`/`skip` with no `fail` (the `unverified`
runtime-version warning is expected until the owner records acceptance in
RUNTIME_CONTRACT.md). `scripts/permissions --profile safe` (without
`--apply`) shows what a profile would change.

## Rules for agents

- Run commands exactly as printed; `scripts/bootstrap` and `wi` only accept
  explicit argv, never shell strings.
- Never echo the token or config contents; `wi` error messages are safe codes.
- Never widen the setup: no extra folders, no sudoers entries, no proxy in
  front of the loopback port, no additional API routes.
- Scope of this procedure ends at a working, verified `wi`. Anything else
  (diagnostics backends, node policy) is the owner's business.

## Rollback

Bootstrap is additive; to undo it completely (owner decision):

```sh
sudo loginctl disable-linger wisp
sudo runuser -u wisp -- env XDG_RUNTIME_DIR=/run/user/$(id -u wisp) \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u wisp)/bus \
    systemctl --user disable --now wisp.service wisp-telegram.service
sudo userdel -r wisp          # removes the home, runtime and units
wi update --remove || rm -f ~/.local/bin/wi   # client link
rm -rf ~/.config/wisp-tui ~/.local/state/wisp-tui            # client config/state
```

Then remove the checkout's `.venv` if you don't want it.
