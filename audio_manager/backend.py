"""Backend contract shared by every audio backend implementation.

The command layer depends only on this interface. The phase-one implementation
in :mod:`cli_backend` drives ``pw-dump``, ``wpctl``, and ``pw-metadata``; a
future WirePlumber GObject backend can satisfy the same protocol without any
change above it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, runtime_checkable

from .errors import AudioManagerError
from .models import AudioNode, AudioSnapshot, VolumeState

__all__ = [
    "AudioBackend",
    "BackendCapabilities",
    "OperationResult",
    "ResultCallback",
]

ResultCallback = Callable[["OperationResult"], None]


@dataclass(frozen=True)
class BackendCapabilities:
    """What the running system actually supports."""

    connected: bool = False
    can_read_graph: bool = False
    can_set_volume: bool = False
    can_set_default: bool = False
    can_move_streams: bool = False
    missing_commands: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class OperationResult:
    """The outcome of one backend operation.

    ``attempted`` and ``succeeded`` are counts of individual nodes, so that an
    application whose streams partially refused an operation can be reported
    accurately rather than as a flat success or failure.
    """

    attempted: int = 0
    succeeded: int = 0
    error: AudioManagerError | None = None
    volume: VolumeState | None = None
    detail: str = ""
    nodes: tuple[AudioNode, ...] = ()

    @property
    def partial(self) -> bool:
        """Returns True if some, but not all, nodes succeeded."""

        return 0 < self.succeeded < self.attempted

    @classmethod
    def failure(cls, error: AudioManagerError, attempted: int = 1) -> OperationResult:
        """Returns a result representing a complete failure."""

        return cls(attempted=attempted, succeeded=0, error=error)


@runtime_checkable
class AudioBackend(Protocol):
    """The operations the command layer needs from an audio backend."""

    def start(
        self,
        on_change: Callable[[AudioSnapshot], None],
        on_disconnect: Callable[[AudioManagerError], None],
    ) -> None:
        """Starts monitoring the audio graph."""

    def close(self) -> None:
        """Stops all workers and child processes without blocking."""

    def capabilities(self) -> BackendCapabilities:
        """Returns what this backend can currently do."""

    def snapshot(self) -> AudioSnapshot:
        """Returns the most recent immutable graph snapshot."""

    def get_volume(self, node: AudioNode, done: ResultCallback) -> None:
        """Reads the authoritative volume state of node."""

    def set_volume(
        self,
        nodes: Sequence[AudioNode],
        value: float,
        limit: float,
        done: ResultCallback,
    ) -> None:
        """Sets an absolute volume on every node."""

    def change_volume(
        self,
        nodes: Sequence[AudioNode],
        delta_percent: int,
        limit: float,
        done: ResultCallback,
    ) -> None:
        """Steps the volume of every node by delta_percent percentage points."""

    def set_mute(
        self,
        nodes: Sequence[AudioNode],
        muted: bool,
        done: ResultCallback,
    ) -> None:
        """Sets the mute state of every node."""

    def set_default(self, node: AudioNode, done: ResultCallback) -> None:
        """Makes node the default sink or source."""

    def move_streams(
        self,
        streams: Sequence[AudioNode],
        target: AudioNode,
        done: ResultCallback,
    ) -> None:
        """Routes every stream to target."""

    def clear_stream_targets(
        self,
        streams: Sequence[AudioNode],
        done: ResultCallback,
    ) -> None:
        """Returns every stream to policy-selected routing."""

    def run_background(
        self,
        work: Callable[[], OperationResult],
        done: ResultCallback,
    ) -> None:
        """Runs work on the backend's serialized worker thread.

        This exists so that command code can keep blocking work such as a
        ``/proc`` process-tree walk off Orca's main thread without owning a
        thread of its own.
        """
