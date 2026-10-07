"""Audio Manager: keyboard-driven PipeWire control for Orca.

This is a Linux port of the interaction model of the NVDA Audio Manager add-on.
It navigates playback devices, recording devices, and application audio;
changes volume, mute state, and defaults; routes application streams; and locks
the microphone volume.

Only documented Orca extension APIs are used: command registration, generated
preferences, the remote controller's ``*_internal()`` helpers, and the lifecycle
hooks. Orca's speech, settings, event, and key-grab managers are never imported.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Callable

# Orca loads a package extension under the dotted module name
# "orca_user_extension.<package>" but does not create that parent package.
# Python resolves relative imports through the parent chain, so the submodule
# imports below fail with "No module named 'orca_user_extension'" unless a
# placeholder exists. Creating it here keeps the package importable both under
# Orca and directly (where there is no dotted parent to stand in for).
_PARENT_NAME = __name__.rpartition(".")[0]
if _PARENT_NAME and _PARENT_NAME not in sys.modules:
    _parent_module = types.ModuleType(_PARENT_NAME)
    _parent_module.__path__ = []  # type: ignore[attr-defined]
    sys.modules[_PARENT_NAME] = _parent_module

from orca import keybindings
from orca.command import Command, KeyboardCommand
from orca.extension import Extension, ExtensionPreference

from .cli_backend import CliBackend
from .commands import AudioCommands
from .errors import AudioManagerError
from .feedback import Presenter
from .microphone_lock import MicrophoneLock
from .models import AudioSnapshot

__all__ = ["AudioManagerExtension"]

# The original add-on used Control+Windows+Alt. The Super modifier is not among
# the modifiers documented for Orca user extensions and is commonly claimed by
# the desktop under Wayland, so the spatial layout is preserved on
# Orca+Control+Alt instead.
_MODIFIERS = keybindings.ORCA_CTRL_MODIFIER_MASK | keybindings.ALT_MODIFIER_MASK
_SHIFT_MODIFIERS = _MODIFIERS | keybindings.SHIFT_MODIFIER_MASK

# command name, description, desktop key, laptop key, whether Shift is included.
_COMMANDS: tuple[tuple[str, str, str, str, bool], ...] = (
    (
        "previous_recording_device",
        "Selects the previous recording device",
        "KP_1",
        "Home",
        False,
    ),
    ("next_recording_device", "Selects the next recording device", "KP_3", "End", False),
    (
        "previous_playback_device",
        "Selects the previous playback device",
        "KP_4",
        "Page_Up",
        False,
    ),
    ("next_playback_device", "Selects the next playback device", "KP_6", "Page_Down", False),
    (
        "previous_audio_application",
        "Selects the previous audio application",
        "KP_7",
        "Left",
        False,
    ),
    ("next_audio_application", "Selects the next audio application", "KP_9", "Right", False),
    ("volume_up", "Increases the volume of the current target", "KP_8", "Up", False),
    ("volume_down", "Decreases the volume of the current target", "KP_2", "Down", False),
    ("toggle_mute", "Mutes or unmutes the current target", "KP_5", "space", False),
    (
        "set_default_device",
        "Makes the selected device the system default",
        "KP_Enter",
        "Return",
        False,
    ),
    (
        "next_application_output",
        "Routes the application's playback to the next device",
        "KP_Multiply",
        "bracketright",
        False,
    ),
    (
        "previous_application_output",
        "Routes the application's playback to the previous device",
        "KP_Multiply",
        "bracketright",
        True,
    ),
    (
        "next_application_input",
        "Routes the application's recording to the next device",
        "KP_Divide",
        "bracketleft",
        False,
    ),
    (
        "previous_application_input",
        "Routes the application's recording to the previous device",
        "KP_Divide",
        "bracketleft",
        True,
    ),
    (
        "reset_application_routes",
        "Resets the playback and recording devices of all audio applications",
        "KP_Subtract",
        "BackSpace",
        False,
    ),
    (
        "reset_application_audio",
        "Resets the volume of all audio applications and unmutes them",
        "KP_Subtract",
        "BackSpace",
        True,
    ),
    (
        "next_orca_output",
        "Moves Orca's audio to the next playback device",
        "KP_Add",
        "backslash",
        False,
    ),
    (
        "previous_orca_output",
        "Moves Orca's audio to the previous playback device",
        "KP_Add",
        "backslash",
        True,
    ),
    (
        "toggle_current_window_mute",
        "Mutes or unmutes the audio of the current window",
        "KP_8",
        "space",
        True,
    ),
    (
        "toggle_default_output_mute",
        "Mutes or unmutes the default playback device",
        "KP_5",
        "Up",
        True,
    ),
    (
        "toggle_default_microphone_mute",
        "Mutes or unmutes the default recording device",
        "KP_2",
        "Down",
        True,
    ),
    (
        "toggle_microphone_volume_lock",
        "Locks or unlocks the volume of the default recording device",
        "KP_Delete",
        "Delete",
        False,
    ),
    # Not KP_0: it shares a keycode with KP_Insert, the desktop Orca key, so
    # holding Orca+Control+Alt alone would trigger this command and repeat it.
    ("report_current_target", "Reports the current audio target", "0", "0", False),
)


class AudioManagerExtension(Extension):
    """Keyboard-driven PipeWire device and application control for Orca."""

    GROUP_LABEL = "Audio Manager"
    DESCRIPTION = "Keyboard-driven PipeWire audio device and application control."
    VERSION = "0.1.0"
    AUTHOR = "Deniz Aygun"
    COPYRIGHT = "2026 Deniz Aygun"

    def __init__(self) -> None:
        self._shutting_down = False
        self._started = False
        self._backend: CliBackend | None = None
        self._presenter: Presenter | None = None
        self._commands: AudioCommands | None = None
        self._microphone_lock: MicrophoneLock | None = None
        super().__init__()

    # -- settings ----------------------------------------------------------

    def _read_setting(self, key: str, default: Any) -> Any:
        """Returns one of this extension's settings."""

        return self.settings.get(key, default=default)

    def _write_setting(self, key: str, value: Any) -> None:
        """Persists one of this extension's settings."""

        self.settings.set(key, value)

    def _int_setting(self, key: str, default: int, low: int, high: int) -> int:
        """Returns a bounded integer setting, or default if it is unreadable."""

        try:
            return max(low, min(high, int(self._read_setting(key, default))))
        except (TypeError, ValueError):
            return default

    def _timeout_seconds(self) -> float:
        """Returns the configured subprocess timeout, in seconds."""

        return self._int_setting("operation-timeout-ms", 2000, 500, 5000) / 1000.0

    def _poll_interval(self) -> int:
        """Returns the microphone-lock safety interval, in milliseconds."""

        return self._int_setting("microphone-lock-poll-ms", 500, 200, 2000)

    def get_preferences(self) -> list[ExtensionPreference]:
        """Returns the preferences shown in Orca's generated dialog."""

        return [
            ExtensionPreference.integer("volume-step-percent", "Volume step (percent)", 1, 1, 20),
            ExtensionPreference.integer(
                "maximum-volume-percent", "Maximum volume (percent)", 100, 100, 150
            ),
            ExtensionPreference.boolean(
                "boundary-sounds", "Play a sound at list boundaries", True
            ),
            ExtensionPreference.path(
                "boundary-sound-folder",
                "Folder containing left.wav and right.wav",
                "",
                directory=True,
            ),
            ExtensionPreference.boolean("wrap-device-lists", "Wrap around device lists", False),
            ExtensionPreference.boolean(
                "include-monitor-sources", "Include monitor sources in recording devices", False
            ),
            ExtensionPreference.boolean(
                "include-screen-reader-streams",
                "Include screen reader audio in application navigation",
                False,
            ),
            ExtensionPreference.boolean(
                "reset-compatibility-double-press",
                "Pressing the route reset command twice also resets volume and mute",
                False,
            ),
            ExtensionPreference.boolean(
                "microphone-lock-enabled", "Lock the microphone volume", False
            ),
            ExtensionPreference.integer(
                "microphone-lock-volume-percent",
                "Locked microphone volume (percent)",
                50,
                0,
                100,
            ),
            ExtensionPreference.integer(
                "microphone-lock-poll-ms", "Microphone lock check interval (ms)", 500, 200, 2000
            ),
            ExtensionPreference.integer(
                "operation-timeout-ms", "Audio command timeout (ms)", 2000, 500, 5000
            ),
            ExtensionPreference.info(
                "Moving Orca's own audio is experimental. Leave the stream match empty to let "
                "the extension refuse ambiguous cases. A rule that names a shared speech "
                "service will move the audio of every application using that service."
            ),
            ExtensionPreference.string(
                "orca-stream-match", "Exact Orca stream match (advanced)", ""
            ),
        ]

    # -- commands ----------------------------------------------------------

    def _get_commands(self) -> list[Command]:
        """Returns the commands this extension registers.

        Command names are stable so that user key customizations survive
        upgrades. Orca leaves a command unbound if its default keybinding
        conflicts; the command stays registered and can be rebound.
        """

        commands: list[Command] = []
        for name, description, desktop_key, laptop_key, shifted in _COMMANDS:
            modifiers = _SHIFT_MODIFIERS if shifted else _MODIFIERS
            commands.append(
                KeyboardCommand(
                    name,
                    self._bind(name),
                    self.GROUP_LABEL,
                    description,
                    desktop_keybinding=keybindings.KeyBinding(desktop_key, modifiers),
                    laptop_keybinding=keybindings.KeyBinding(laptop_key, modifiers),
                )
            )
        return commands

    def _bind(self, name: str) -> Callable[[], bool]:
        """Returns a callable that runs the named command."""

        def run() -> bool:
            if self._shutting_down:
                return True
            if not self._start() or self._commands is None:
                return True
            return bool(getattr(self._commands, name)())

        run.__name__ = name
        return run

    # -- lifecycle ---------------------------------------------------------

    def on_ready(self) -> None:
        """Starts the backend once Orca is ready for normal operation."""

        self._start()

    def _start(self) -> bool:
        """Creates and starts the backend, returning True when usable."""

        if self._started:
            return self._commands is not None
        self._started = True

        try:
            backend = CliBackend(self._timeout_seconds)
            presenter = Presenter(
                self.controller,
                settings_reader=self._read_setting,
                is_shutting_down=lambda: self._shutting_down,
            )
            microphone_lock = MicrophoneLock(
                backend,
                self._read_setting,
                self._write_setting,
                self._poll_interval,
            )
            commands = AudioCommands(
                backend,
                presenter,
                self._read_setting,
                self.controller,
                microphone_lock,
            )
            backend.start(self._on_snapshot, self._on_disconnect)
        except Exception:  # pylint: disable=broad-exception-caught
            # A failure here must not prevent Orca from starting. The commands
            # stay registered so that recovery does not require reinstalling.
            self._backend = None
            self._commands = None
            return False

        self._backend = backend
        self._presenter = presenter
        self._microphone_lock = microphone_lock
        self._commands = commands
        microphone_lock.start()
        return True

    def _on_snapshot(self, snapshot: AudioSnapshot) -> None:
        """Handles a new graph snapshot on Orca's main thread."""

        if self._shutting_down or self._commands is None:
            return
        self._commands.on_snapshot(snapshot)

    def _on_disconnect(self, error: AudioManagerError) -> None:
        """Handles the backend losing its connection to PipeWire."""

        if self._shutting_down or self._commands is None:
            return
        self._commands.on_disconnect(error)

    def on_disabled(self) -> None:
        """Releases all resources when the extension is disabled."""

        self._teardown()

    def on_shutdown(self) -> None:
        """Stops background work quickly when Orca exits.

        Nothing is presented here: Orca has already said "Screen reader off",
        and shutdown hooks are given only a short time to finish.
        """

        self._shutting_down = True
        self._teardown()

    def _teardown(self) -> None:
        """Stops the lock timer and the backend without blocking."""

        if self._microphone_lock is not None:
            self._microphone_lock.stop()
            self._microphone_lock = None
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        self._commands = None
        self._presenter = None
        self._started = False
