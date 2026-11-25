# Terminal UX target

wisp-tui follows **Pi's fullscreen mode**: Pi 1.4.0 with `tuiMode: "fullscreen"`,
which has been Pi's default since 1.0.0 (2026-10-01). Before 1.0.0, Pi
defaulted to regular mode (native scrollback), which is what earlier wisp-tui
releases copied.

Sources, from an installed `@earendil-works/pi-coding-agent` 1.4.0:
`docs/usage.md` ("Adjust the terminal"), `docs/settings.md` (`tuiMode`,
`fullscreen*`), `docs/keybindings.md` ("Fullscreen"), `docs/terminal-setup.md`
(iTerm2), and the `pi-tui` README (`TuiAltScreen`). Re-check them when Pi
changes. To compare raw output, run Pi with `PI_TUI_WRITE_LOG` set and wisp-tui
under a PTY capture (`tests/test_terminal.py`).

| # | Pi fullscreen behavior | wisp-tui |
|---|---|---|
| 1 | Editor and status area stay fixed at the bottom; the transcript scrolls above them. | Done: alternate screen, docked `#bottom`. Status and command hints sit above it, so they never move the editor. |
| 2 | Follows streaming output while at the bottom; keeps a manually chosen position while content grows. | Done: `ChatScroll` anchor. |
| 3 | Mouse wheel scrolls the transcript; speeds up fast spins over SSH; Alt+wheel moves 5×. | Done: `ChatScroll` acceleration — a fast spin grows up to 10 rows per event inside a 0.3 s window; Alt+wheel 5×; both scale with the app's scroll sensitivity. |
| 4 | PageUp/PageDown scroll the transcript and take precedence over the editor. | Done. |
| 5 | Ctrl+Home/Ctrl+End jump to the top/bottom; Ctrl+End resumes following. | Not done: wisp-tui uses these keys for editor start/end. Owner decision. |
| 6 | Clickable "Jump to latest message" label while scrolled up. | Done: a row above the status line instead of an overlay on the last transcript row. |
| 7 | Working indicator embedded in the editor border. | Done. |
| 8 | Drag to select, auto-copy (native clipboard locally, OSC 52 over SSH), highlighted selection, "Copied!" flash. | Partial: chat only, reverse-video highlight, copy on release (`pbcopy` on a local Mac, else OSC 52), a reverse-video "Copied!" flash at the top right (1 s; "Copy failed" 5 s); a click clears it, and so does trimming the oldest items. No auto-scroll past the chat's edge, no double/triple-click word/line selection. |
| 9 | Transcript search (Ctrl+Shift+F). | Done: a search bar above the chat (Enter next, Shift+Enter previous, Esc closes); the current hit is marked in reverse video like the drag selection and scrolled to a third from the top; the case-insensitive scan wraps; "no matches" shows beside the input. |
| 10 | Jump between prompts (Ctrl+Shift+Up/Down). | Done: jumps between user prompts; Ctrl+Shift+Down past the last prompt resumes following. |
| 11 | Scrollbar revealed on hover. | Done: a right-edge overlay scrollbar while the mouse is over the chat — glyphs only (track dim, thumb bold), no reflow of the transcript, no background painted, hidden when the content fits. Not draggable. |
| 12 | On exit, prints the complete transcript and leaves the editor and footer below it (setting: resume hint only). | Partial: prints the newest `SHOWN_ITEMS` finished items plus live ones, the empty editor rules and the footer, then the resume hint. |
| 13 | iTerm2: set Advanced → "Trackpad scrolls fast?" to No for normal trackpad speed. | Applies as-is (same wheel reporting). |
| 14 | Tool entries (observed): command, decoded output tail with "(N earlier lines, ctrl+o to expand)", wrapped lines, "Took 0.0s", in a filled block. | Done: filled block (dark grey, dark red on failure); output wraps and shows its last 5 rows with an earlier-lines count and "ctrl+o to expand"; **Ctrl+O** toggles every entry (and entries added while on), a click toggles one; expanded shows the full command and output; results are read from stored history while the run is live (every 2 s, backing off to 30 s); shell results show decoded stdout/stderr (plus exit code / timeout); JSON results show as `path: value` lines; MCP titles show the inner arguments (and the inner name when it differs). |
| 15 | Selectors (model, sessions) replace the editor in place, as tall as their content: a `>` filter, `→` cursor, `✓` current item, `(n/total)`, a key-hint line; Ctrl+S sets the default model, and the thinking level is kept for new sessions. | Done (thinking saved by `/thinking`): pickers and approval prompts are full-width panels over the editor, between accent rules (approvals say "(no arguments)" instead of `{}`), with the chat above still visible; the status line they cover is repeated at the panel's top. No provider column or model detail line (the runtime's model list has only `id` and `reasoning`). |
 
When a change affects any row, update its status here in the same change.
