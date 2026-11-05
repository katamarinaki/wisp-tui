"""The F1 / /help text and the slash commands offered while typing."""

# (command, arguments, description); the first prefix match is completed.
COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("/help", "", "keys and commands"),
    ("/new", "[title]", "start a fresh session"),
    ("/resume", "[id]", "pick or open a saved session"),
    ("/sessions", "", "browse all Wisp sessions"),
    ("/model", "", "choose model or mode"),
    ("/cancel", "", "cancel the active run"),
    ("/retry", "", "resend the unacknowledged request"),
    ("/reconnect", "", "reattach to the active run"),
    ("/status", "", "connection and session details"),
    ("/exit", "", "leave wisp-tui"),
    ("/quit", "", "leave wisp-tui"),
)

HELP_TEXT = """**wisp-tui** — chat with the local Wisp runtime.

| Key | Action |
|---|---|
| Enter | Send the whole prompt |
| Ctrl+J | Insert a newline (SSH-safe) |
| Shift+Enter | Insert a newline if your terminal distinguishes it |
| Ctrl+P / Ctrl+N | Previous / next submitted prompt; return to your draft |
| Ctrl+Shift+P | Command palette, where the terminal distinguishes it |
| F1 | This help |
| F2 | Session picker |
| F3 | Model / mode picker |
| Ctrl+C | Cancel the active run — press twice to leave |

Bracketed paste stays literal in the editor until you press Enter. Slash
commands execute only when the entire input is a single line; multiline text
starting with `/` is a prompt, not a sequence of commands. Ctrl+J works over
ordinary SSH; Shift+Enter may arrive as plain Enter in some terminals.

**Commands:** `/new [title]`, `/resume`, `/sessions`, `/model`, `/cancel`,
`/retry`, `/reconnect`, `/status`, `/help`, `/exit` (or `/quit`).
Typing `/` lists matching commands; Tab or Right completes the suggestion.

**Sessions:** every launch starts fresh; your first message creates the Wisp
session. `/new` starts another one. Sessions you use here are saved locally
(`~/.local/state/wisp-tui/sessions/`): `/resume` (or `wisp-tui -r`) picks one,
`/resume 3bf1ba87` (or `wisp-tui -r 3bf1ba87`) opens it by ID prefix, and
`wisp-tui -c` continues the most recent. `/sessions` (F2) lists all Wisp
sessions, including Telegram/desktop ones.

`/retry` retries an unacknowledged submission with its original input and request
ID. It never sends your newer draft. Pending requests are saved privately before
submission and recovered after exit, without automatic resubmission. A pending
request state error blocks new work until resolved.

Ctrl+P/Ctrl+N browse your last 50 acknowledged prompts, preserving the current
draft and edits made while browsing. Up/Down still move within multiline text.
One unsent draft is saved locally after a short pause and on exit, up to 128 KiB.
A fresh draft restores on a fresh launch; a session draft restores when you
resume that session. Starting new text replaces the one saved draft. Nothing
is sent automatically. Input history/recovery errors leave editing available.

A finished run is one answered prompt, not the end of the conversation: just
send your next message. The footer keeps the session, model/mode, thinking
setting and last finished run's reported tokens visible (↑ input, ↓ output,
Σ total). Missing stats are hidden; `/status` explains what is not reported.
Tokens update when a run finishes, not live; context occupancy is unavailable.
The launch directory belongs to this client, not Wisp's tools.

Finished output is printed into the terminal's own scrollback, like any
command's output: scroll, select and copy it with the terminal as usual, and it
stays after you quit. Only the answer being written, running tools and the
input stay in the live area at the bottom. Tool entries show the command, the
last 5 output lines (read from stored session history when a unique call ID is
available) and the status; full output stays in Wisp's history.

While a run is active you will see tool calls as they happen. When Wisp needs
approval for a tool, a dialog appears: **y** approve, **n** deny, **p** allow
for this project, **g** allow globally, **Esc** deny. Letter keys work once the
dialog has been shown for half a second, so typing ahead cannot answer it.
Use Up/Down or PageUp/PageDown to scroll long approval details; decision keys
stay visible. Ctrl+C still cancels work from approvals and pickers; press twice
to leave. Normal questions consume the Wisp account balance, exactly like
Telegram/Desk sessions.
"""
