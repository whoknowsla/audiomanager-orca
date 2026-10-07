"""User intents, expressed independently of PipeWire process details.

Every method here is a complete command: it validates the current target against
the newest snapshot, starts a bounded asynchronous operation, presents its own
errors, and returns immediately so that Orca's main loop is never blocked.
"""

from __future__ import annotations

import time
from functools import partial
from typing import Any, Callable, Sequence

from . import feedback, process_matcher
from .applications import group_key_for
from .backend import AudioBackend, OperationResult
from .errors import AmbiguousApplication, AudioManagerError
from .feedback import Presenter
from .microphone_lock import MicrophoneLock
from .models import ApplicationGroup, AudioNode, AudioSnapshot
from .navigators import Navigator

__all__ = ["AudioCommands"]

_DOUBLE_PRESS_SECONDS = 0.6
_SHARED_SPEECH_HINTS = ("speech-dispatcher", "speechd", "spd-say")


class AudioCommands:
    """Implements every command the extension registers."""

    def __init__(
        self,
        backend: AudioBackend,
        presenter: Presenter,
        settings_reader: Callable[[str, Any], Any],
        controller: Any,
        microphone_lock: MicrophoneLock,
    ) -> None:
        self._backend = backend
        self._presenter = presenter
        self._read_setting = settings_reader
        self._controller = controller
        self._microphone_lock = microphone_lock
        self._navigator = Navigator()
        self._volume_sequence = 0
        self._last_route_reset = 0.0
        self._reset_volume_result: OperationResult | None = None

    # -- settings ----------------------------------------------------------

    def _int_setting(self, key: str, default: int, low: int, high: int) -> int:
        """Returns a bounded integer preference."""

        try:
            return max(low, min(high, int(self._read_setting(key, default))))
        except (TypeError, ValueError):
            return default

    def _bool_setting(self, key: str, default: bool) -> bool:
        """Returns a boolean preference."""

        return bool(self._read_setting(key, default))

    @property
    def _step(self) -> int:
        """Returns the configured volume step in percentage points."""

        return self._int_setting("volume-step-percent", 1, 1, 20)

    @property
    def _maximum_percent(self) -> int:
        """Returns the configured maximum volume percentage."""

        return self._int_setting("maximum-volume-percent", 100, 100, 150)

    @property
    def _limit(self) -> float:
        """Returns the maximum volume on the wpctl scale."""

        return self._maximum_percent / 100.0

    @property
    def _wrap(self) -> bool:
        """Returns True if the main device lists should wrap."""

        return self._bool_setting("wrap-device-lists", False)

    @property
    def _include_monitors(self) -> bool:
        """Returns True if monitor sources are shown as recording devices."""

        return self._bool_setting("include-monitor-sources", False)

    @property
    def _include_screen_reader(self) -> bool:
        """Returns True if screen-reader streams appear as applications."""

        return self._bool_setting("include-screen-reader-streams", False)

    # -- shared helpers ----------------------------------------------------

    def _snapshot(self) -> AudioSnapshot | None:
        """Returns the newest snapshot, or None after reporting the problem."""

        snapshot = self._backend.snapshot()
        if not snapshot.connected:
            capabilities = self._backend.capabilities()
            if capabilities.missing_commands:
                missing = ", ".join(capabilities.missing_commands)
                self._presenter.present(f"These PipeWire tools are missing: {missing}.")
            else:
                self._presenter.present("PipeWire is not available.")
            return None
        return snapshot

    def _present_move(self, move_message: str, previous_lost: bool) -> None:
        """Presents a navigation result, explaining a lost selection."""

        if previous_lost:
            self._presenter.present(f"{feedback.MSG_TARGET_GONE} {move_message}")
        else:
            self._presenter.present(move_message)

    def _announce(self, success: str, result: OperationResult) -> None:
        """Presents success, or why the operation did not fully succeed."""

        self._presenter.present(feedback.result_message(result, success))

    def _selected_device(self, snapshot: AudioSnapshot) -> AudioNode | None:
        """Returns the selected playback or recording device, if it still exists."""

        state = self._navigator.state
        if state.active_kind == "sink":
            return snapshot.node_by_key(state.sink_key)
        if state.active_kind == "source":
            return snapshot.node_by_key(state.source_key)
        return None

    def _current_target(
        self,
        snapshot: AudioSnapshot,
    ) -> tuple[tuple[AudioNode, ...], str] | None:
        """Returns the nodes and label the shared commands should act on."""

        state = self._navigator.state
        if state.active_kind in ("sink", "source"):
            node = self._selected_device(snapshot)
            return ((node,), node.display_name) if node else None
        if state.active_kind == "application":
            group = snapshot.application_by_key(state.application_key)
            if group is None:
                return None
            nodes = group.volume_nodes()
            return (nodes, group.display_name) if nodes else None
        return None

    def _require_target(
        self,
        snapshot: AudioSnapshot,
    ) -> tuple[tuple[AudioNode, ...], str] | None:
        """Returns the current target or presents why there is none."""

        if self._navigator.state.active_kind is None:
            return None
        target = self._current_target(snapshot)
        if target is None:
            self._presenter.present(feedback.MSG_TARGET_GONE)
            return None
        return target

    def _current_application(self, snapshot: AudioSnapshot) -> ApplicationGroup | None:
        """Returns the selected application, or presents why there is none."""

        state = self._navigator.state
        if state.active_kind != "application" or state.application_key is None:
            return None
        group = snapshot.application_by_key(state.application_key)
        if group is None:
            self._presenter.present(feedback.MSG_TARGET_GONE)
            return None
        return group

    def _live_streams(self, snapshot: AudioSnapshot, playback_only: bool) -> tuple[AudioNode, ...]:
        """Returns the live application streams that resets may touch."""

        streams = snapshot.playback_streams
        if not playback_only:
            streams = streams + snapshot.capture_streams
        if self._include_screen_reader:
            return streams
        return tuple(node for node in streams if not node.is_screen_reader)

    @staticmethod
    def _target_label(snapshot: AudioSnapshot, stream: AudioNode) -> str | None:
        """Returns the name of the device a stream is routed to, if known."""

        if not stream.target_object:
            return None
        for node in snapshot.sinks + snapshot.sources:
            if str(node.object_serial) == stream.target_object:
                return node.display_name
            if node.node_name == stream.target_object:
                return node.display_name
        return None

    # -- device navigation -------------------------------------------------

    def next_playback_device(self) -> bool:
        """Selects the next playback device."""

        return self._move_device(1, playback=True)

    def previous_playback_device(self) -> bool:
        """Selects the previous playback device."""

        return self._move_device(-1, playback=True)

    def next_recording_device(self) -> bool:
        """Selects the next recording device."""

        return self._move_device(1, playback=False)

    def previous_recording_device(self) -> bool:
        """Selects the previous recording device."""

        return self._move_device(-1, playback=False)

    def _move_device(self, delta: int, playback: bool) -> bool:
        """Moves through the playback or recording device list."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        if playback:
            devices = snapshot.visible_sinks()
            empty_message = feedback.MSG_NO_PLAYBACK_DEVICES
        else:
            devices = snapshot.visible_sources(self._include_monitors)
            empty_message = feedback.MSG_NO_RECORDING_DEVICES
        if not devices:
            self._presenter.present(empty_message)
            return True

        if playback:
            move = self._navigator.move_sinks(devices, delta, self._wrap)
        else:
            move = self._navigator.move_sources(devices, delta, self._wrap)
        if move.at_boundary:
            self._presenter.boundary(delta > 0)
        if isinstance(move.item, AudioNode):
            self._present_move(feedback.describe_device(move.item), move.previous_lost)
        return True

    # -- application navigation -------------------------------------------

    def next_audio_application(self) -> bool:
        """Selects the next audio application."""

        return self._move_application(1)

    def previous_audio_application(self) -> bool:
        """Selects the previous audio application."""

        return self._move_application(-1)

    def _move_application(self, delta: int) -> bool:
        """Moves through the application list."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        groups = snapshot.visible_applications(self._include_screen_reader)
        if not groups:
            self._presenter.present(feedback.MSG_NO_APPLICATIONS)
            return True

        move = self._navigator.move_applications(groups, delta, self._wrap)
        if move.at_boundary:
            self._presenter.boundary(delta > 0)
        if isinstance(move.item, ApplicationGroup):
            self._present_move(
                self._describe_application(snapshot, move.item), move.previous_lost
            )
        return True

    def _describe_application(self, snapshot: AudioSnapshot, group: ApplicationGroup) -> str:
        """Returns the spoken description of an application group."""

        playback_target = next(
            (self._target_label(snapshot, node) for node in group.playback_nodes), None
        )
        capture_target = next(
            (self._target_label(snapshot, node) for node in group.capture_nodes), None
        )
        return feedback.describe_application(group, playback_target, capture_target)

    # -- volume and mute ---------------------------------------------------

    def volume_up(self) -> bool:
        """Raises the volume of the current target."""

        return self._change_volume(self._step)

    def volume_down(self) -> bool:
        """Lowers the volume of the current target."""

        return self._change_volume(-self._step)

    def _change_volume(self, delta: int) -> bool:
        """Steps the volume of the current target."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        target = self._require_target(snapshot)
        if target is None:
            return True

        nodes, label = target
        self._volume_sequence += 1
        token = self._volume_sequence
        self._backend.change_volume(
            nodes, delta, self._limit, partial(self._on_volume_changed, token, label)
        )
        return True

    def _on_volume_changed(self, token: int, label: str, result: OperationResult) -> None:
        """Announces a volume change, discarding superseded announcements."""

        if token != self._volume_sequence:
            return
        if result.error is not None:
            self._presenter.error(result.error)
            return
        self._presenter.present(
            feedback.volume_message(result.volume, label, self._maximum_percent)
        )

    def toggle_mute(self) -> bool:
        """Mutes or unmutes the current target."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        target = self._require_target(snapshot)
        if target is None:
            return True

        nodes, label = target
        muted = self._desired_mute(nodes)
        state = "Muted" if muted else "Unmuted"
        self._backend.set_mute(nodes, muted, partial(self._announce, f"{state} {label}"))
        return True

    @staticmethod
    def _desired_mute(nodes: Sequence[AudioNode]) -> bool:
        """Returns True when the group should be muted.

        Mute all members if any member is audible; otherwise unmute all.
        """

        return any(node.volume is not None and not node.volume.muted for node in nodes)

    def set_default_device(self) -> bool:
        """Makes the selected device the system default."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        if self._navigator.state.active_kind not in ("sink", "source"):
            return True

        node = self._selected_device(snapshot)
        if node is None:
            self._presenter.present(feedback.MSG_TARGET_GONE)
            return True

        role = "playback" if node.is_sink else "recording"
        self._backend.set_default(
            node, partial(self._announce, f"{node.display_name} is the default {role} device.")
        )
        return True

    # -- per-application routing ------------------------------------------

    def next_application_output(self) -> bool:
        """Routes the application's playback to the next device."""

        return self._move_application_route(1, playback=True)

    def previous_application_output(self) -> bool:
        """Routes the application's playback to the previous device."""

        return self._move_application_route(-1, playback=True)

    def next_application_input(self) -> bool:
        """Routes the application's capture to the next device."""

        return self._move_application_route(1, playback=False)

    def previous_application_input(self) -> bool:
        """Routes the application's capture to the previous device."""

        return self._move_application_route(-1, playback=False)

    def _move_application_route(self, delta: int, playback: bool) -> bool:
        """Cycles the route of the selected application's streams."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        group = self._current_application(snapshot)
        if group is None:
            return True

        streams = group.playback_nodes if playback else group.capture_nodes
        movable = tuple(node for node in streams if not node.dont_move)
        if not movable:
            self._presenter.present(
                feedback.MSG_NO_MOVABLE_STREAM if playback else feedback.MSG_NO_MOVABLE_CAPTURE
            )
            return True

        candidates = (
            snapshot.visible_sinks()
            if playback
            else snapshot.visible_sources(self._include_monitors)
        )
        if not candidates:
            self._presenter.present(
                feedback.MSG_NO_PLAYBACK_DEVICES
                if playback
                else feedback.MSG_NO_RECORDING_DEVICES
            )
            return True

        move = (
            self._navigator.move_application_output(candidates, delta)
            if playback
            else self._navigator.move_application_input(candidates, delta)
        )
        if not isinstance(move.item, AudioNode):
            return True

        target = move.item
        direction = "playback" if playback else "recording"
        self._backend.move_streams(
            movable,
            target,
            partial(
                self._announce,
                f"{group.display_name} {direction} through {target.display_name}",
            ),
        )
        return True

    # -- resets ------------------------------------------------------------

    def reset_application_routes(self) -> bool:
        """Returns every live application stream to policy-selected routing."""

        now = time.monotonic()
        repeated = (now - self._last_route_reset) <= _DOUBLE_PRESS_SECONDS
        self._last_route_reset = now
        if repeated and self._bool_setting("reset-compatibility-double-press", False):
            return self.reset_application_audio()

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        streams = self._live_streams(snapshot, playback_only=False)
        if not streams:
            self._presenter.present("There are no application audio streams to reset.")
            return True

        self._backend.clear_stream_targets(
            streams,
            partial(
                self._on_reset_done,
                "Reset the playback and recording devices of all audio applications.",
            ),
        )
        return True

    def reset_application_audio(self) -> bool:
        """Restores full volume and unmutes every live application stream."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        streams = self._live_streams(snapshot, playback_only=True)
        if not streams:
            self._presenter.present("There are no application audio streams to reset.")
            return True

        # The two writes are serialized by the backend worker. The first one
        # records its outcome instead of discarding it, so that a failed volume
        # reset is still reported even when unmuting succeeds.
        self._reset_volume_result = None
        self._backend.set_volume(
            streams, 1.0, max(1.0, self._limit), self._on_reset_volume_done
        )
        self._backend.set_mute(
            streams,
            False,
            partial(
                self._on_reset_done,
                "Reset the volume of all audio applications and unmuted them.",
            ),
        )
        return True

    def _on_reset_volume_done(self, result: OperationResult) -> None:
        """Records the outcome of the volume half of an audio reset."""

        self._reset_volume_result = result

    def _on_reset_done(self, success: str, result: OperationResult) -> None:
        """Announces the result of a reset."""

        volume_result = self._reset_volume_result
        self._reset_volume_result = None
        if result.error is None and volume_result is not None and volume_result.error is not None:
            result = volume_result
        self._presenter.present(feedback.result_message(result, success))

    # -- default device switches ------------------------------------------

    def toggle_default_output_mute(self) -> bool:
        """Mutes or unmutes the default playback device."""

        return self._toggle_default_mute(playback=True)

    def toggle_default_microphone_mute(self) -> bool:
        """Mutes or unmutes the default recording device."""

        return self._toggle_default_mute(playback=False)

    def _toggle_default_mute(self, playback: bool) -> bool:
        """Mutes or unmutes the default playback or recording device."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        node = snapshot.default_sink() if playback else snapshot.default_source()
        if node is None:
            role = "playback" if playback else "recording"
            self._presenter.present(f"There is no default {role} device.")
            return True

        muted = self._desired_mute([node])
        subject = "The sound" if playback else "The microphone"
        state = "off" if muted else "on"
        self._backend.set_mute(
            [node], muted, partial(self._announce, f"{subject} is turned {state}.")
        )
        return True

    # -- current window ----------------------------------------------------

    def toggle_current_window_mute(self) -> bool:
        """Mutes or unmutes the audio of the application in the active window.

        The match must be unambiguous. This action never falls back to muting
        the default sink, because that would silence Orca and the whole desktop.
        """

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        pid = process_matcher.active_window_pid(self._controller)
        if pid is None:
            self._presenter.present(feedback.MSG_AMBIGUOUS_WINDOW)
            return True

        streams = tuple(node for node in snapshot.playback_streams if not node.is_screen_reader)
        if not streams:
            self._presenter.present(feedback.MSG_NO_WINDOW_AUDIO)
            return True

        # The process-tree walk touches /proc, so it runs in the worker.
        self._backend.run_background(
            partial(self._match_window_streams, streams, pid),
            self._on_window_matched,
        )
        return True

    @staticmethod
    def _match_window_streams(streams: Sequence[AudioNode], pid: int) -> OperationResult:
        """Matches window audio in the worker thread."""

        match = process_matcher.match_playback_streams(streams, pid)
        if match.ambiguous:
            raise AmbiguousApplication(match.reason)
        return OperationResult(attempted=1, succeeded=1, nodes=match.nodes)

    def _on_window_matched(self, result: OperationResult) -> None:
        """Applies the mute change once matching has finished."""

        if result.error is not None:
            self._presenter.error(result.error)
            return
        if not result.nodes:
            self._presenter.present(feedback.MSG_NO_WINDOW_AUDIO)
            return

        muted = self._desired_mute(result.nodes)
        label = result.nodes[0].display_name
        self._backend.set_mute(
            result.nodes,
            muted,
            partial(self._announce, f"{label} is muted." if muted else f"{label} is unmuted."),
        )

    # -- microphone lock ---------------------------------------------------

    def toggle_microphone_volume_lock(self) -> bool:
        """Locks or unlocks the volume of the default recording device."""

        self._presenter.present(self._microphone_lock.toggle(self._backend.snapshot()))
        return True

    # -- Orca output routing (experimental) --------------------------------

    def next_orca_output(self) -> bool:
        """Moves Orca's own speech stream to the next playback device."""

        return self._move_orca_output(1)

    def previous_orca_output(self) -> bool:
        """Moves Orca's own speech stream to the previous playback device."""

        return self._move_orca_output(-1)

    def _move_orca_output(self, delta: int) -> bool:
        """Routes an unambiguously identified Orca speech stream."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        streams, problem = self._orca_streams(snapshot)
        if problem is not None:
            self._presenter.present(problem)
            return True

        sinks = snapshot.visible_sinks()
        if not sinks:
            self._presenter.present(feedback.MSG_NO_PLAYBACK_DEVICES)
            return True

        move = self._navigator.move_application_output(sinks, delta)
        if not isinstance(move.item, AudioNode):
            return True

        target = move.item
        self._presenter.present(f"Moving Orca audio to {target.display_name}")
        self._backend.move_streams(
            streams, target, partial(self._announce, f"Orca audio through {target.display_name}")
        )
        return True

    def _orca_streams(
        self,
        snapshot: AudioSnapshot,
    ) -> tuple[tuple[AudioNode, ...], str | None]:
        """Returns Orca's speech streams, or the reason for refusing.

        Automatic identification only accepts a single stream owner that is
        clearly Orca's. A shared Speech Dispatcher process can mix several
        clients, so moving it could affect more than Orca; that case is refused
        unless the user has named an exact match in preferences.
        """

        unavailable = "Orca's audio stream cannot be identified separately on this system."
        rule = str(self._read_setting("orca-stream-match", "") or "").strip().casefold()

        if rule:
            matches = tuple(
                node
                for node in snapshot.playback_streams
                if rule
                in {
                    (node.application_id or "").casefold(),
                    node.node_name.casefold(),
                    (node.process_binary or "").casefold(),
                    (node.application_name or "").casefold(),
                }
            )
            movable = tuple(node for node in matches if not node.dont_move)
            return (movable, None) if movable else ((), unavailable)

        candidates = tuple(node for node in snapshot.playback_streams if node.is_screen_reader)
        if not candidates:
            return (), unavailable

        if len({group_key_for(node) for node in candidates}) > 1:
            return (), unavailable

        for node in candidates:
            identity = f"{node.process_binary or ''} {node.node_name}".casefold()
            if any(hint in identity for hint in _SHARED_SPEECH_HINTS):
                return (), (
                    "Orca's audio is provided by a shared speech service. "
                    "Set an exact stream match in this extension's preferences to move it."
                )
            if node.dont_move:
                return (), unavailable

        return candidates, None

    # -- reporting ---------------------------------------------------------

    def report_current_target(self) -> bool:
        """Reports the current target and its state."""

        snapshot = self._snapshot()
        if snapshot is None:
            return True

        state = self._navigator.state
        if state.active_kind == "application":
            group = snapshot.application_by_key(state.application_key)
            if group is None:
                self._presenter.present(feedback.MSG_TARGET_GONE)
                return True
            self._presenter.present(self._describe_application(snapshot, group))
            return True

        if state.active_kind is None:
            return True

        node = self._selected_device(snapshot)
        if node is None:
            self._presenter.present(feedback.MSG_TARGET_GONE)
            return True

        message = feedback.describe_device(node)
        lock_status = self._microphone_lock.describe_target(node) if node.is_source else ""
        self._presenter.present(f"{message} {lock_status}".strip())
        return True

    # -- lifecycle ---------------------------------------------------------

    def on_snapshot(self, snapshot: AudioSnapshot) -> None:
        """Handles a new graph snapshot."""

        self._microphone_lock.on_snapshot(snapshot)

    def on_disconnect(self, error: AudioManagerError) -> None:
        """Handles the backend losing its connection."""

        self._navigator.clear()
