"""Chat transcript: live items, finished items above them, tool entries."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from typing import Any

from rich.text import Text
from textual.widgets import Static

from .app_base import AppBase
from .chat_view import ChatItem, ChatScroll, StreamView
from .client import ClientError, safe_code
from .history import ChatMessage, HistoryError, parse_history, tool_history_results
from .text import safe_text
from .tool_data import output_text
from .tool_view import ToolCallView

ChatEntry = ChatItem | ToolCallView

# A finished run's final checkpoint can commit moments after the run.completed
# event; retry the stored-result read before reporting it as missing.
TOOL_HISTORY_POST_RUN_DELAYS = (1.0, 3.0, 6.0)


def hidden_note(hidden: int) -> str:
    return (
        f"\u2191 {hidden} earlier item{'s' if hidden != 1 else ''} not shown "
        "\u2014 Wisp keeps the full session history."
    )


def _pbcopy(text: str) -> bool:
    try:
        done = subprocess.run(["pbcopy"], input=text.encode(), timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


class ChatLayer(AppBase):
    def action_chat_page(self, direction: int) -> None:
        self.query_one("#chat", ChatScroll).page(direction)

    def on_chat_scroll_selected(self, message: ChatScroll.Selected) -> None:
        """Like Pi: the native clipboard locally, OSC 52 over SSH."""
        text = message.text
        if sys.platform == "darwin" and not os.environ.get("SSH_CONNECTION"):
            # Terminal.app ignores OSC 52 and iTerm2 blocks it by default.
            copied: list[bool] = []
            self._run_threaded(
                lambda: copied.append(_pbcopy(text)),
                group="clipboard",
                on_done=lambda: self._flash_copied(copied == [True]),
            )
            return
        # Pi's limit; terminals drop or mangle longer OSC 52 payloads.
        if len(base64.b64encode(text.encode())) > 100_000:
            self._flash_copied(False)
            return
        self.copy_to_clipboard(text)
        self._flash_copied(True)

    def _flash_copied(self, copied: bool) -> None:
        # Pi's durations: a short confirmation, a longer error.
        self._flash("Copied!" if copied else "Copy failed", 1.0 if copied else 5.0)

    def _flash(self, message: str, seconds: float) -> None:
        """A transient reverse-video note at the top right, apart from the status."""
        if self._flash_timer is not None:
            self._flash_timer.stop()
        for flash in self.query("#flash").results(Static):
            flash.update(Text(f" {message} ", style="reverse"))
            flash.display = True
        self._flash_timer = self.set_timer(seconds, self._hide_flash)

    def _hide_flash(self) -> None:
        self._flash_timer = None
        for flash in self.query("#flash").results(Static):
            flash.display = False

    def _update_jump_latest(self) -> None:
        chat = self.query_one("#chat", ChatScroll)
        self.query_one("#jump-latest").display = chat.scroll_y < chat.max_scroll_y

    def _mount_chat(self, widget: ChatEntry, before: ChatEntry | None = None) -> None:
        """Queue an item in display order; finished items move above live ones."""
        self._welcome.display = False
        if before is not None and before in self._chat_items:
            self._chat_items.insert(self._chat_items.index(before), widget)
        else:
            self._chat_items.append(widget)
        self._queue_sync()

    def _queue_sync(self) -> None:
        if not self._sync_queued:
            self._sync_queued = True
            # Deferred, so a batch (e.g. a history load) completes its tool
            # entries before they are finished.
            self.call_later(self._sync_chat)

    def _settled(self, item: ChatEntry) -> bool:
        if isinstance(item, ToolCallView):
            return item.settled
        return self._stream_view is None or item is not self._stream_view.widget

    @staticmethod
    def _entry(item: ChatEntry) -> tuple[str, str]:
        return item.entry() if isinstance(item, ToolCallView) else (item.kind, item.remaining())

    def _sync_chat(self, force: bool = False) -> None:
        """Show the finished leading items as finished; mount the rest live.

        Items finish strictly in order, so a running tool holds back later text.
        """
        self._sync_queued = False
        while self._chat_items:
            item = self._chat_items[0]
            if not force and not self._settled(item):
                if isinstance(item, ChatItem):
                    self._commit((item.kind, item.take_settled()))
                break
            self._chat_items.popleft()
            self._commit(self._entry(item))
            if item.parent is not None:
                item.remove()
        self._tool_calls = {
            key: view for key, view in self._tool_calls.items() if view in self._chat_items
        }
        for chat in self.query("#chat").results(ChatScroll):
            previous: ChatEntry | None = None
            for item in self._chat_items:
                if item.parent is None:
                    if previous is not None:
                        chat.mount(item, after=previous)
                    else:
                        # Finished items stay above; join any live ones below them.
                        live = next((i for i in self._chat_items if i.parent is chat), None)
                        if live is not None:
                            chat.mount(item, before=live)
                        else:
                            chat.mount(item)
                previous = item

    def _commit(self, entry: tuple[str, str]) -> None:
        if not entry[1].strip():
            return
        if len(self._printed) == self.SHOWN_ITEMS:
            self._dropped += 1
        self._printed.append(entry)
        for chat in self.query("#chat").results(ChatScroll):
            widget = ChatItem(*entry)
            live = next((item for item in self._chat_items if item.parent is chat), None)
            if live is not None:
                chat.mount(widget, before=live)
            else:
                chat.mount(widget)
            self._finished.append(widget)
            while len(self._finished) > self.SHOWN_ITEMS:
                self._finished.popleft().remove()
                # Later rows move up; the selection would mark other text.
                chat.clear_selection()
            if self._dropped:
                if self._dropped_note is None:
                    self._dropped_note = ChatItem("system", hidden_note(self._dropped))
                    chat.mount(self._dropped_note, before=0)
                else:
                    self._dropped_note.set_text(hidden_note(self._dropped))

    def _reset_chat(self, hidden: int = 0) -> None:
        """Start a fresh view below everything shown; optionally note hidden items."""
        self._chat_epoch += 1
        self._reattached_checkpoint = None
        self._pending_view_history = None
        if self._stream_view is not None:
            self._stream_view.flush()
            self._stream_view = None
        self._sync_chat(force=True)
        self._tool_calls.clear()
        self._tool_history_token = None
        self._tool_history_targets = ()
        self._tool_history_again = False
        self._tool_history_post_run_reads = 0
        if hidden:
            self._show_line(hidden_note(hidden))

    def transcript(self) -> list[tuple[str, str]]:
        """(kind, text) of the shown finished and live items, in display order."""
        dropped = [("system", hidden_note(self._dropped))] if self._dropped else []
        return [*dropped, *self._printed, *(self._entry(item) for item in self._chat_items)]

    def _show_line(self, text: str, cls: str = "system") -> None:
        self._mount_chat(ChatItem(cls, safe_text(text)))

    def _record_locally(self, entry: dict[str, Any], session_id: str | None = None) -> None:
        """Append to the local session record; report a failure once."""
        target = session_id or self.session_id
        if target is None:
            return
        title = "" if self.session_title == "(untitled)" else self.session_title
        code = self.sessions.record(target, entry, title=title)
        if code and not self._record_error_shown:
            self._record_error_shown = True
            self._show_line(f"Local session record unavailable: {code}", "error")

    def _show_tool_call(
        self,
        event: dict[str, Any],
        *,
        completed: bool,
        input_text: str | None = None,
        historical: bool = False,
        terminal: bool = False,
        summary: str = "",
    ) -> ToolCallView:
        call_id = event.get("toolCallId")
        key = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else ""
        if not historical and not terminal:
            self._saw_live_tool_events = True
        view = self._tool_calls.get(key) if key else None
        if view is not None:
            view.update_event(event, completed=completed)
        else:
            view = ToolCallView(event, completed=completed, historical=historical)
            view.session_id = self.session_id
            if terminal:
                view.is_run_summary = True
                view.refresh_entry()
            before = None
            if self._stream_view is not None:
                if historical or terminal:
                    # Above the answer, unless part of it is already printed.
                    widget = self._stream_view.widget
                    before = None if widget.committed else widget
                else:
                    # A live call ends this text segment; later text goes below it.
                    self._stream_view.flush()
                    self._stream_view = None
                    self._answer_segment_start = len(self._answer_text)
            self._mount_chat(view, before=before)
            if key:
                self._tool_calls[key] = view
        if input_text is not None:
            # Checkpoint arguments have already been bounded/formatted.
            view.apply_history(input_text=input_text, summary=summary)
        self._queue_sync()
        if completed and not historical:
            # Deferred so parallel completions share one history read.
            self.call_after_refresh(self._request_tool_history)
        return view

    def _add_message(self, message: ChatMessage) -> None:
        if message.role == "assistant":
            if message.text.strip():
                self._mount_chat(ChatItem("assistant", message.text))
            for call in message.tool_calls:
                self._show_tool_call(
                    {"name": call.name, "toolCallId": call.call_id},
                    completed=False,
                    input_text=call.input_text,
                    historical=True,
                    summary=call.summary,
                )
        elif message.role == "tool":
            view = self._show_tool_call(
                {"name": message.tool_name, "toolCallId": message.tool_call_id},
                completed=True,
                historical=True,
            )
            view.apply_history(output_text=output_text(message.text), status=message.tool_status)
        else:
            if message.role == "user":
                self._tool_calls.clear()
            self._mount_chat(ChatItem(message.role, message.text))

    def _tool_targets(self) -> tuple[ToolCallView, ...]:
        return tuple(
            view
            for view in self._chat_items
            if isinstance(view, ToolCallView)
            and view.session_id == self.session_id
            and view.call_id
            and view.output_text is None
        )

    def _apply_tool_history(
        self, messages: list[ChatMessage], targets: tuple[ToolCallView, ...]
    ) -> None:
        matched, ambiguous = tool_history_results(messages, {view.call_id for view in targets})
        retry = False
        post_run_retry = False
        for view in targets:
            if (
                view not in self._chat_items
                or view.session_id != self.session_id
                or view.output_text is not None
            ):
                continue
            if view.call_id in ambiguous:
                view.set_result_notice("Stored call ID is ambiguous; result not matched.")
            elif view.call_id in matched:
                details = matched[view.call_id]
                view.apply_history(
                    details.input_text, details.output_text, details.status, details.summary
                )
            elif self._tool_history_again or not (view.completed or view.unreported):
                # A queued read, or the call's own completion read, may still find it.
                view.set_result_notice("")
                continue
            elif self.active_run is not None:
                # Wisp may store the result after reporting the completion, and
                # the run-end read is still coming: a miss is never final while
                # the run is live, so the entry stays available to that read.
                if not view.result_retried:
                    view.result_retried = retry = True
                view.set_result_notice("")
                continue
            elif self._tool_history_post_run_reads < len(TOOL_HISTORY_POST_RUN_DELAYS):
                # The final checkpoint may commit just after run.completed;
                # keep waiting instead of declaring the result missing.
                view.set_result_notice("")
                post_run_retry = True
                continue
            else:
                view.set_result_notice("Result not in stored history.")
            view.result_checked = True
            view.refresh_entry()
        if retry:
            self.set_timer(1.0, self._request_tool_history)
        if post_run_retry:
            delay = TOOL_HISTORY_POST_RUN_DELAYS[self._tool_history_post_run_reads]
            self._tool_history_post_run_reads += 1
            self.set_timer(delay, self._request_tool_history)
        self._queue_sync()

    def _request_tool_history(self, *, force_after_current: bool = False) -> None:
        targets = self._tool_targets()
        session_id = self.session_id
        if not targets or session_id is None:
            return
        if self._tool_history_token is not None:
            self._tool_history_again |= force_after_current or any(
                view not in self._tool_history_targets for view in targets
            )
            return
        token = object()
        epoch = self._chat_epoch
        self._tool_history_token = token
        self._tool_history_targets = targets
        for view in targets:
            view.set_result_notice("Loading stored result…")

        def work() -> None:
            try:
                payload = self.client.request(
                    "GET", f"/v1/sessions/{session_id}/history", timeout=5.0
                )
                messages = parse_history(payload)
            except ClientError as error:
                code = safe_code(str(error), "history_unavailable")
            except HistoryError:
                code = "unexpected_history_shape"
            except Exception:
                code = "history_unavailable"
            else:
                self.call_from_thread(
                    self._tool_history_loaded, token, session_id, epoch, targets, messages
                )
                return
            self.call_from_thread(
                self._tool_history_failed, token, session_id, epoch, targets, code
            )

        self._run_threaded(
            work, group="tool-history", on_done=lambda: self._tool_history_done(token)
        )

    def _tool_history_current(self, token: object, session_id: str, epoch: int) -> bool:
        return (
            self._tool_history_token is token
            and self.session_id == session_id
            and self._chat_epoch == epoch
        )

    def _tool_history_loaded(
        self,
        token: object,
        session_id: str,
        epoch: int,
        targets: tuple[ToolCallView, ...],
        messages: list[ChatMessage],
    ) -> None:
        if self._tool_history_current(token, session_id, epoch):
            self._apply_tool_history(messages, targets)

    def _tool_history_failed(
        self,
        token: object,
        session_id: str,
        epoch: int,
        targets: tuple[ToolCallView, ...],
        code: str,
    ) -> None:
        if self._tool_history_current(token, session_id, epoch):
            for view in targets:
                if view in self._chat_items and view.output_text is None:
                    if self.active_run is not None:
                        # Not final while the run is live; the run-end read retries.
                        view.set_result_notice("")
                        continue
                    # A running call still gets its own read on completion.
                    finished = view.completed or view.unreported
                    view.result_checked = finished
                    view.set_result_notice(f"result unavailable: {code}" if finished else "")
            self._queue_sync()

    def _tool_history_done(self, token: object) -> None:
        if self._tool_history_token is not token:
            return
        again = self._tool_history_again
        self._tool_history_token = None
        self._tool_history_targets = ()
        self._tool_history_again = False
        if again:
            self._request_tool_history()

    def _begin_stream(self, initial: str = "") -> StreamView:
        widget = ChatItem("assistant", "")
        self._mount_chat(widget)
        view = StreamView(self, widget, self._queue_sync)
        view.set_text(initial)
        return view

    def _show_stream_text(self, text: str) -> None:
        """Show ``text`` as the current answer (appending when it continues it)."""
        if self._stream_view is None:
            if self._reattached_checkpoint is not None and not self._replay_label_shown:
                self._replay_label_shown = True
                self._show_line("Current run replay — may overlap the saved checkpoint.", "tool")
            self._stream_view = self._begin_stream()
        self._stream_view.set_text(text)
