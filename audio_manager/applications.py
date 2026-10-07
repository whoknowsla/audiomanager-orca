"""Application grouping and snapshot assembly.

One application can own several PipeWire nodes at once: music and notification
playback, one stream per browser tab, or playback plus capture during a call.
The user should see one entry per application while every member node is still
available to operate on.
"""

from __future__ import annotations

from typing import Any, Iterable

from .models import (
    CAPTURE_STREAM,
    PLAYBACK_STREAM,
    SINK,
    SOURCE,
    ApplicationGroup,
    AudioNode,
    AudioSnapshot,
    node_from_object,
    parse_default_names,
)

__all__ = ["build_snapshot", "group_key_for", "group_streams", "sort_key"]


def sort_key(item: AudioNode | ApplicationGroup) -> tuple[str, str]:
    """Returns the deterministic ordering key for a node or application."""

    return (item.display_name.casefold(), item.stable_key)


def group_key_for(node: AudioNode) -> str:
    """Returns the grouping key for a stream node.

    ``application.id`` is preferred because it is the most deliberate identity a
    client can publish. Process identity comes next. A node with no reliable
    identity stays in its own group rather than being merged into a generic
    bucket together with unrelated audio.
    """

    if node.application_id:
        return f"app-id:{node.application_id}"
    if node.process_id is not None and node.process_binary:
        return f"pid:{node.process_id}:{node.process_binary}"
    if node.process_id is not None:
        return f"pid:{node.process_id}"
    return f"node:{node.node_name}"


def group_streams(streams: Iterable[AudioNode]) -> tuple[ApplicationGroup, ...]:
    """Groups stream nodes into applications.

    Direction is deliberately not part of the group key so that an application
    with both playback and capture streams appears once.
    """

    playback: dict[str, list[AudioNode]] = {}
    capture: dict[str, list[AudioNode]] = {}
    order: list[str] = []

    for node in streams:
        if node.media_class not in (PLAYBACK_STREAM, CAPTURE_STREAM):
            continue
        key = group_key_for(node)
        if key not in playback:
            playback[key] = []
            capture[key] = []
            order.append(key)
        if node.is_playback_stream:
            playback[key].append(node)
        else:
            capture[key].append(node)

    groups = []
    for key in order:
        playback_nodes = tuple(playback[key])
        capture_nodes = tuple(capture[key])
        # Never empty: a key is only recorded together with its first node.
        members = playback_nodes + capture_nodes
        groups.append(
            ApplicationGroup(
                stable_key=key,
                display_name=members[0].display_name,
                application_id=next(
                    (node.application_id for node in members if node.application_id), None
                ),
                process_ids=frozenset(
                    node.process_id for node in members if node.process_id is not None
                ),
                playback_nodes=playback_nodes,
                capture_nodes=capture_nodes,
                is_screen_reader=any(node.is_screen_reader for node in members),
            )
        )

    return tuple(sorted(groups, key=sort_key))


def build_snapshot(
    objects: Iterable[dict[str, Any]],
    generation: int = 0,
    connected: bool = True,
) -> AudioSnapshot:
    """Builds an immutable snapshot from raw pw-dump objects."""

    object_list = [obj for obj in objects if isinstance(obj, dict)]
    default_sink_name, default_source_name = parse_default_names(object_list)

    sinks: list[AudioNode] = []
    sources: list[AudioNode] = []
    playback_streams: list[AudioNode] = []
    capture_streams: list[AudioNode] = []

    for obj in object_list:
        node = node_from_object(obj)
        if node is None:
            continue
        if node.media_class == SINK:
            sinks.append(node.with_default(node.node_name == default_sink_name))
        elif node.media_class == SOURCE:
            sources.append(node.with_default(node.node_name == default_source_name))
        elif node.media_class == PLAYBACK_STREAM:
            playback_streams.append(node)
        elif node.media_class == CAPTURE_STREAM:
            capture_streams.append(node)

    streams = playback_streams + capture_streams
    return AudioSnapshot(
        generation=generation,
        connected=connected,
        sinks=tuple(sorted(sinks, key=sort_key)),
        sources=tuple(sorted(sources, key=sort_key)),
        playback_streams=tuple(sorted(playback_streams, key=sort_key)),
        capture_streams=tuple(sorted(capture_streams, key=sort_key)),
        applications=group_streams(streams),
        default_sink_name=default_sink_name,
        default_source_name=default_source_name,
    )
