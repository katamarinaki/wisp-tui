"""Transcript scrolling that leaves the reader in control during streaming."""
from __future__ import annotations

import asyncio
from typing import Awaitable

from textual import events
from textual.actions import SkipAction
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


class ChatScroll(VerticalScroll):
    following = reactive(True)

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self._reading_position: list[tuple[Widget, float]] | None = None
        self._layout_revision = 0
        self._pending_layout: list[Awaitable[object]] = []

    class FollowChanged(Message):
        pass

    def watch_following(self) -> None:
        self.post_message(self.FollowChanged())

    def follow_latest(self) -> None:
        # Check *after* layout too: a wheel/key event can pause following while
        # a Markdown render's scroll callback is still queued.
        self.call_after_refresh(self._follow_after_refresh)

    def _follow_after_refresh(self) -> None:
        if self.following:
            self.scroll_end(animate=False, immediate=True, x_axis=False)

    def resume_follow(self) -> None:
        self._reading_position = None
        self.following = True
        self.follow_latest()

    def preserve_reading_position(self) -> None:
        """Keep a surviving visible item in place when tools/trim shift rows."""
        if self.following or self._reading_position is not None:
            return
        self._reading_position = [
            (child, child.virtual_region.y - self.scroll_y)
            for child in self.children
            if child.virtual_region.bottom > self.scroll_y and child.region.height
        ]

    def restore_after_update(self, *updates: Awaitable[object]) -> None:
        if self._reading_position is None:
            return
        self._layout_revision += 1
        revision = self._layout_revision
        self._pending_layout.extend(updates)
        pending = tuple(self._pending_layout)

        async def restore() -> None:
            # Mount/removal completes asynchronously. An early refresh can
            # still have the old geometry, so wait for every pending mutation.
            await asyncio.gather(*pending)
            if revision == self._layout_revision:
                self._pending_layout.clear()
                self.call_after_refresh(self._restore_reading_position, revision)

        self.run_worker(restore(), group="reading-position")

    def _restore_reading_position(self, revision: int) -> None:
        if revision != self._layout_revision:
            return
        position, self._reading_position = self._reading_position, None
        if self.following or position is None:
            return
        for child, offset in position:
            if child.parent is self and child.is_attached:
                self.scroll_to(y=child.virtual_region.y - offset, animate=False, immediate=True)
                break

    def on_resize(self) -> None:
        self.follow_latest()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._reading_position = None
        if not event.ctrl and not event.shift and self.max_scroll_y > 0:
            self.following = False

    def _resume_at_end(self) -> None:
        if not self.following and self.scroll_y >= self.max_scroll_y:
            self.resume_follow()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._reading_position = None
        if not event.ctrl and not event.shift:
            # The base handler scrolls after this one, without animation.
            self.call_after_refresh(self._resume_at_end)

    def on_key(self) -> None:
        self._reading_position = None

    def action_scroll_up(self) -> None:
        self._reading_position = None
        if self.max_scroll_y > 0:
            self.following = False
        super().action_scroll_up()

    def action_page_up(self) -> None:
        self._reading_position = None
        if self.max_scroll_y > 0:
            self.following = False
        super().action_page_up()

    def action_scroll_home(self) -> None:
        self._reading_position = None
        if self.max_scroll_y > 0:
            self.following = False
        super().action_scroll_home()

    def action_scroll_down(self) -> None:
        self._reading_position = None
        if not self.allow_vertical_scroll:
            raise SkipAction()
        # An animated scroll that does not move never calls on_complete.
        self._resume_at_end()
        self.scroll_down(on_complete=self._resume_at_end)

    def action_page_down(self) -> None:
        self._reading_position = None
        if not self.allow_vertical_scroll:
            raise SkipAction()
        self._resume_at_end()
        self.scroll_page_down(on_complete=self._resume_at_end)

    def action_scroll_end(self) -> None:
        self.resume_follow()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        # Inspecting a tool via Tab must not be undone by the next delta.
        self._reading_position = None
        self.following = False


class ResumeFollow(Static):
    """A transparent, clickable hint; F4 is available from the composer too."""

    class Requested(Message):
        pass

    def __init__(self) -> None:
        super().__init__("Follow paused · F4 / click to resume", id="resume-follow", markup=False)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Requested())
