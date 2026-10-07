"""Immutable data model for the PipeWire graph.

The backend converts raw ``pw-dump`` objects into the frozen dataclasses defined
here. Everything above the backend (navigators, commands, feedback) only ever
sees these snapshots, which makes the upper layers pure and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable

__all__ = [
    "ApplicationGroup",
    "AudioNode",
    "AudioSnapshot",
    "CAPTURE_STREAM",
    "METADATA_TYPE",
    "NODE_TYPE",
    "PLAYBACK_STREAM",
    "SINK",
    "SOURCE",
    "STREAM_CLASSES",
    "VolumeState",
    "is_screen_reader_identity",
    "linear_to_scaled",
    "node_from_object",
    "parse_default_names",
    "scaled_to_percent",
]

SINK = "Audio/Sink"
SOURCE = "Audio/Source"
PLAYBACK_STREAM = "Stream/Output/Audio"
CAPTURE_STREAM = "Stream/Input/Audio"

STREAM_CLASSES = frozenset({PLAYBACK_STREAM, CAPTURE_STREAM})
DEVICE_CLASSES = frozenset({SINK, SOURCE})
INTERESTING_CLASSES = STREAM_CLASSES | DEVICE_CLASSES

NODE_TYPE = "PipeWire:Interface:Node"
METADATA_TYPE = "PipeWire:Interface:Metadata"

# Identities that belong to the screen reader's own audio path. These are hidden
# from ordinary application navigation, and excluded from mute and reset
# operations, so that the extension can never silence Orca itself.
_SCREEN_READER_EXACT = frozenset(
    {
        "orca",
        "brltty",
        "espeak",
        "espeak-ng",
        "speechd",
        "speech-dispatcher",
        "spd-say",
    }
)
_SCREEN_READER_PREFIXES = ("speech-dispatcher", "speechd-", "sd_", "orca-", "orca_", "orca.")
_SCREEN_READER_TOKENS = frozenset({"orca", "speechd", "brltty"})

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def linear_to_scaled(linear: float) -> float:
    """Converts a PipeWire linear amplitude to the cubic scale used by wpctl.

    WirePlumber's mixer API, and therefore ``wpctl``, presents volumes on a cubic
    scale where 1.0 means 100 percent. PipeWire stores linear amplitudes in
    ``channelVolumes``. Reading the graph directly avoids a subprocess for every
    navigation command.
    """

    if linear <= 0.0:
        return 0.0
    return float(linear ** (1.0 / 3.0))


def scaled_to_percent(scaled: float) -> int:
    """Converts a wpctl-scale volume to a whole percentage."""

    return int(round(scaled * 100.0))


def is_screen_reader_identity(*values: str | None) -> bool:
    """Returns True if any value identifies screen-reader or speech audio."""

    for value in values:
        if not value:
            continue
        low = value.strip().lower()
        if not low:
            continue
        if low in _SCREEN_READER_EXACT or low.startswith(_SCREEN_READER_PREFIXES):
            return True
        if not _SCREEN_READER_TOKENS.isdisjoint(_WORD_SPLIT.split(low)):
            return True
    return False


@dataclass(frozen=True)
class VolumeState:
    """A volume and mute state on the wpctl scale, where 1.0 is 100 percent."""

    volume: float
    muted: bool

    @property
    def percent(self) -> int:
        """Returns the volume as a whole percentage."""

        return scaled_to_percent(self.volume)


@dataclass(frozen=True)
class AudioNode:
    """One PipeWire node: a sink, a source, or an application stream."""

    object_id: int
    object_serial: int | None
    stable_key: str
    media_class: str
    node_name: str
    display_name: str
    description: str | None = None
    nick: str | None = None
    device_id: int | None = None
    application_id: str | None = None
    application_name: str | None = None
    process_id: int | None = None
    process_binary: str | None = None
    media_name: str | None = None
    target_object: str | None = None
    volume: VolumeState | None = None
    is_default: bool = False
    is_hidden: bool = False
    is_virtual: bool = False
    is_monitor: bool = False
    is_screen_reader: bool = False
    dont_move: bool = False

    @property
    def is_sink(self) -> bool:
        """Returns True if this node is a playback device."""

        return self.media_class == SINK

    @property
    def is_source(self) -> bool:
        """Returns True if this node is a recording device."""

        return self.media_class == SOURCE

    @property
    def is_playback_stream(self) -> bool:
        """Returns True if this node is an application playback stream."""

        return self.media_class == PLAYBACK_STREAM

    @property
    def is_capture_stream(self) -> bool:
        """Returns True if this node is an application capture stream."""

        return self.media_class == CAPTURE_STREAM

    def with_default(self, is_default: bool) -> AudioNode:
        """Returns a copy of this node with the default flag applied."""

        if is_default == self.is_default:
            return self
        return replace(self, is_default=is_default)


@dataclass(frozen=True)
class ApplicationGroup:
    """All streams that belong to one application."""

    stable_key: str
    display_name: str
    application_id: str | None
    process_ids: frozenset[int]
    playback_nodes: tuple[AudioNode, ...] = ()
    capture_nodes: tuple[AudioNode, ...] = ()
    is_screen_reader: bool = False

    @property
    def nodes(self) -> tuple[AudioNode, ...]:
        """Returns every member node."""

        return self.playback_nodes + self.capture_nodes

    @property
    def has_playback(self) -> bool:
        """Returns True if the application is playing audio."""

        return bool(self.playback_nodes)

    @property
    def has_capture(self) -> bool:
        """Returns True if the application is capturing audio."""

        return bool(self.capture_nodes)

    def volume_nodes(self) -> tuple[AudioNode, ...]:
        """Returns the nodes that volume and mute commands act on.

        Playback wins when both directions are present, matching the original
        add-on's treatment of an application session as its playback session.
        """

        return self.playback_nodes or self.capture_nodes


@dataclass(frozen=True)
class AudioSnapshot:
    """An immutable view of the audio graph at one point in time."""

    generation: int = 0
    connected: bool = False
    sinks: tuple[AudioNode, ...] = ()
    sources: tuple[AudioNode, ...] = ()
    playback_streams: tuple[AudioNode, ...] = ()
    capture_streams: tuple[AudioNode, ...] = ()
    applications: tuple[ApplicationGroup, ...] = ()
    default_sink_name: str | None = None
    default_source_name: str | None = None

    def visible_sinks(self) -> tuple[AudioNode, ...]:
        """Returns the playback devices to expose to the user."""

        return tuple(node for node in self.sinks if not node.is_hidden)

    def visible_sources(self, include_monitors: bool = False) -> tuple[AudioNode, ...]:
        """Returns the recording devices to expose to the user."""

        return tuple(
            node
            for node in self.sources
            if not node.is_hidden and (include_monitors or not node.is_monitor)
        )

    def visible_applications(
        self,
        include_screen_reader: bool = False,
    ) -> tuple[ApplicationGroup, ...]:
        """Returns the application groups to expose to the user."""

        if include_screen_reader:
            return self.applications
        return tuple(group for group in self.applications if not group.is_screen_reader)

    def all_nodes(self) -> tuple[AudioNode, ...]:
        """Returns every parsed node."""

        return self.sinks + self.sources + self.playback_streams + self.capture_streams

    def node_by_key(self, stable_key: str | None) -> AudioNode | None:
        """Returns the node with stable_key, or None."""

        if not stable_key:
            return None
        return next((node for node in self.all_nodes() if node.stable_key == stable_key), None)

    def application_by_key(self, stable_key: str | None) -> ApplicationGroup | None:
        """Returns the application group with stable_key, or None."""

        if not stable_key:
            return None
        return next(
            (group for group in self.applications if group.stable_key == stable_key),
            None,
        )

    def default_sink(self) -> AudioNode | None:
        """Returns the current default playback device, or None."""

        return next((node for node in self.sinks if node.is_default), None)

    def default_source(self) -> AudioNode | None:
        """Returns the current default recording device, or None."""

        return next((node for node in self.sources if node.is_default), None)


def _first_text(props: dict[str, Any], *keys: str) -> str | None:
    """Returns the first non-empty string value among keys."""

    for key in keys:
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_int(value: Any) -> int | None:
    """Returns value as an int when it safely converts, else None."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_bool(value: Any) -> bool:
    """Returns value interpreted as a PipeWire boolean property."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _volume_from_params(params: dict[str, Any]) -> VolumeState | None:
    """Returns the volume state from a node's Props parameter, if present."""

    props_params = params.get("Props")
    if not isinstance(props_params, list) or not props_params:
        return None

    entry = next((item for item in props_params if isinstance(item, dict)), None)
    if entry is None:
        return None

    channels = entry.get("channelVolumes")
    linear: float | None = None
    if isinstance(channels, list) and channels:
        numeric = [float(value) for value in channels if isinstance(value, (int, float))]
        if numeric:
            linear = sum(numeric) / len(numeric)
    if linear is None and isinstance(entry.get("volume"), (int, float)):
        linear = float(entry["volume"])
    if linear is None:
        return None

    return VolumeState(linear_to_scaled(linear), _as_bool(entry.get("mute", False)))


