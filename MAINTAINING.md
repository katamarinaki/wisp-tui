# Maintaining wisp-tui

## Known limits

- Every question you send spends Wisp account balance, the same as a Telegram
  or Desktop session.
- Safe multiline paste needs your terminal's **bracketed paste**, which Textual
  turns on. Without it, a pasted carriage return looks identical to pressing
  Enter, and no timing heuristic can tell them apart. Draft recovery and
  prompt recall cover input you typed in this TUI. Prompts you sent from other
  clients don't appear in recall.
- Scripted responses cover recovery in the tests, and the owner has run the
  end-to-end SSH workflow (see the live-acceptance note in
  [Runtime contract](RUNTIME_CONTRACT.md)). The runtime's
  default run listing still leaves out internal and Desktop-origin runs.
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

## Releasing

A release is an SSH-signed `vX.Y.Z` tag. `wi update` refuses anything else.

1. On `develop`, bump the version in both `pyproject.toml` and
   `src/wisp_tui/__init__.py`, then merge `develop` into `main`.
2. When CI passes on `main`, the release-draft workflow drafts `vX.Y.Z` for
   that commit, with the commit subjects since the last release as notes.
   Each later push to `main` redrafts it and discards edits to the notes,
   until the tag exists.
3. Sign the tag for the draft's commit with the Secure Enclave key, confirm
   with Touch ID, and push it:

   ```sh
   git fetch origin main
   SSH_AUTH_SOCK=<Secretive agent socket> git -c gpg.format=ssh \
       -c user.signingkey="key::<Secure Enclave public key>" \
       tag -s vX.Y.Z -m "wisp-tui vX.Y.Z" <commit from the draft>
   git push origin vX.Y.Z
   ```

   With the backup key, use `-c user.signingkey=<path to the backup key>`
   instead and drop `SSH_AUTH_SOCK`.

4. Edit the notes if needed, then publish the draft. Push the signed tag
   first: publishing without it makes GitHub create an unsigned tag, which
   `wi update` rejects until you replace it. Running `wi` shows the new
   release on its next start.

### Signing keys

`release-signers` lists the public keys whose signatures `wi update` accepts,
in Git's allowed-signers format. Users pin it at install time, so changing it
later means every user deletes `~/.config/wisp-tui/release-signers` and reruns
`scripts/install`. List both keys from the start, so losing the Mac doesn't
force that.

1. **Main key.** In the Secretive app, create a key that requires
   authentication. It lives in the Secure Enclave, asks for Touch ID on every
   use, and can't be exported or backed up.
2. **Backup key.** Create it away from daily use, with a strong passphrase,
   and keep it offline. A hardware key works too: `ssh-keygen -t ed25519-sk`.

   ```sh
   ssh-keygen -t ed25519 -C "wisp-tui release backup" -f wisp-tui-release-backup
   ```

3. **Write `release-signers`** with one line per public key, then commit it:

   ```
   wisp-tui-release namespaces="git" ecdsa-sha2-nistp256 AAAA…
   wisp-tui-release namespaces="git" ssh-ed25519 AAAA…
   ```

4. **Publish the fingerprints** in [Security](SECURITY.md). Get them with
   `ssh-keygen -lf <public key file>`.

## Upgrading dependencies

`requirements.txt` pins the runtime packages to exact versions, and
`requirements-dev.txt` adds the test, mypy and ruff tooling. `pyproject.toml`
allows Textual 8.2.x. Every pin lists the sha256 of each wheel PyPI publishes for it,
and `--only-binary :all:` refuses source builds. `tests/test_dependencies.py`
fails when your venv drifts from the pins or a pin lacks hashes.

Textual carries the most risk: wisp-tui relies on rendering details that
Textual has changed before. To upgrade:

1. Bump the pins, swap in the wheel hashes PyPI lists for the new releases,
   reinstall, and run `pytest`, `mypy` and `ruff check`.
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

`src/wisp_tui/terminal.py` is the only module that imports Textual internals
(`textual._compositor.InlineUpdate`), and `AppBase._display` overrides a
private Textual method. Check both against the new Textual source first.
