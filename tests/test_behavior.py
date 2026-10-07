"""Tests for navigation, backend parsing, matching, and command behavior."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Orca's modules have an import cycle that only shows up when orca.extension is
# imported first. Importing the command manager first mirrors Orca's own startup
# order and avoids it. Orca itself is fully imported before it loads extensions.
import orca.command_manager  # noqa: E402,F401  pylint: disable=unused-import

from audio_manager import feedback
from audio_manager.backend import BackendCapabilities, OperationResult
from audio_manager.cli_backend import (
    CliBackend,
    _merge_object,
    parse_volume_output,
    volume_argument,
)
from audio_manager.commands import AudioCommands
from audio_manager.errors import MalformedBackendOutput, UnsupportedOperation
from audio_manager.models import (
    CAPTURE_STREAM,
    PLAYBACK_STREAM,
    SINK,
    SOURCE,
    ApplicationGroup,
    AudioNode,
    AudioSnapshot,
    VolumeState,
    parse_default_names,
)
from audio_manager.navigators import Navigator, step
from audio_manager.process_matcher import _descendants, match_playback_streams


def make_node(name, media_class=SINK, object_id=1, serial=None, percent=50, muted=False, **kwargs):
    """Returns a synthetic AudioNode."""

    return AudioNode(
        object_id=object_id,
        object_serial=serial if serial is not None else object_id + 1,
        stable_key=kwargs.pop("stable_key", f"{media_class}|{name}"),
        media_class=media_class,
        node_name=name,
        display_name=kwargs.pop("display_name", name),
        volume=VolumeState(percent / 100.0, muted),
        **kwargs,
    )


class FakeController:
    """Captures what would have been spoken."""

    def __init__(self, window=None):
        self.messages = []
        self.tones = []
        self.window = window

    def present_message_internal(self, message):
        self.messages.append(message)
        return True

    def play_tone_internal(self, duration, frequency, volume=1.0, wave="sine", interrupt=True):
        self.tones.append(frequency)
        return True

    def get_active_window_internal(self):
        return self.window


class FakeBackend:
    """Records the operations commands request, and completes them at once."""

    def __init__(self, snapshot):
        self.current = snapshot
        self.calls = []

    def snapshot(self):
        return self.current

    def capabilities(self):
        return BackendCapabilities(connected=self.current.connected)

    def change_volume(self, nodes, delta_percent, limit, done):
        self.calls.append(("change_volume", tuple(nodes), delta_percent, limit))
        done(
            OperationResult(
                attempted=len(nodes), succeeded=len(nodes), volume=VolumeState(0.5, False)
            )
        )

    def set_volume(self, nodes, value, limit, done):
        self.calls.append(("set_volume", tuple(nodes), value, limit))
        done(OperationResult(attempted=len(nodes), succeeded=len(nodes)))

    def set_mute(self, nodes, muted, done):
        self.calls.append(("set_mute", tuple(nodes), muted))
        done(OperationResult(attempted=len(nodes), succeeded=len(nodes)))

    def set_default(self, node, done):
        self.calls.append(("set_default", node))
        done(OperationResult(attempted=1, succeeded=1))

    def move_streams(self, streams, target, done):
        self.calls.append(("move_streams", tuple(streams), target))
        done(OperationResult(attempted=len(streams), succeeded=len(streams)))

    def clear_stream_targets(self, streams, done):
        self.calls.append(("clear_stream_targets", tuple(streams)))
        done(OperationResult(attempted=len(streams), succeeded=len(streams)))

    def run_background(self, work, done):
        self.calls.append(("run_background",))
        try:
            done(work())
        except Exception as error:  # pylint: disable=broad-exception-caught
            done(OperationResult.failure(error))


class FakeLock:
    """A microphone lock that records nothing and speaks nothing."""

    enabled = False

    def on_snapshot(self, snapshot):
        """Ignores graph changes."""

    def toggle(self, snapshot):
        """Returns a fixed message."""

        return "Microphone volume locked at 50 percent."

    def describe_target(self, node):
        """Returns no status."""

        return ""


class StepTest(unittest.TestCase):
    """Movement clamps by default and wraps for route candidates."""

    def setUp(self):
        self.items = [make_node("a", object_id=1), make_node("b", object_id=2)]

    def test_clamps_at_the_end(self):
        move = step(self.items, self.items[-1].stable_key, 1, wrap=False)
        self.assertTrue(move.at_boundary)
        self.assertEqual(move.item, self.items[-1])

    def test_wraps_when_requested(self):
        move = step(self.items, self.items[-1].stable_key, 1, wrap=True)
        self.assertFalse(move.at_boundary)
        self.assertEqual(move.item, self.items[0])

    def test_reports_an_empty_list(self):
        self.assertTrue(step([], None, 1, wrap=False).empty)

    def test_recovers_when_the_selection_disappeared(self):
        move = step(self.items, "Audio/Sink|gone", 1, wrap=False)
        self.assertTrue(move.previous_lost)
        self.assertEqual(move.item, self.items[0])


class VolumeParsingTest(unittest.TestCase):
    """wpctl output is matched strictly, never guessed."""

    def test_parses_volume_and_mute(self):
        self.assertEqual(parse_volume_output("Volume: 0.49\n").percent, 49)
        self.assertTrue(parse_volume_output("Volume: 0.49 [MUTED]\n").muted)

    def test_rejects_unexpected_output(self):
        with self.assertRaises(MalformedBackendOutput):
            parse_volume_output("Lautstarke: 0,49")

    def test_builds_relative_arguments(self):
        self.assertEqual(volume_argument(1), "1%+")
        self.assertEqual(volume_argument(-5), "5%-")


class MergeTest(unittest.TestCase):
    """Partial monitor updates must not discard cached properties."""

    def test_merges_params_without_losing_props(self):
        old = {
            "id": 1,
            "info": {"props": {"node.name": "sink"}, "params": {"Props": [{"mute": False}]}},
        }
        new = {"id": 1, "info": {"params": {"Props": [{"mute": True}]}}}
        merged = _merge_object(old, new)
        self.assertEqual(merged["info"]["props"]["node.name"], "sink")
        self.assertTrue(merged["info"]["params"]["Props"][0]["mute"])

    def test_metadata_update_keeps_unchanged_entries(self):
        # In monitor mode pw-dump resends a metadata object with only the
        # entries that changed, for example after a stream route is set.
        sink = {"subject": 0, "key": "default.audio.sink", "value": {"name": "out"}}
        source = {"subject": 0, "key": "default.audio.source", "value": {"name": "mic"}}
        route = {"subject": 77, "key": "target.object", "value": "400"}
        header = {
            "id": 40,
            "type": "PipeWire:Interface:Metadata",
            "props": {"metadata.name": "default"},
        }
        old = {**header, "metadata": [sink, source]}
        new = {**header, "metadata": [route]}
        merged = _merge_object(old, new)
        self.assertEqual(parse_default_names([merged]), ("out", "mic"))
        self.assertIn(route, merged["metadata"])

    def test_metadata_update_replaces_and_removes_entries(self):
        old = {
            "metadata": [
                {"subject": 0, "key": "default.audio.sink", "value": {"name": "out"}},
                {"subject": 0, "key": "custom", "value": "1"},
            ]
        }
        new = {
            "metadata": [
                {"subject": 0, "key": "default.audio.sink", "value": {"name": "headset"}},
                {"subject": 0, "key": "custom", "value": None},
            ]
        }
        merged = _merge_object(old, new)
        self.assertEqual(
            merged["metadata"],
            [{"subject": 0, "key": "default.audio.sink", "value": {"name": "headset"}}],
        )


class RouteValidationTest(unittest.TestCase):
    """Routing rejects impossible or refused moves."""

    def test_rejects_crossing_directions(self):
        stream = make_node("app", PLAYBACK_STREAM, object_id=10)
        source = make_node("mic", SOURCE, object_id=11)
        with self.assertRaises(UnsupportedOperation):
            CliBackend._validate_move(stream, source)

    def test_rejects_streams_marked_dont_move(self):
        stream = make_node("app", PLAYBACK_STREAM, object_id=10, dont_move=True)
        sink = make_node("out", SINK, object_id=11)
        with self.assertRaises(UnsupportedOperation):
            CliBackend._validate_move(stream, sink)


class ProcessMatchingTest(unittest.TestCase):
    """Window matching prefers strong evidence and refuses ambiguity."""

    def test_exact_pid_match_wins(self):
        wanted = make_node("app", PLAYBACK_STREAM, object_id=1, process_id=42)
        other = make_node("other", PLAYBACK_STREAM, object_id=2, process_id=7)
        result = match_playback_streams([wanted, other], 42)
        self.assertEqual(result.nodes, (wanted,))
        self.assertTrue(result.matched)

    def test_never_matches_on_name_alone(self):
        node = make_node("app", PLAYBACK_STREAM, object_id=1, application_name="Example")
        self.assertFalse(match_playback_streams([node], 42).matched)

    def test_ignores_screen_reader_streams(self):
        node = make_node(
            "speech-dispatcher",
            PLAYBACK_STREAM,
            object_id=1,
            process_id=42,
            is_screen_reader=True,
        )
        self.assertEqual(match_playback_streams([node], 42).reason, "no-streams")


class CommandTest(unittest.TestCase):
    """Commands always report something and act on the right nodes."""

    def setUp(self):
        self.sink = make_node("sink-a", SINK, object_id=1, percent=40, display_name="Headset")
        self.other_sink = make_node(
            "sink-b", SINK, object_id=2, percent=90, display_name="Speakers"
        )
        self.stream = make_node(
            "Player",
            PLAYBACK_STREAM,
            object_id=10,
            percent=70,
            process_id=99,
            application_name="Player",
        )
        self.group = ApplicationGroup(
            stable_key="pid:99",
            display_name="Player",
            application_id=None,
            process_ids=frozenset({99}),
            playback_nodes=(self.stream,),
        )
        self.snapshot = AudioSnapshot(
            connected=True,
            sinks=(self.sink, self.other_sink),
            playback_streams=(self.stream,),
            applications=(self.group,),
        )
        self.controller = FakeController()
        self.backend = FakeBackend(self.snapshot)
        self.commands = AudioCommands(
            self.backend,
            feedback.Presenter(self.controller),
            lambda key, default: default,
            self.controller,
            FakeLock(),
        )

    def test_volume_without_a_target_is_silent(self):
        self.commands.volume_up()
        self.assertEqual(self.controller.messages, [])
        self.assertEqual(self.backend.calls, [])

    def test_navigation_then_volume_acts_on_the_selection(self):
        self.commands.next_playback_device()
        self.commands.volume_up()
        self.assertEqual(self.backend.calls[0][0], "change_volume")
        self.assertEqual(self.backend.calls[0][1], (self.sink,))
        self.assertIn("Headset", self.controller.messages[-1])

    def test_boundary_plays_a_tone_and_stays_on_the_last_item(self):
        for _ in range(3):
            self.commands.next_playback_device()
        self.assertTrue(self.controller.tones)
        self.assertIn("Speakers", self.controller.messages[-1])

    def test_mute_toggles_the_whole_application(self):
        self.commands.next_audio_application()
        self.commands.toggle_mute()
        call = next(call for call in self.backend.calls if call[0] == "set_mute")
        self.assertEqual(call[1], (self.stream,))
        self.assertTrue(call[2])

    def test_default_requires_a_device_selection(self):
        self.commands.next_audio_application()
        messages = list(self.controller.messages)
        self.commands.set_default_device()
        self.assertEqual(self.controller.messages, messages)
        self.assertFalse(any(call[0] == "set_default" for call in self.backend.calls))

    def test_route_change_targets_a_sink(self):
        self.commands.next_audio_application()
        self.commands.next_application_output()
        call = next(call for call in self.backend.calls if call[0] == "move_streams")
        self.assertEqual(call[1], (self.stream,))
        self.assertTrue(call[2].is_sink)

    def test_reset_skips_screen_reader_streams(self):
        speech = make_node("orca", PLAYBACK_STREAM, object_id=20, is_screen_reader=True)
        self.backend.current = AudioSnapshot(
            connected=True, playback_streams=(self.stream, speech)
        )
        self.commands.reset_application_routes()
        call = next(call for call in self.backend.calls if call[0] == "clear_stream_targets")
        self.assertEqual(call[1], (self.stream,))

    def test_window_mute_refuses_without_a_window(self):
        self.commands.toggle_current_window_mute()
        self.assertEqual(self.controller.messages[-1], feedback.MSG_AMBIGUOUS_WINDOW)

    def test_disconnected_backend_is_reported(self):
        self.backend.current = AudioSnapshot(connected=False)
        self.commands.next_playback_device()
        self.assertEqual(self.controller.messages[-1], "PipeWire is not available.")

    def test_orca_routing_refuses_shared_speech(self):
        speech = make_node(
            "speech-dispatcher-espeak-ng",
            PLAYBACK_STREAM,
            object_id=30,
            process_binary="speech-dispatcher",
            is_screen_reader=True,
        )
        self.backend.current = AudioSnapshot(
            connected=True, sinks=(self.sink,), playback_streams=(speech,)
        )
        self.commands.next_orca_output()
        self.assertIn("shared speech service", self.controller.messages[-1])
        self.assertEqual([call for call in self.backend.calls if call[0] == "move_streams"], [])


class ReportTest(unittest.TestCase):
    """Reports include mute, default status, and mixed volumes."""

    def test_device_report_mentions_default_and_mute(self):
        node = make_node("sink", SINK, percent=42, muted=True, is_default=True)
        message = feedback.describe_device(node)
        self.assertIn("Muted", message)
        self.assertIn("default playback device", message)
        self.assertIn("42 percent", message)

    def test_application_report_uses_a_range_for_mixed_volumes(self):
        first = make_node("a", PLAYBACK_STREAM, object_id=1, percent=30)
        second = make_node("b", PLAYBACK_STREAM, object_id=2, percent=70)
        group = ApplicationGroup(
            stable_key="pid:1",
            display_name="Browser",
            application_id=None,
            process_ids=frozenset({1}),
            playback_nodes=(first, second),
        )
        message = feedback.describe_application(group)
        self.assertIn("mixed, range 30 to 70 percent", message)
        self.assertIn(feedback.MSG_FOLLOWS_DEFAULT, message)

    def test_capture_only_application_reports_recording(self):
        capture = make_node("mic", CAPTURE_STREAM, object_id=3, percent=80)
        group = ApplicationGroup(
            stable_key="pid:3",
            display_name="Recorder",
            application_id=None,
            process_ids=frozenset({3}),
            capture_nodes=(capture,),
        )
        self.assertIn("recording", feedback.describe_application(group))


class NavigatorSelectionTest(unittest.TestCase):
    """Changing application clears stale route positions."""

    def test_route_position_does_not_leak_between_applications(self):
        navigator = Navigator()
        first = ApplicationGroup("a", "A", None, frozenset())
        second = ApplicationGroup("b", "B", None, frozenset())
        navigator.select_application(first)
        navigator.state.application_sink_key = "Audio/Sink|x"
        navigator.select_application(second)
        self.assertIsNone(navigator.state.application_sink_key)


class MonitorBatchTest(unittest.TestCase):
    """Monitor batches rebuild the snapshot only when it can change."""

    SINK_OBJECT = {
        "id": 40,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {"media.class": SINK, "node.name": "out", "object.serial": 400}},
    }

    def setUp(self):
        self.backend = CliBackend()
        self.backend._schedule_publish = lambda: None

    def test_consume_applies_complete_arrays_and_keeps_the_rest(self):
        remainder = self.backend._consume(json.dumps([self.SINK_OBJECT]) + "\n[\n")
        self.assertEqual(remainder, "[\n")
        self.assertEqual(len(self.backend.snapshot().sinks), 1)

    def test_port_churn_does_not_rebuild(self):
        self.backend._apply_batch([self.SINK_OBJECT])
        generation = self.backend.snapshot().generation
        self.backend._apply_batch([{"id": 41, "type": "PipeWire:Interface:Port", "info": {}}])
        self.assertEqual(self.backend.snapshot().generation, generation)

    def test_node_removal_rebuilds(self):
        self.backend._apply_batch([self.SINK_OBJECT])
        self.backend._apply_batch([{"id": 40, "info": None}])
        self.assertEqual(self.backend.snapshot().sinks, ())


class DescendantWalkTest(unittest.TestCase):
    """The process-tree walk finds exactly the subtree below the root."""

    def test_includes_the_whole_subtree_only(self):
        parents = {10: 1, 11: 10, 12: 11, 20: 1, 21: 20}
        self.assertEqual(_descendants(parents, 10), frozenset({10, 11, 12}))

    def test_stops_on_a_cyclic_map(self):
        self.assertEqual(_descendants({5: 6, 6: 5}, 5), frozenset({5, 6}))


class ResultMessageTest(unittest.TestCase):
    """Partial failures report how many streams refused."""

    def test_counts_every_refusal(self):
        result = OperationResult(attempted=3, succeeded=1)
        self.assertIn("2 streams refused", feedback.result_message(result, "done"))


if __name__ == "__main__":
    unittest.main()
