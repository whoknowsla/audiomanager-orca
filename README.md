# Audio Manager for Orca

A Linux port of the interaction model of the NVDA **Audio Manager** add-on, built
as an Orca user extension on top of PipeWire and WirePlumber.

- Version: 0.1.0
- Author: Deniz Aygun
- License: MIT
- Targets: Orca 51 (user extension API), PipeWire 1.x, WirePlumber 0.5

## What it does

- Navigate playback devices, recording devices, and application audio.
- Report and change volume and mute state for the current target.
- Make the selected device the default playback or recording device.
- Route an application's playback and capture streams to other devices.
- Reset live application routes, volumes, and mute states.
- Mute the audio of the current window when it can be matched safely.
- Toggle the default sink and the default source ("sound switch", "microphone
  switch").
- Keep the default microphone at a chosen volume.
- Move Orca's own audio stream (experimental, refuses ambiguous cases).

Commands speak and braille a result, including no devices, PipeWire
unavailable, permission denied, operation timed out, and selected object gone.
Commands that need a selection stay silent when nothing is selected.

## Requirements

- Orca with user-extension support (`~/.local/share/orca/extensions/`).
- PipeWire with WirePlumber, and the `pw-dump`, `wpctl`, and `pw-metadata`
  commands on `PATH` (packaged as `pipewire`/`pipewire-utils` and `wireplumber`
  on most distributions).

No shared library, daemon, root privileges, or WirePlumber configuration
fragments are needed. The extension never edits WirePlumber state files and
never runs `wpctl reset`.

## Installation

```sh
mkdir -p ~/.local/share/orca/extensions
cp -r audio_manager ~/.local/share/orca/extensions/
orca --approve-extension audio_manager
```

Then restart Orca. `./install.sh` performs the copy and the approval for you.

Approval is required for security: Orca records the extension's SHA256 hash. Any
change to the package contents changes that hash, so re-approve after updating.
Approval and enabled state are separate: an approved extension can still be
disabled on the **User Extensions** page in Orca's preferences.

## Default keybindings

The original add-on used `Control+Windows+Alt`. The Super modifier is not among
the modifiers documented for Orca user extensions and is frequently claimed by
the desktop under Wayland, so the same spatial layout is used with
**Orca+Control+Alt**. Orca leaves a command unbound if its default keybinding
conflicts with an Orca command or another extension; the command stays
registered and can be assigned in Orca's preferences.

| Command | Desktop | Laptop |
| --- | --- | --- |
| Previous recording device | Orca+Control+Alt+KP 1 | Orca+Control+Alt+Home |
| Next recording device | Orca+Control+Alt+KP 3 | Orca+Control+Alt+End |
| Previous playback device | Orca+Control+Alt+KP 4 | Orca+Control+Alt+Page Up |
| Next playback device | Orca+Control+Alt+KP 6 | Orca+Control+Alt+Page Down |
| Previous audio application | Orca+Control+Alt+KP 7 | Orca+Control+Alt+Left |
| Next audio application | Orca+Control+Alt+KP 9 | Orca+Control+Alt+Right |
| Volume up | Orca+Control+Alt+KP 8 | Orca+Control+Alt+Up |
| Volume down | Orca+Control+Alt+KP 2 | Orca+Control+Alt+Down |
| Mute or unmute | Orca+Control+Alt+KP 5 | Orca+Control+Alt+Space |
| Set as default device | Orca+Control+Alt+KP Enter | Orca+Control+Alt+Return |
| Next application playback device | Orca+Control+Alt+KP Multiply | Orca+Control+Alt+] |
| Previous application playback device | Shift+Orca+Control+Alt+KP Multiply | Shift+Orca+Control+Alt+] |
| Next application recording device | Orca+Control+Alt+KP Divide | Orca+Control+Alt+[ |
| Previous application recording device | Shift+Orca+Control+Alt+KP Divide | Shift+Orca+Control+Alt+[ |
| Reset application routes | Orca+Control+Alt+KP Subtract | Orca+Control+Alt+Backspace |
| Reset application volume and mute | Shift+Orca+Control+Alt+KP Subtract | Shift+Orca+Control+Alt+Backspace |
| Next Orca output device | Orca+Control+Alt+KP Add | Orca+Control+Alt+\ |
| Previous Orca output device | Shift+Orca+Control+Alt+KP Add | Shift+Orca+Control+Alt+\ |
| Mute or unmute the current window | Shift+Orca+Control+Alt+KP 8 | Shift+Orca+Control+Alt+Space |
| Sound switch (default playback device) | Shift+Orca+Control+Alt+KP 5 | Shift+Orca+Control+Alt+Up |
| Microphone switch (default recording device) | Shift+Orca+Control+Alt+KP 2 | Shift+Orca+Control+Alt+Down |
| Lock or unlock the microphone volume | Orca+Control+Alt+KP Delete | Orca+Control+Alt+Delete |
| Report the current target | Orca+Control+Alt+0 | Orca+Control+Alt+0 |

The original add-on used one key for both resets, distinguished by a double
press. Here they are separate commands. Enable
**Pressing the route reset command twice also resets volume and mute** in
preferences to get the original single-key behavior as well.

## Preferences

Open Orca's preferences, go to **User Extensions**, select **Audio Manager**, and
press the settings button.

| Preference | Default | Purpose |
| --- | --- | --- |
| Volume step (percent) | 1 | Step used by volume up and down |
| Maximum volume (percent) | 100 | Prevents accidental amplification |
| Play a sound at list boundaries | on | Feedback at the first and last item |
| Folder containing left.wav and right.wav | empty | Use your own boundary sounds instead of tones |
| Wrap around device lists | off | The original behavior clamps at the ends |
| Include monitor sources | off | Show sink monitors as recording devices |
| Include screen reader audio | off | Show Orca and speech services as applications |
| Route reset double press | off | Compatibility with the original single key |
| Lock the microphone volume | off | Persisted lock state |
| Locked microphone volume (percent) | 50 | Level the lock maintains |
| Microphone lock check interval (ms) | 500 | Safety net for the event-driven lock |
| Audio command timeout (ms) | 2000 | Timeout for each PipeWire command |
| Exact Orca stream match | empty | Advanced: name Orca's audio stream explicitly |

## Behavior notes and limits

**Applications, not sessions.** An application that owns several streams (browser
tabs, music plus notifications, a call with playback and capture) appears once.
Volume and mute act on every playback member, or on every capture member when
there is no playback. A partial failure is reported as "Changed N of M streams".
Streams with no reliable identity are kept separate rather than merged with
unrelated audio.

**Selections are stable keys, not indexes.** Hotplug and reordering cannot move
your selection onto a different device. If the selected object disappears, the
extension says so before acting on the nearest item.

**Routing is WirePlumber's job.** Application routing sets `target.object` on the
stream and lets WirePlumber create the links, pick channel maps, and persist the
choice. The extension does not create links or keep its own route database.
Resetting unsets `target.object` on live streams; it never deletes WirePlumber
state files.

**Current-window mute is best effort.** The window's process ID is matched
against playback streams: exact PID first, then a bounded `/proc` process-tree
walk, then a unique application identity. Display names are never used. If
several unrelated applications match, the extension refuses and says so. It never
falls back to muting the default sink, because that would silence Orca and the
whole desktop.

**Screen reader audio is protected.** Orca and speech-service streams are hidden
from application navigation and excluded from mute and reset operations unless
you enable that in preferences.

**Moving Orca's own audio is experimental.** Orca's extension API exposes no
output-device setting, so this works at the PipeWire stream level. When the only
candidate is a shared Speech Dispatcher stream, moving it could affect other
clients of that service, so the extension refuses and tells you to set an exact
match in preferences if you want it anyway.

**Responsiveness.** No subprocess, graph parse, `/proc` walk, or PipeWire round
trip runs on Orca's main thread. One reader thread owns `pw-dump --monitor`; one
serialized worker owns every one-shot command. If PipeWire is stopped or hangs,
commands report the problem and Orca stays responsive. The backend reconnects
with bounded exponential backoff.

## Structure

```text
audio_manager/
├── __init__.py          Extension class, preferences, commands, lifecycle
├── backend.py           Backend protocol, capabilities, operation results
├── cli_backend.py       pw-dump monitor plus async wpctl and pw-metadata
├── models.py            Immutable graph model and pw-dump parsing
├── applications.py      Stream grouping, labels, snapshot assembly
├── navigators.py        Stable-key selection and list movement
├── commands.py          User intents, independent of PipeWire details
├── feedback.py          Messages and boundary feedback
├── microphone_lock.py   Event-driven microphone volume lock
├── process_matcher.py   Active window and process-tree matching
└── errors.py            Typed errors and their user messages
```

A phase-two backend using the WirePlumber GObject API can be added behind the
same protocol in `backend.py` without changing the layers above it. It is not
part of this release.

## Tests

```sh
python3 -m unittest discover -s tests
```

The tests cover graph parsing with unknown fields, node classification,
application grouping, default detection, clamped and wrapped movement,
stale-selection recovery, `wpctl` output parsing, argument construction, route
validation, process matching and its refusals, and command behavior against a
stub backend. They do not touch the running audio graph.

## Credits

Interaction model and feature set from the NVDA Audio Manager add-on by
huaiyinfeilong. This is an independent Linux implementation for Orca; no code
from the original add-on is used.

## License

Released under the MIT License. See [LICENSE](LICENSE).
