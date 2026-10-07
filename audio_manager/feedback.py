"""User-facing messages and boundary feedback.

Commands that need a selection stay silent when nothing is selected. No
device, disconnected, timed out, permission denied, and stale-target cases
all have their own message.

The extension API does not yet support extension-provided localization, so the
strings here are English until that Orca limitation changes.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Sequence

from .backend import OperationResult
from .errors import user_message_for
from .models import ApplicationGroup, AudioNode, VolumeState

__all__ = [
    "MSG_AMBIGUOUS_WINDOW",
    "MSG_NO_APPLICATIONS",
    "MSG_NO_PLAYBACK_DEVICES",
    "MSG_NO_RECORDING_DEVICES",
    "Presenter",
    "describe_application",
    "describe_device",
    "result_message",
    "volume_message",
]

MSG_NO_PLAYBACK_DEVICES = "No playback devices are available."
MSG_NO_RECORDING_DEVICES = "No recording devices are available."
MSG_NO_APPLICATIONS = "No audio applications are running."
MSG_TARGET_GONE = "The previously selected item is no longer available."
MSG_AMBIGUOUS_WINDOW = "The current window's audio could not be identified safely."
MSG_NO_WINDOW_AUDIO = "The current window is not playing audio."
MSG_NO_MOVABLE_STREAM = "This application has no movable playback stream."
MSG_NO_MOVABLE_CAPTURE = "This application has no movable recording stream."
MSG_FOLLOWS_DEFAULT = "target follows the system default"

_BOUNDARY_START_FREQUENCY = 440
_BOUNDARY_END_FREQUENCY = 880
_BOUNDARY_DURATION = 0.08


def _percent_text(volume: VolumeState | None) -> str:
    """Returns the spoken volume fragment for a volume state."""

    if volume is None:
        return "volume unavailable"
    return f"volume {volume.percent} percent"


def describe_device(node: AudioNode) -> str:
    """Returns the spoken description of a playback or recording device.

    For example: "Muted, default playback device, USB Headset; volume 42
    percent."
    """

    parts: list[str] = []
    if node.volume is not None and node.volume.muted:
        parts.append("Muted")
    if node.is_default:
        parts.append("default playback device" if node.is_sink else "default recording device")
    prefix = ", ".join(parts)
    body = f"{node.display_name}; {_percent_text(node.volume)}"
    return f"{prefix}, {body}" if prefix else body


def _mixed_volume_text(nodes: Sequence[AudioNode]) -> str:
    """Returns the volume fragment for a set of nodes."""

    states = [node.volume for node in nodes if node.volume is not None]
    if not states:
        return "volume unavailable"

    percents = sorted({state.percent for state in states})
    if len(percents) == 1:
        return f"volume {percents[0]} percent"
    return f"mixed, range {percents[0]} to {percents[-1]} percent"


def describe_application(
    group: ApplicationGroup,
    playback_target: str | None = None,
    capture_target: str | None = None,
) -> str:
    """Returns the spoken description of an application group."""

    directions = []
    if group.has_playback:
        directions.append("playback")
    if group.has_capture:
        directions.append("recording")

    parts = [group.display_name]
    if directions:
        parts.append(" and ".join(directions))

    nodes = group.volume_nodes()
    if any(node.volume is not None and node.volume.muted for node in nodes):
        parts.append("muted")
    parts.append(_mixed_volume_text(nodes))

    if group.has_playback:
        parts.append(f"playback through {playback_target or MSG_FOLLOWS_DEFAULT}")
    if group.has_capture:
        parts.append(f"recording through {capture_target or MSG_FOLLOWS_DEFAULT}")

    return "; ".join(parts)


def volume_message(volume: VolumeState | None, name: str, maximum_percent: int) -> str:
    """Returns the spoken result of a volume change."""

    if volume is None:
        return f"{name}; volume changed"

    percent = volume.percent
    if percent <= 0:
        return f"{percent} percent, minimum volume, {name}"
    if percent >= maximum_percent:
        return f"{percent} percent, maximum volume, {name}"
    return f"{percent} percent, {name}"


def result_message(result: OperationResult, success: str) -> str:
    """Returns the message for an operation result."""

    if result.error is not None:
        return user_message_for(result.error)
    if result.partial:
        refused = result.attempted - result.succeeded
        refusal = "one stream" if refused == 1 else f"{refused} streams"
        return (
            f"Changed {result.succeeded} of {result.attempted} streams; "
            f"{refusal} refused the operation."
        )
    return success


class Presenter:
    """Presents messages and boundary feedback through Orca's controller."""

    def __init__(
        self,
        controller: Any,
        settings_reader: Callable[[str, Any], Any] | None = None,
        is_shutting_down: Callable[[], bool] | None = None,
    ) -> None:
        self._controller = controller
        self._settings_reader = settings_reader or (lambda _key, default: default)
        self._is_shutting_down = is_shutting_down or (lambda: False)

    def present(self, message: str) -> None:
        """Speaks and brailles a message, unless Orca is shutting down."""

        if self._is_shutting_down() or not message:
            return
        self._controller.present_message_internal(message)

    def error(self, error: BaseException) -> None:
        """Presents the short user message for an error."""

        self.present(user_message_for(error))

    def boundary(self, forward: bool) -> None:
        """Signals that a clamped list hit its first or last item."""

        if self._is_shutting_down():
            return
        if not self._settings_reader("boundary-sounds", True):
            return

        folder = self._settings_reader("boundary-sound-folder", "")
        if folder:
            filename = os.path.join(str(folder), "right.wav" if forward else "left.wav")
            if os.path.isfile(filename):
                self._controller.play_sound_file_internal(filename)
                return

        frequency = _BOUNDARY_END_FREQUENCY if forward else _BOUNDARY_START_FREQUENCY
        self._controller.play_tone_internal(_BOUNDARY_DURATION, frequency, volume=0.5)
