"""Chat transcript: mounting and trimming items, tool entries and their history."""

from __future__ import annotations

from typing import Any

from textual.await_remove import AwaitRemove
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widget import AwaitMount, Widget
from textual.widgets import Collapsible, Markdown, Static

from .app_base import AppBase
from .chat_view import AnswerMarkdown, ChatScroll, ResumeFollow, StreamView
from .client import ClientError, safe_code
from .history import ChatMessage, HistoryError, parse_history, tool_history_results
from .text import safe_text, terminal_text
from .tool_data import detail_text
from .tool_view import ToolCallView


class ChatLayer(AppBase):
    def _scroll_down(self) -> None:
        self.query_one("#chat", ChatScroll).follow_latest()

    def on_chat_scroll_follow_changed(self, event: ChatScroll.FollowChanged) -> None:
        self.query_one("#resume-follow", ResumeFollow).display = not self.query_one(
            "#chat", ChatScroll
        ).following

    def on_resume_follow_requested(self, event: ResumeFollow.Requested) -> None:
        self.action_resume_follow()

    def action_resume_follow(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.query_one("#chat", ChatScroll).resume_follow()

    def _mount_chat(self, widget: Widget, before: Widget | None = None) -> AwaitMount:
        """Mount in display order, dropping the oldest beyond MAX_CHAT_ITEMS."""
        self.query_one("#chat", ChatScroll).preserve_reading_position()
        if before is not None and before in self._chat_items:
            mounted = self.query_one("#chat", VerticalScroll).mount(widget, before=before)
            self._chat_items.insert(self._chat_items.index(before), widget)
        else:
            mounted = self.query_one("#chat", VerticalScroll).mount(widget)
            self._chat_items.append(widget)
        removed = self._trim_chat()
        self.query_one("#chat", ChatScroll).restore_after_update(mounted, *removed)
        return mounted

    def _trim_chat(self) -> list[AwaitRemove]:
        streaming = self._stream_view.widget if self._stream_view is not None else None
        removed = 0
        removals: list[AwaitRemove] = []
        while len(self._chat_items) > self.MAX_CHAT_ITEMS:
            oldest = self._chat_items.popleft()
            if oldest is streaming:
                # Never drop the answer that is still being written.
                second = self._chat_items.popleft()
                self._chat_items.appendleft(oldest)
                oldest = second
            removals.append(oldest.remove())
            self._tool_calls = {
                key: view for key, view in self._tool_calls.items() if view is not oldest
            }
            removed += 1
        if removed:
            self._trimmed_count += removed
            self._update_trim_marker()
        return removals

    def _update_trim_marker(self) -> None:
        count = self._trimmed_count
        text = (
            f"\u2191 {count} earlier item{'s' if count != 1 else ''} not shown "
            "\u2014 Wisp keeps the full session history."
        )
        # Reuse a marker whose mount is still pending, too, so rapid replay
        # updates cannot create several markers before the first is mounted.
        if self._trim_marker is not None:
            self._trim_marker.update(text)
            return
        # remove_children() is asynchronous; a prior marker may still be in
        # the DOM during a reset. A class avoids colliding fixed widget IDs.
        marker = Static(text, classes="system chat-trimmed", markup=False)
        self._trim_marker = marker
        chat = self.query_one("#chat", VerticalScroll)
        if self._chat_items:
            chat.mount(marker, before=self._chat_items[0])
        else:
            chat.mount(marker)

    def _reset_chat(self, hidden: int = 0) -> None:
        """Remove every chat item; optionally note ``hidden`` older items."""
        self._chat_epoch += 1
        self._reattached_checkpoint = None
        self._pending_view_history = None
        self.query_one("#chat", ChatScroll).resume_follow()
        self.query_one("#chat", VerticalScroll).remove_children()
        self._chat_items.clear()
        self._tool_calls.clear()
        self._tool_history_token = None
        self._tool_history_targets = ()
        self._tool_history_again = False
        # A still-running answer continues in a fresh widget (current segment so far).
        self._stream_view = None
        self._trim_marker = None
        self._trimmed_count = hidden
        if hidden:
            self._update_trim_marker()

    def transcript(self) -> list[tuple[str, str]]:
        """(kind, text) for each shown chat item; the inline area is not scrollback."""
        items: list[tuple[str, str]] = []
        if self._trim_marker is not None:
            items.append(("system", str(self._trim_marker.content)))
        streaming = self._stream_view
        for widget in self._chat_items:
            if isinstance(widget, Markdown):
                text = (
                    streaming.target
                    if streaming is not None and streaming.widget is widget
                    else widget.source
                )
                items.append(("assistant", terminal_text(text)))
            elif isinstance(widget, ToolCallView):
                items.append(("tool", f"{widget.title}\n{widget.status_text}"))
            elif isinstance(widget, Static):
                kind = next(
                    (cls for cls in ("user", "error", "tool") if widget.has_class(cls)), "system"
                )
                items.append((kind, str(widget.content)))
        return items

    def _show_line(self, text: str, cls: str = "system") -> None:
        self._mount_chat(Static(safe_text(text), classes=cls, markup=False))
        self._scroll_down()

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
        self.query_one("#chat", ChatScroll).preserve_reading_position()
        if view is not None:
            view.update_event(event, completed=completed)
            self.query_one("#chat", ChatScroll).restore_after_update()
        else:
            view = ToolCallView(event, completed=completed, historical=historical)
            view.session_id = self.session_id
            if terminal:
                view.is_run_summary = True
                view.refresh_entry()
            before = None
            if self._stream_view is not None:
                if historical or terminal:
                    before = self._stream_view.widget
                else:
                    # A live call ends this text segment; later text goes below it.
                    self._stream_view.flush()
                    self._stream_view = None
                    self._answer_segment_start = len(self._answer_text)
            self._mount_chat(view, before=before)
            if key and view in self._chat_items:
                self._tool_calls[key] = view
        if input_text is not None:
            # Checkpoint arguments have already been bounded/formatted.
            view.apply_history(input_text=input_text, summary=summary)
        self._scroll_down()
        if completed and not historical:
            # Deferred so parallel completions share one history read.
            self.call_after_refresh(self._request_tool_history)
        return view

    def _add_message(self, message: ChatMessage) -> None:
        if message.role == "assistant":
            if message.text.strip():
                self._mount_chat(AnswerMarkdown(message.text, classes="assistant"))
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
            view.apply_history(
                output_text=detail_text(message.text),
                status=message.tool_status,
                pruned=message.tool_output_pruned,
            )
        else:
            if message.role == "user":
                self._tool_calls.clear()
            self._mount_chat(
                Static(terminal_text(message.text), classes=message.role, markup=False)
            )
        self._scroll_down()

    def _tool_targets(self) -> tuple[ToolCallView, ...]:
        return tuple(
            view
            for view in self._chat_items
            if isinstance(view, ToolCallView)
            and view.session_id == self.session_id
            and view.call_id
            and view.output_text is None
        )

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if (
            isinstance(event.collapsible, ToolCallView)
            and event.collapsible.call_id
            and event.collapsible.output_text is None
        ):
            self._request_tool_history(force_after_current=True)

    def _apply_tool_history(
        self, messages: list[ChatMessage], targets: tuple[ToolCallView, ...]
    ) -> None:
        chat = self.query_one("#chat", ChatScroll)
        if targets:
            chat.preserve_reading_position()
        matched, ambiguous = tool_history_results(messages, {view.call_id for view in targets})
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
                    details.input_text,
                    details.output_text,
                    details.status,
                    details.pruned,
                    details.summary,
                )
            else:
                view.set_result_notice(
                    "Result not in stored history yet. Collapse and expand to retry."
                )
        if targets:
            chat.restore_after_update()

    def _request_tool_history(self, *, force_after_current: bool = False) -> None:
        targets = self._tool_targets()
        session_id = self.session_id
        if not targets or session_id is None or self._history_refresh_token is not None:
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
                    view.set_result_notice(f"{code} · collapse and expand to retry.")

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
        widget = AnswerMarkdown(classes="assistant")
        view = StreamView(self, widget, self._mount_chat(widget), self._scroll_down)
        view.set_text(initial)
        self._scroll_down()
        return view

    def _show_stream_text(self, text: str) -> None:
        """Show ``text`` as the current answer (appending when it continues it)."""
        if self._stream_view is None:
            if self._reattached_checkpoint is not None and not self._replay_label_shown:
                self._replay_label_shown = True
                self._show_line("Current run replay — may overlap the saved checkpoint.", "tool")
            self._stream_view = self._begin_stream()
        self._stream_view.set_text(text)
