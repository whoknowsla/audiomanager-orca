"""Phase-one backend built on pw-dump, wpctl, and pw-metadata.

Design constraints that this module exists to satisfy:

* No subprocess call, graph parse, or PipeWire round trip may happen on Orca's
  main thread. One reader thread owns the long-lived ``pw-dump --monitor``
  child; one serialized worker owns every one-shot process.
* Results cross back into the main thread as immutable snapshots and result
  objects through ``GLib.idle_add()``.
* Commands are built as argv lists. No shell, no interpolation of labels, and
  no non-numeric identifiers are ever passed to a child process.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
from functools import partial
from typing import Any, Callable, Sequence

from gi.repository import GLib

from . import applications
from .backend import BackendCapabilities, OperationResult, ResultCallback
from .errors import (
    AudioManagerError,
    CommandMissing,
    ConnectionFailed,
    MalformedBackendOutput,
    ObjectDisappeared,
    OperationTimedOut,
    PermissionDenied,
    UnsupportedOperation,
)
from .models import (
    METADATA_TYPE,
    NODE_TYPE,
    SINK,
    SOURCE,
    AudioNode,
    AudioSnapshot,
    VolumeState,
)

__all__ = ["CliBackend", "parse_volume_output", "volume_argument"]

_VOLUME_LINE = re.compile(r"^Volume:\s*([0-9]+(?:\.[0-9]+)?)\s*(\[MUTED\])?\s*$")

# A malformed or hostile monitor stream must not be able to exhaust memory.
# The stream is decoded text, so the limit counts characters.
_MAX_BUFFER_CHARS = 16 * 1024 * 1024
_RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
_PERMISSION_HINTS = ("permission", "not allowed", "access denied", "denied")
# Only these object types feed a snapshot. Batches that touch nothing else,
# such as port and link churn, do not need a rebuild.
_SNAPSHOT_TYPES = frozenset({NODE_TYPE, METADATA_TYPE})


def parse_volume_output(text: str) -> VolumeState:
    """Parses the output of ``wpctl get-volume``.

    The child runs under ``LC_ALL=C`` and the format is matched strictly. A
    parse failure is an error, never a guessed value.
    """

    for line in text.splitlines():
        match = _VOLUME_LINE.match(line.strip())
        if match:
            return VolumeState(float(match.group(1)), bool(match.group(2)))
    raise MalformedBackendOutput(f"Unparsable volume output: {text.strip()[:120]!r}")


def volume_argument(delta_percent: int) -> str:
    """Returns the wpctl relative-volume argument for delta_percent."""

    magnitude = abs(int(delta_percent))
    direction = "+" if delta_percent >= 0 else "-"
    return f"{magnitude}%{direction}"


def _child_env() -> dict[str, str]:
    """Returns the environment for child processes."""

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env.pop("LANGUAGE", None)
    return env


def _merge_metadata(old: Any, new: Any) -> Any:
    """Merges changed metadata entries into the cached entries.

    In monitor mode ``pw-dump`` resends a metadata object with only the entries
    that changed. Replacing the cached list would drop unrelated entries, such
    as the default devices whenever a stream route is set. Entries are keyed by
    subject and key; a null value removes one. A deleted key is not reported at
    all, so its last value stays cached until the next full dump; for the
    default devices that is harmless, because a stale name matches no node.
    """

    if not isinstance(old, list) or not isinstance(new, list):
        return new
    entries: dict[tuple[Any, Any], dict[str, Any]] = {}
    for entry in old + new:
        if isinstance(entry, dict):
            entries[(entry.get("subject"), entry.get("key"))] = entry
    return [entry for entry in entries.values() if entry.get("value") is not None]


def _merge_object(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Merges a monitor update over a cached object.

    ``pw-dump --monitor`` may report only the parts of an object that changed,
    so replacing the cached object wholesale can lose properties that are still
    valid.
    """

    merged = dict(old)
    for key, value in new.items():
        if key == "metadata":
            merged[key] = _merge_metadata(old.get(key), value)
            continue
        if key != "info" or not isinstance(value, dict):
            merged[key] = value
            continue
        info = dict(old.get("info") or {})
        for info_key, info_value in value.items():
            if info_key == "params" and isinstance(info_value, dict):
                params = dict(info.get("params") or {})
                params.update(info_value)
                info["params"] = params
            else:
                info[info_key] = info_value
        merged["info"] = info
    return merged


