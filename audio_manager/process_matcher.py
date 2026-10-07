"""Conservative matching between the active window and PipeWire streams.

PipeWire properties supplied by clients are hints, not authorization identities.
Compatibility layers can publish the PID of a bridge or service rather than of
the application the user is looking at. An announced refusal is therefore better
than muting audio that belongs to something else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .applications import group_key_for
from .models import AudioNode

__all__ = [
    "MatchResult",
    "active_window_pid",
    "descendant_pids",
    "match_playback_streams",
]

_MAX_PROCESSES = 8192


@dataclass(frozen=True)
class MatchResult:
    """The outcome of matching the active window to playback streams."""

    nodes: tuple[AudioNode, ...] = ()
    ambiguous: bool = False
    reason: str = ""

    @property
    def matched(self) -> bool:
        """Returns True if a safe, unambiguous match was found."""

        return bool(self.nodes) and not self.ambiguous


def active_window_pid(controller: Any) -> int | None:
    """Returns the process ID of Orca's active window, or None.

    Only the documented read-only controller call and standard AT-SPI methods
    are used.
    """

    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        return None

    window = controller.get_active_window_internal()
    if window is None:
        return None

    try:
        application = Atspi.Accessible.get_application(window)
        if application is None:
            return None
        pid = Atspi.Accessible.get_process_id(application)
    except Exception:  # pylint: disable=broad-exception-caught
        return None

    return int(pid) if isinstance(pid, int) and pid > 0 else None


def _parent_pid(pid: int) -> int | None:
    """Returns the parent PID of pid by reading /proc, or None."""

    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as stat_file:
            contents = stat_file.read()
    except OSError:
        return None

    # The comm field is parenthesized and may itself contain spaces, so the
    # fields after it are located relative to the final closing parenthesis.
    close = contents.rfind(")")
    if close < 0:
        return None
    fields = contents[close + 1 :].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def descendant_pids(root_pid: int, limit: int = _MAX_PROCESSES) -> frozenset[int]:
    """Returns root_pid together with its descendants.

    The walk is bounded so that a pathological process tree cannot make the
    worker thread run for an unbounded time.
    """

    if root_pid <= 0:
        return frozenset()

    parents: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return frozenset({root_pid})

    for entry in entries:
        if not entry.isdigit():
            continue
        if len(parents) >= limit:
            break
        pid = int(entry)
        parent = _parent_pid(pid)
        if parent is not None:
            parents[pid] = parent

    return _descendants(parents, root_pid)


def _descendants(parents: dict[int, int], root_pid: int) -> frozenset[int]:
    """Returns root_pid and every process below it in the parents map.

    Walks down from the root once, so the cost is linear in the number of
    processes. The visited set also stops a malformed, cyclic map.
    """

    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)

    result = {root_pid}
    pending = [root_pid]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in result:
                result.add(child)
                pending.append(child)
    return frozenset(result)


def _distinct_group_count(nodes: Iterable[AudioNode]) -> int:
    """Returns how many distinct applications the nodes belong to."""

    return len({group_key_for(node) for node in nodes})


def match_playback_streams(
    streams: Sequence[AudioNode],
    pid: int | None,
    application_identity: str | None = None,
) -> MatchResult:
    """Matches playback streams to the active window.

    The policy runs from strongest to weakest evidence and stops at the first
    level that produces a match:

    1. an exact process ID match on a playback stream;
    2. a stream whose process is a descendant of the window's process;
    3. a unique application identity match.

    Display names alone are never used, and screen-reader streams are never
    candidates.
    """

    candidates = [
        node for node in streams if node.is_playback_stream and not node.is_screen_reader
    ]
    if not candidates:
        return MatchResult(reason="no-streams")

    if pid is not None:
        exact = tuple(node for node in candidates if node.process_id == pid)
        if exact:
            return MatchResult(nodes=exact)

        family = descendant_pids(pid)
        related = tuple(
            node
            for node in candidates
            if node.process_id is not None and node.process_id in family
        )
        if related:
            if _distinct_group_count(related) > 1:
                return MatchResult(nodes=related, ambiguous=True, reason="multiple-applications")
            return MatchResult(nodes=related)

    if application_identity:
        identity = application_identity.strip().casefold()
        by_identity = tuple(
            node
            for node in candidates
            if (node.application_id or "").strip().casefold() == identity
        )
        if by_identity:
            if _distinct_group_count(by_identity) > 1:
                return MatchResult(
                    nodes=by_identity, ambiguous=True, reason="multiple-applications"
                )
            return MatchResult(nodes=by_identity)

    return MatchResult(reason="no-match")
