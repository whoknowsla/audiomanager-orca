"""Selection state and list movement.

Selections are stored as stable keys, never as list indexes, so that hotplug,
reordering, and PipeWire restarts cannot silently move the user's target onto a
different object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence, TypeVar

from .models import ApplicationGroup, AudioNode

__all__ = ["Move", "Navigator", "SelectionState", "TargetKind", "step"]

TargetKind = Literal["sink", "source", "application"]

Item = TypeVar("Item", AudioNode, ApplicationGroup)


@dataclass
class SelectionState:
    """The user's current selections, expressed as stable keys."""

    active_kind: TargetKind | None = None
    sink_key: str | None = None
    source_key: str | None = None
    application_key: str | None = None
    application_sink_key: str | None = None
    application_source_key: str | None = None


@dataclass(frozen=True)
class Move:
    """The outcome of a navigation request."""

    item: AudioNode | ApplicationGroup | None = None
    at_boundary: bool = False
    previous_lost: bool = False
    empty: bool = False


def step(
    items: Sequence[Item],
    current_key: str | None,
    delta: int,
    wrap: bool,
) -> Move:
    """Moves delta positions through items relative to current_key.

    When the previously selected item is gone, the nearest deterministic item is
    chosen and the caller is told, so that it can explain the change instead of
    silently acting on a different object.
    """

    if not items:
        return Move(empty=True)

    keys = [item.stable_key for item in items]
    if current_key is None:
        index = 0 if delta > 0 else len(items) - 1
        return Move(item=items[index])

    if current_key not in keys:
        index = 0 if delta > 0 else len(items) - 1
        return Move(item=items[index], previous_lost=True)

    index = keys.index(current_key) + delta
    if wrap:
        return Move(item=items[index % len(items)])

    if index < 0:
        return Move(item=items[0], at_boundary=True)
    if index >= len(items):
        return Move(item=items[-1], at_boundary=True)
    return Move(item=items[index])


class Navigator:
    """Holds the current selections and applies movement to snapshots."""

    def __init__(self) -> None:
        self.state = SelectionState()

    def clear(self) -> None:
        """Forgets every selection, for example after a PipeWire restart."""

        self.state = SelectionState()

    def select_sink(self, node: AudioNode | None) -> None:
        """Records node as the current playback device target."""

        if node is None:
            self.state.sink_key = None
            return
        self.state.sink_key = node.stable_key
        self.state.active_kind = "sink"

    def select_source(self, node: AudioNode | None) -> None:
        """Records node as the current recording device target."""

        if node is None:
            self.state.source_key = None
            return
        self.state.source_key = node.stable_key
        self.state.active_kind = "source"

    def select_application(self, group: ApplicationGroup | None) -> None:
        """Records group as the current application target."""

        if group is None:
            self.state.application_key = None
            return
        if group.stable_key != self.state.application_key:
            # Route candidate positions belong to the previously selected
            # application, so they must not carry over.
            self.state.application_sink_key = None
            self.state.application_source_key = None
        self.state.application_key = group.stable_key
        self.state.active_kind = "application"

    def move_sinks(self, sinks: Sequence[AudioNode], delta: int, wrap: bool) -> Move:
        """Moves through the playback device list."""

        move = step(sinks, self.state.sink_key, delta, wrap)
        if isinstance(move.item, AudioNode):
            self.select_sink(move.item)
        return move

    def move_sources(self, sources: Sequence[AudioNode], delta: int, wrap: bool) -> Move:
        """Moves through the recording device list."""

        move = step(sources, self.state.source_key, delta, wrap)
        if isinstance(move.item, AudioNode):
            self.select_source(move.item)
        return move

    def move_applications(
        self,
        applications: Sequence[ApplicationGroup],
        delta: int,
        wrap: bool,
    ) -> Move:
        """Moves through the application list."""

        move = step(applications, self.state.application_key, delta, wrap)
        if isinstance(move.item, ApplicationGroup):
            self.select_application(move.item)
        return move

    def move_application_output(self, sinks: Sequence[AudioNode], delta: int) -> Move:
        """Moves through the playback route candidates for the application.

        Route candidate lists wrap, matching the original add-on.
        """

        move = step(sinks, self.state.application_sink_key, delta, wrap=True)
        if isinstance(move.item, AudioNode):
            self.state.application_sink_key = move.item.stable_key
        return move

    def move_application_input(self, sources: Sequence[AudioNode], delta: int) -> Move:
        """Moves through the capture route candidates for the application."""

        move = step(sources, self.state.application_source_key, delta, wrap=True)
        if isinstance(move.item, AudioNode):
            self.state.application_source_key = move.item.stable_key
        return move

    def set_application_route(self, node: AudioNode) -> None:
        """Records the route candidate that was applied to the application."""

        if node.is_sink:
            self.state.application_sink_key = node.stable_key
        elif node.is_source:
            self.state.application_source_key = node.stable_key