def _display_name_for(media_class: str, props: dict[str, Any]) -> str:
    """Returns the human-readable label for a node."""

    if media_class in DEVICE_CLASSES:
        label = _first_text(props, "node.description", "node.nick", "node.name")
    else:
        label = _first_text(
            props,
            "application.name",
            "application.id",
            "application.process.binary",
            "media.name",
            "node.description",
            "node.name",
        )
    return label or "Unknown audio node"


def _stable_key_for(media_class: str, props: dict[str, Any], object_serial: int | None) -> str:
    """Returns a selection key that survives graph churn.

    Devices keep their ``node.name`` across restarts and reordering. Streams are
    per-instance objects, so they additionally carry the object serial, which is
    unique and monotonic for the lifetime of one PipeWire server instance.
    """

    node_name = _first_text(props, "node.name") or "unnamed"
    if media_class in DEVICE_CLASSES:
        return f"{media_class}|{node_name}"
    serial = object_serial if object_serial is not None else "?"
    return f"{media_class}|{node_name}|{serial}"


def _is_monitor(props: dict[str, Any], node_name: str) -> bool:
    """Returns True if the node is a monitor of a sink."""

    if _as_bool(props.get("node.monitor", False)):
        return True
    if str(props.get("device.class", "")).lower() == "monitor":
        return True
    return node_name.endswith(".monitor")


