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
| 3 | Mouse wheel scrolls the transcript; speeds up fast spins over SSH; Alt+wheel moves 5×. | Partial: Textual's fixed wheel step, no acceleration or Alt multiplier. |
| 4 | PageUp/PageDown scroll the transcript and take precedence over the editor. | Done. |
| 5 | Ctrl+Home/Ctrl+End jump to the top/bottom; Ctrl+End resumes following. | Not done: wisp-tui uses these keys for editor start/end. Owner decision. |
| 6 | Clickable "Jump to latest message" label while scrolled up. | Done: a row above the status line instead of an overlay on the last transcript row. |
| 7 | Working indicator embedded in the editor border. | Done. |
| 8 | Drag to select, auto-copy via OSC 52, highlighted selection. | Not done: `ALLOW_SELECT = False`, because the highlight paints a background (AGENTS.md rule). Terminal selection works with Option/Shift-drag. Owner decision. |
| 9 | Transcript search (Ctrl+Shift+F). | Not done. |
| 10 | Jump between prompts (Ctrl+Shift+Up/Down). | Not done. |
| 11 | Scrollbar revealed on hover. | Not done: scrollbar hidden. |
| 12 | On exit, prints the complete transcript and leaves the editor and footer below it (setting: resume hint only). | Partial: prints the newest `SHOWN_ITEMS` finished items plus live ones, the empty editor rules and the footer, then the resume hint. |
| 13 | iTerm2: set Advanced → "Trackpad scrolls fast?" to No for normal trackpad speed. | Applies as-is (same wheel reporting). |
| 14 | Tool entries (observed): command, decoded output tail with "(N earlier lines, ctrl+o to expand)", wrapped lines, "Took 0.0s", in a filled block. | Partial: output wraps and shows its last 5 rows with an earlier-lines count; shell results show decoded stdout/stderr (plus exit code / timeout); MCP titles show only the inner arguments. No expand key, no timing line apart from the status, and no fill (unpainted-background rule). |

When a change affects any row, update its status here in the same change.