class _Job:
    """One unit of serialized backend work."""

    def __init__(self, run: Callable[[], OperationResult], done: ResultCallback | None) -> None:
        self.run = run
        self.done = done


class CliBackend:
    """An audio backend driven by the standard PipeWire command line tools."""

    def __init__(self, timeout_provider: Callable[[], float] | None = None) -> None:
        self._timeout_provider = timeout_provider or (lambda: 2.0)
        self._lock = threading.Lock()
        self._cache: dict[int, dict[str, Any]] = {}
        self._snapshot = AudioSnapshot()
        self._generation = 0
        self._stop = threading.Event()
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._monitor_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._on_change: Callable[[AudioSnapshot], None] | None = None
        self._on_disconnect: Callable[[AudioManagerError], None] | None = None
        self._publish_pending = False
        self._paths: dict[str, str | None] = {}
        self._env = _child_env()
        self._capabilities = BackendCapabilities()

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        on_change: Callable[[AudioSnapshot], None],
        on_disconnect: Callable[[AudioManagerError], None],
    ) -> None:
        """Starts the monitor and worker threads."""

        self._on_change = on_change
        self._on_disconnect = on_disconnect
        self._resolve_tools()

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="audio-manager-worker", daemon=True
        )
        self._worker_thread.start()

        missing = tuple(name for name, path in self._paths.items() if path is None)
        if missing:
            self._capabilities = BackendCapabilities(missing_commands=missing)
            self._report_disconnect(CommandMissing(", ".join(missing)))
            return

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="audio-manager-monitor", daemon=True
        )
        self._monitor_thread.start()

    def close(self) -> None:
        """Stops everything promptly and without waiting on the network."""

        self._stop.set()
        self._on_change = None
        self._on_disconnect = None
        self._jobs.put(None)

        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

        for thread in (self._monitor_thread, self._worker_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.1)
        self._monitor_thread = None
        self._worker_thread = None

    def _resolve_tools(self) -> None:
        """Resolves the executables once, so no lookup happens per command."""

        for name in ("pw-dump", "wpctl", "pw-metadata"):
            self._paths[name] = shutil.which(name)

    def capabilities(self) -> BackendCapabilities:
        """Returns what this backend can currently do."""

        return self._capabilities

    def snapshot(self) -> AudioSnapshot:
        """Returns the most recent immutable graph snapshot."""

        with self._lock:
            return self._snapshot

    # -- monitor thread ----------------------------------------------------

    def _monitor_loop(self) -> None:
        """Owns the long-lived pw-dump monitor and restarts it with backoff."""

        attempt = 0
        while not self._stop.is_set():
            try:
                restart_reason = self._run_monitor_once()
            except OSError as error:
                restart_reason = ConnectionFailed(str(error))

            if self._stop.is_set():
                return

            self._set_connected(False)
            self._report_disconnect(restart_reason or ConnectionFailed("pw-dump exited"))
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            if self._stop.wait(delay):
                return

    def _run_monitor_once(self) -> AudioManagerError | None:
        """Runs one pw-dump monitor session and returns why it ended."""

        path = self._paths.get("pw-dump")
        if path is None:
            return CommandMissing("pw-dump")

        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            [path, "--monitor", "--no-colors"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._env,
            text=True,
            bufsize=1,
        )
        self._process = process
        self._reset_cache()

        if process.stdout is None:
            self._terminate(process)
            return ConnectionFailed("pw-dump produced no output stream")

        # Lines are collected and joined once per top-level array, because
        # repeated string concatenation is quadratic on the large initial dump.
        lines: list[str] = []
        size = 0
        while not self._stop.is_set():
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line)
            size += len(line)
            if size > _MAX_BUFFER_CHARS:
                self._terminate(process)
                return MalformedBackendOutput("monitor output exceeded the buffer limit")
            if line.rstrip() != "]":
                continue
            remainder = self._consume("".join(lines))
            lines = [remainder] if remainder else []
            size = len(remainder)

        self._terminate(process)
        return None

    def _consume(self, buffer: str) -> str:
        """Decodes every complete JSON array in buffer and applies it."""

        decoder = json.JSONDecoder()
        index = 0
        length = len(buffer)
        while index < length:
            while index < length and buffer[index] in " \t\r\n":
                index += 1
            if index >= length:
                break
            try:
                value, end = decoder.raw_decode(buffer, index)
            except json.JSONDecodeError:
                break
            index = end
            if isinstance(value, list):
                self._apply_batch(value)
        return buffer[index:]

    def _reset_cache(self) -> None:
        """Drops every cached object, invalidating all transient identifiers."""

        with self._lock:
            self._cache.clear()

    def _apply_batch(self, objects: Sequence[Any]) -> None:
        """Applies one monitor batch to the cache and publishes a snapshot.

        Only the monitor thread writes the cache and the generation, so the
        snapshot is built outside the lock. That keeps ``snapshot()`` on Orca's
        main thread from waiting on a graph parse.
        """

        relevant = False
        with self._lock:
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                object_id = obj.get("id")
                if not isinstance(object_id, int):
                    continue
                if "info" in obj and obj["info"] is None:
                    removed = self._cache.pop(object_id, None)
                    relevant = relevant or (
                        removed is not None and removed.get("type") in _SNAPSHOT_TYPES
                    )
                    continue
                cached = self._cache.get(object_id)
                merged = _merge_object(cached, obj) if cached else obj
                self._cache[object_id] = merged
                relevant = relevant or merged.get("type") in _SNAPSHOT_TYPES
            if not relevant and self._snapshot.connected:
                return
            objects_now = list(self._cache.values())
            self._generation += 1
            generation = self._generation

        snapshot = applications.build_snapshot(objects_now, generation, connected=True)
        capabilities = self._capabilities_for(snapshot)
        with self._lock:
            self._snapshot = snapshot
            self._capabilities = capabilities
        self._schedule_publish()

    def _capabilities_for(self, snapshot: AudioSnapshot) -> BackendCapabilities:
        """Returns the capabilities implied by a successful graph read."""

        return BackendCapabilities(
            connected=True,
            can_read_graph=True,
            can_set_volume=self._paths.get("wpctl") is not None,
            can_set_default=self._paths.get("wpctl") is not None,
            can_move_streams=self._paths.get("pw-metadata") is not None,
            detail=f"{len(snapshot.all_nodes())} audio nodes",
        )

    def _set_connected(self, connected: bool) -> None:
        """Marks the backend connected or disconnected."""

        with self._lock:
            self._capabilities = BackendCapabilities(
                connected=connected,
                can_read_graph=connected,
                missing_commands=self._capabilities.missing_commands,
            )
            if not connected:
                self._cache.clear()
                self._snapshot = AudioSnapshot(generation=self._snapshot.generation)
        self._schedule_publish()

    def _schedule_publish(self) -> None:
        """Marshals the newest snapshot to the main thread at most once."""

        with self._lock:
            if self._publish_pending or self._stop.is_set():
                return
            self._publish_pending = True
        GLib.idle_add(self._publish, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _publish(self) -> bool:
        """Delivers the newest snapshot on the main thread."""

        with self._lock:
            self._publish_pending = False
            snapshot = self._snapshot
        callback = self._on_change
        if callback is not None and not self._stop.is_set():
            callback(snapshot)
        return False

    def _on_main_thread(self, action: Callable[[], None]) -> None:
        """Runs action on Orca's main thread unless the backend is stopping."""

        if self._stop.is_set():
            return

        def deliver() -> bool:
            if not self._stop.is_set():
                action()
            return False

        GLib.idle_add(deliver)

    def _report_disconnect(self, error: AudioManagerError) -> None:
        """Reports a disconnect on the main thread."""

        def notify() -> None:
            # Read at delivery time: close() clears the callback.
            callback = self._on_disconnect
            if callback is not None:
                callback(error)

        self._on_main_thread(notify)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        """Ends a child process without ever searching for other processes."""

        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
        except OSError:
            pass

    # -- worker thread -----------------------------------------------------

    def _worker_loop(self) -> None:
        """Runs queued operations one at a time, in order."""

        while True:
            job = self._jobs.get()
            if job is None or self._stop.is_set():
                return
            try:
                result = job.run()
            except AudioManagerError as error:
                result = OperationResult.failure(error)
            except Exception as error:  # pylint: disable=broad-exception-caught
                result = OperationResult.failure(UnsupportedOperation(str(error)))
            if job.done is not None:
                self._on_main_thread(partial(job.done, result))

    def _submit(self, run: Callable[[], OperationResult], done: ResultCallback | None) -> None:
        """Queues one operation for the worker."""

        if self._stop.is_set():
            return
        self._jobs.put(_Job(run, done))

    def run_background(
        self,
        work: Callable[[], OperationResult],
        done: ResultCallback,
    ) -> None:
        """Runs work on the serialized worker thread."""

        self._submit(work, done)

    def _run(self, name: str, args: Sequence[str]) -> str:
        """Runs one short-lived tool and returns its stdout."""

        path = self._paths.get(name)
        if path is None:
            raise CommandMissing(name)

        for value in args:
            if not isinstance(value, str):
                raise UnsupportedOperation(f"Invalid argument for {name}")
        argv = [path, *args]

        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                capture_output=True,
                text=True,
                env=self._env,
                timeout=max(0.2, self._timeout_provider()),
                check=False,
            )
        except FileNotFoundError as error:
            raise CommandMissing(name) from error
        except subprocess.TimeoutExpired as error:
            raise OperationTimedOut(name) from error

        if completed.returncode != 0:
            stderr = (completed.stderr or "")[:200].strip()
            low = stderr.lower()
            if any(hint in low for hint in _PERMISSION_HINTS):
                raise PermissionDenied(stderr)
            raise UnsupportedOperation(stderr or f"{name} exited with {completed.returncode}")
        return completed.stdout or ""

    # -- identifier resolution --------------------------------------------

    def _resolve(self, node: AudioNode) -> AudioNode:
        """Re-resolves node against the newest snapshot before writing.

        A transient object ID can be reused by an unrelated object after a
        hotplug, so the identifier is always re-derived from the stable key
        immediately before a write.
        """

        fresh = self.snapshot().node_by_key(node.stable_key)
        if fresh is None:
            raise ObjectDisappeared(node.display_name)
        return fresh

    @staticmethod
    def _identifier(node: AudioNode) -> str:
        """Returns the validated numeric identifier for a node."""

        if not isinstance(node.object_id, int) or node.object_id < 0:
            raise ObjectDisappeared(node.display_name)
        return str(node.object_id)

    @staticmethod
    def _serial(node: AudioNode) -> str:
        """Returns the validated object serial used as a route target."""

        if not isinstance(node.object_serial, int) or node.object_serial < 0:
            raise UnsupportedOperation(f"{node.display_name} has no object serial")
        return str(node.object_serial)

    def _for_each(
        self,
        nodes: Sequence[AudioNode],
        action: Callable[[AudioNode], None],
    ) -> OperationResult:
        """Applies action to every node, counting successes and failures."""

        attempted = len(nodes)
        succeeded = 0
        resolved: list[AudioNode] = []
        first_error: AudioManagerError | None = None

        for node in nodes:
            try:
                fresh = self._resolve(node)
                action(fresh)
            except AudioManagerError as error:
                first_error = first_error or error
                continue
            succeeded += 1
            resolved.append(fresh)

        return OperationResult(
            attempted=attempted,
            succeeded=succeeded,
            error=first_error if succeeded == 0 else None,
            nodes=tuple(resolved),
            detail=first_error.detail if first_error else "",
        )

    # -- operations --------------------------------------------------------

    def get_volume(self, node: AudioNode, done: ResultCallback) -> None:
        """Reads the authoritative volume state of node."""

        def run() -> OperationResult:
            fresh = self._resolve(node)
            text = self._run("wpctl", ["get-volume", self._identifier(fresh)])
            return OperationResult(
                attempted=1,
                succeeded=1,
                volume=parse_volume_output(text),
                nodes=(fresh,),
            )

        self._submit(run, done)

    def set_volume(
        self,
        nodes: Sequence[AudioNode],
        value: float,
        limit: float,
        done: ResultCallback,
    ) -> None:
        """Sets an absolute volume on every node."""

        target = max(0.0, min(float(value), float(limit)))
        self._write_volume(nodes, f"{target:.4f}", limit, done)

    def change_volume(
        self,
        nodes: Sequence[AudioNode],
        delta_percent: int,
        limit: float,
        done: ResultCallback,
    ) -> None:
        """Steps the volume of every node by delta_percent percentage points."""

        self._write_volume(nodes, volume_argument(delta_percent), limit, done)

    def _write_volume(
        self,
        nodes: Sequence[AudioNode],
        argument: str,
        limit: float,
        done: ResultCallback,
    ) -> None:
        """Runs ``wpctl set-volume`` with argument on every node, then reads back."""

        node_list = list(nodes)
        limit_argument = f"{float(limit):.4f}"

        def run() -> OperationResult:
            def action(node: AudioNode) -> None:
                self._run(
                    "wpctl",
                    ["set-volume", self._identifier(node), argument, "--limit", limit_argument],
                )

            return self._with_volume_readback(self._for_each(node_list, action))

        self._submit(run, done)

    def _with_volume_readback(self, result: OperationResult) -> OperationResult:
        """Adds the confirmed volume of the first changed node to result."""

        if not result.nodes:
            return result
        try:
            text = self._run("wpctl", ["get-volume", self._identifier(result.nodes[0])])
            volume = parse_volume_output(text)
        except AudioManagerError:
            return result
        return OperationResult(
            attempted=result.attempted,
            succeeded=result.succeeded,
            error=result.error,
            volume=volume,
            detail=result.detail,
            nodes=result.nodes,
        )

    def set_mute(self, nodes: Sequence[AudioNode], muted: bool, done: ResultCallback) -> None:
        """Sets the mute state of every node."""

        node_list = list(nodes)
        value = "1" if muted else "0"

        def run() -> OperationResult:
            def action(node: AudioNode) -> None:
                self._run("wpctl", ["set-mute", self._identifier(node), value])

            return self._for_each(node_list, action)

        self._submit(run, done)

    def set_default(self, node: AudioNode, done: ResultCallback) -> None:
        """Makes node the default sink or source."""

        def run() -> OperationResult:
            fresh = self._resolve(node)
            if fresh.media_class not in (SINK, SOURCE):
                raise UnsupportedOperation("Only devices can be made default")
            self._run("wpctl", ["set-default", self._identifier(fresh)])
            return OperationResult(attempted=1, succeeded=1, nodes=(fresh,))

        self._submit(run, done)

    def move_streams(
        self,
        streams: Sequence[AudioNode],
        target: AudioNode,
        done: ResultCallback,
    ) -> None:
        """Routes every stream to target by setting target.object metadata."""

        stream_list = list(streams)

        def run() -> OperationResult:
            fresh_target = self._resolve(target)
            serial = self._serial(fresh_target)

            def action(stream: AudioNode) -> None:
                self._validate_move(stream, fresh_target)
                self._run(
                    "pw-metadata",
                    [
                        "-n",
                        "default",
                        self._identifier(stream),
                        "target.object",
                        serial,
                        "Spa:Id",
                    ],
                )

            result = self._for_each(stream_list, action)
            return OperationResult(
                attempted=result.attempted,
                succeeded=result.succeeded,
                error=result.error,
                detail=result.detail,
                nodes=(fresh_target,),
            )

        self._submit(run, done)

    def clear_stream_targets(self, streams: Sequence[AudioNode], done: ResultCallback) -> None:
        """Returns every stream to policy-selected routing."""

        stream_list = list(streams)

        def run() -> OperationResult:
            def action(stream: AudioNode) -> None:
                self._run(
                    "pw-metadata",
                    ["-n", "default", "-d", self._identifier(stream), "target.object"],
                )

            return self._for_each(stream_list, action)

        self._submit(run, done)

    @staticmethod
    def _validate_move(stream: AudioNode, target: AudioNode) -> None:
        """Rejects a route change that PipeWire or policy would not honor."""

        if stream.dont_move:
            raise UnsupportedOperation(f"{stream.display_name} cannot be moved")
        if stream.is_playback_stream and not target.is_sink:
            raise UnsupportedOperation("Playback streams can only target playback devices")
        if stream.is_capture_stream and not target.is_source:
            raise UnsupportedOperation("Capture streams can only target recording devices")
        if not stream.is_playback_stream and not stream.is_capture_stream:
            raise UnsupportedOperation("Only application streams can be routed")
