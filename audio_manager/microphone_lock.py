"""Microphone volume lock.

The original add-on rewrote the default recording device's volume every 100
milliseconds and swallowed every exception. On Linux that would mean ten
subprocesses per second, so this implementation instead reacts to graph changes
that the monitor already delivers, corrects only real drift, and keeps at most
one correction in flight. A slower periodic check retries corrections that were
skipped because another was in flight or failures had started a cooldown.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from gi.repository import GLib

from .backend import AudioBackend, OperationResult
from .models import AudioNode, AudioSnapshot

__all__ = ["MicrophoneLock", "TOLERANCE_PERCENT"]

# Corrections are only written when the observed level differs from the desired
# level by more than half a percentage point, so that rounding cannot start a
# write loop.
TOLERANCE_PERCENT = 0.5

_MAX_FAILURES = 3
_COOLDOWN_SECONDS = 30.0


class MicrophoneLock:
    """Keeps the default recording device at a chosen volume."""

    def __init__(
        self,
        backend: AudioBackend,
        settings_reader: Callable[[str, Any], Any],
        settings_writer: Callable[[str, Any], None],
        interval_provider: Callable[[], int],
    ) -> None:
        self._backend = backend
        self._read_setting = settings_reader
        self._write_setting = settings_writer
        self._interval_provider = interval_provider
        self._timer_id: int | None = None
        self._busy = False
        self._failures = 0
        self._cooldown_until = 0.0
        self._stopped = False

    # -- state -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Returns True if the lock is active."""

        return bool(self._read_setting("microphone-lock-enabled", False))

    @property
    def desired_percent(self) -> int:
        """Returns the volume the lock maintains, as a percentage."""

        value = self._read_setting("microphone-lock-volume-percent", 50)
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return 50

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Starts the safety timer if the lock is enabled."""

        if self._stopped or not self.enabled or self._timer_id is not None:
            return
        interval = max(200, min(2000, int(self._interval_provider())))
        self._timer_id = GLib.timeout_add(interval, self._on_timer)

    def stop(self) -> None:
        """Stops the timer and prevents further corrections."""

        self._stopped = True
        self._remove_timer()

    def _remove_timer(self) -> None:
        """Removes the GLib timeout source if one is installed."""

        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    # -- correction --------------------------------------------------------

    def toggle(self, snapshot: AudioSnapshot) -> str:
        """Toggles the lock, capturing the current level when enabling."""

        if self.enabled:
            self._write_setting("microphone-lock-enabled", False)
            self._remove_timer()
            return "Microphone volume unlocked."

        source = snapshot.default_source()
        if source is None:
            return "There is no default recording device."

        if source.volume is not None:
            self._write_setting("microphone-lock-volume-percent", source.volume.percent)
        self._write_setting("microphone-lock-enabled", True)
        self._failures = 0
        self._cooldown_until = 0.0
        self.start()
        return f"Microphone volume locked at {self.desired_percent} percent."

    def on_snapshot(self, snapshot: AudioSnapshot) -> None:
        """Corrects drift when the graph changes.

        A changed default recording device inherits the locked volume, which
        matches the behavior of the original add-on.
        """

        if self._stopped or not self.enabled:
            return
        self._correct(snapshot)

    def _on_timer(self) -> bool:
        """Runs the periodic safety check."""

        if self._stopped or not self.enabled:
            self._timer_id = None
            return False
        self._correct(self._backend.snapshot())
        return True

    def _correct(self, snapshot: AudioSnapshot) -> None:
        """Writes the desired volume when the observed value has drifted."""

        if self._busy or time.monotonic() < self._cooldown_until:
            return

        source = snapshot.default_source()
        if source is None or source.volume is None:
            return

        desired = self.desired_percent
        if abs(source.volume.percent - desired) <= TOLERANCE_PERCENT:
            return

        self._busy = True
        self._backend.set_volume([source], desired / 100.0, 1.0, self._on_corrected)

    def _on_corrected(self, result: OperationResult) -> None:
        """Records the outcome of one correction without speaking."""

        self._busy = False
        if result.error is None:
            self._failures = 0
            return

        # Repeated failures back off instead of generating speech on every
        # check. The user is told when they invoke a command, not by surprise.
        self._failures += 1
        if self._failures >= _MAX_FAILURES:
            self._cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
            self._failures = 0

    def describe_target(self, node: AudioNode | None) -> str:
        """Returns a short status string for the lock, used by reports."""

        if not self.enabled:
            return ""
        name = node.display_name if node is not None else "the default recording device"
        return f"Microphone volume locked at {self.desired_percent} percent on {name}."