def node_from_object(obj: dict[str, Any]) -> AudioNode | None:
    """Converts one pw-dump object into an AudioNode, or returns None.

    Objects that are not audio nodes, and nodes whose media class the extension
    does not present, are ignored rather than guessed at, so that additions to
    PipeWire cannot break parsing.
    """

    if obj.get("type") != NODE_TYPE:
        return None

    object_id = _as_int(obj.get("id"))
    if object_id is None:
        return None

    info = obj.get("info") or {}
    if not isinstance(info, dict):
        return None
    props = info.get("props") or {}
    if not isinstance(props, dict):
        return None

    media_class = props.get("media.class")
    if media_class not in INTERESTING_CLASSES:
        return None

    node_name = _first_text(props, "node.name") or f"node-{object_id}"
    object_serial = _as_int(props.get("object.serial"))
    params = info.get("params") if isinstance(info.get("params"), dict) else {}

    application_name = _first_text(props, "application.name")
    process_binary = _first_text(props, "application.process.binary")
    description = _first_text(props, "node.description")

    return AudioNode(
        object_id=object_id,
        object_serial=object_serial,
        stable_key=_stable_key_for(media_class, props, object_serial),
        media_class=media_class,
        node_name=node_name,
        display_name=_display_name_for(media_class, props),
        description=description,
        nick=_first_text(props, "node.nick"),
        device_id=_as_int(props.get("device.id")),
        application_id=_first_text(props, "application.id"),
        application_name=application_name,
        process_id=_as_int(props.get("application.process.id")),
        process_binary=process_binary,
        media_name=_first_text(props, "media.name"),
        target_object=_first_text(props, "target.object"),
        volume=_volume_from_params(params),
        is_hidden=_as_bool(props.get("node.hidden", False)),
        is_virtual=_as_bool(props.get("node.virtual", False)),
        is_monitor=_is_monitor(props, node_name),
        is_screen_reader=is_screen_reader_identity(
            application_name, process_binary, node_name, description
        ),
        dont_move=_as_bool(props.get("node.dont-move", False)),
    )


def parse_default_names(objects: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Returns the default sink and source node names from the graph.

    The values come from the ``default`` metadata object, which is what
    WirePlumber updates when a default device is selected.
    """

    sink_name: str | None = None
    source_name: str | None = None

    for obj in objects:
        if obj.get("type") != METADATA_TYPE:
            continue
        props = obj.get("props") or {}
        if not isinstance(props, dict) or props.get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            if not isinstance(entry, dict) or entry.get("subject") not in (0, None):
                continue
            value = entry.get("value")
            name = value.get("name") if isinstance(value, dict) else None
            if not isinstance(name, str):
                continue
            if entry.get("key") == "default.audio.sink":
                sink_name = name
            elif entry.get("key") == "default.audio.source":
                source_name = name

    return sink_name, source_name
