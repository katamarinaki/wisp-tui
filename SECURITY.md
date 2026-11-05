# Security

## Threat model

wisp-tui is a chat client for a Wisp runtime on the same machine. It holds a
Wisp `run` token and can answer the runtime's approval prompts, so it must
never widen what that token or a prompt allows.

**It defends against:**

- **Credential leaks.** The token is sent only to an explicit loopback
  `http://127.0.0.1:PORT` or `http://[::1]:PORT`, never through a redirect or
  an environment proxy. Errors show safe codes, never response bodies,
  headers or the token.
- **Scope creep.** A client-side route allowlist limits the client to chat
  routes. Billing, token, update, connector and permission-rule routes are
  never called. The allowlist restrains this tool only. The token itself
  may allow more.
- **Accidental approvals.** Each prompt needs an explicit key press after a
  short arming delay, so type-ahead or key repeat can't answer it. You can
  only deny arguments too large to review. A lasting project or global rule
  needs a second, separately armed press. The client does not resend a
  failed decision.
- **Duplicate work.** The client writes each submission to a private journal
  before sending it. A crash or a second client does not resend it.
- **Local tampering with private files.** Config and state are owner-only
  (0600 files in 0700 directories). The client opens them without following
  symlinks and rejects them if their ownership or mode is wrong.
- **Supply-chain substitution.** Dependencies install from hash-pinned wheels
  only. `wi update` accepts only a release tag signed by the pinned release
  key, so a compromised GitHub account or mirror can't push code to users.

**It does not defend against:**

- Anyone who can run commands as your user. They can read the token, the
  config and the local session logs.
- A malicious or compromised Wisp runtime. The client trusts the runtime it
  connects to for answers, tool output and approval prompts.
- Mistakes in what you approve. The dialog shows what the runtime sent, and
  the runtime may have truncated or redacted it.
- A compromised release key, or a release key pinned from a tampered clone.
  Compare the fingerprints `scripts/install` prints with the ones below.

## Data on disk

| Path | Holds |
|---|---|
| `~/.config/wisp-tui/config.json` | Runtime URL and the token, unless `token_command` is used |
| `~/.config/wisp-tui/release-signers` | The pinned public release signing keys |
| `~/.local/state/wisp-tui/sessions/` | Your prompts and Wisp's answers, in plain text |
| `~/.local/state/wisp-tui/` | Unsent drafts, prompt recall and the submission journal |

Session logs keep tool names and approval decisions, but not approval
arguments unless `log_approval_args` is on. Set `session_retention_days` to
delete old session logs at startup.

## Network

The client talks to the Wisp runtime on loopback. At startup it also asks the
checkout's Git `origin` for release tags, without prompting for credentials.
It sends no telemetry.

## Release signing keys

Releases are tags signed with an SSH key. The main key lives in the
maintainer's Mac Secure Enclave and needs Touch ID for every signature. It
can't be copied off the device. A second, offline key exists as a backup.
`scripts/install` pins both from `release-signers` and prints their
fingerprints, which must match these:

<!-- Owner: replace with the output of `ssh-keygen -lf` for each key. -->
```
SHA256:…  (Secure Enclave, Touch ID)
SHA256:…  (offline backup)
```

To trust new keys, delete `~/.config/wisp-tui/release-signers` and rerun
`scripts/install` from a clone you verified.

## Reporting a vulnerability

Use GitHub's security advisory form for this repository. Do not open a
public issue.
