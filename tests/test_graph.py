"""Tests for graph parsing, classification, and application grouping."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Orca's modules have an import cycle that only shows up when orca.extension is
# imported first. Importing the command manager first mirrors Orca's own startup
# order and avoids it. Orca itself is fully imported before it loads extensions.
import orca.command_manager  # noqa: E402,F401  pylint: disable=unused-import

from audio_manager.applications import build_snapshot, group_key_for, group_streams
from audio_manager.models import (
    is_screen_reader_identity,
    linear_to_scaled,
    node_from_object,
    parse_default_names,
)

SINK_NAME = "alsa_output.usb-Example_Headset-00.analog-stereo"
SOURCE_NAME = "alsa_input.usb-Example_Headset-00.mono-fallback"


def node_object(object_id, media_class, name, serial=None, props=None, volumes=None, mute=False):
    """Returns a synthetic pw-dump node object."""

    node_props = {
        "media.class": media_class,
        "node.name": name,
        "object.serial": serial if serial is not None else object_id + 1,
    }
    node_props.update(props or {})
    info = {"props": node_props, "state": "running", "unknown-future-field": 1}
    if volumes is not None:
        info["params"] = {"Props": [{"channelVolumes": list(volumes), "mute": mute}]}
    return {"id": object_id, "type": "PipeWire:Interface:Node", "version": 3, "info": info}


def default_metadata(sink=SINK_NAME, source=SOURCE_NAME):
    """Returns a synthetic default metadata object."""

    return {
        "id": 40,
        "type": "PipeWire:Interface:Metadata",
        "props": {"metadata.name": "default"},
        "metadata": [
            {"subject": 0, "key": "default.audio.sink", "value": {"name": sink}},
            {"subject": 0, "key": "default.audio.source", "value": {"name": source}},
            {"subject": 0, "key": "default.video.source", "value": {"name": "camera"}},
        ],
    }


class VolumeScaleTest(unittest.TestCase):
    """The graph stores linear amplitudes; wpctl presents a cubic scale."""

    def test_linear_to_scaled_matches_wpctl(self):
        self.assertEqual(round(linear_to_scaled(0.121239), 2), 0.49)
        self.assertEqual(linear_to_scaled(1.0), 1.0)
        self.assertEqual(linear_to_scaled(0.0), 0.0)


class NodeParsingTest(unittest.TestCase):
    """Parsing tolerates unknown fields and ignores uninteresting objects."""

    def test_parses_sink_with_volume(self):
        node = node_from_object(
            node_object(59, "Audio/Sink", SINK_NAME, serial=60, volumes=[0.125, 0.125])
        )
        self.assertIsNotNone(node)
        self.assertTrue(node.is_sink)
        self.assertEqual(node.object_serial, 60)
        self.assertEqual(node.display_name, SINK_NAME)
        self.assertEqual(node.volume.percent, 50)
        self.assertFalse(node.volume.muted)

    def test_prefers_description_then_nick_then_name(self):
        node = node_from_object(
            node_object(
                59,
                "Audio/Sink",
                SINK_NAME,
                props={"node.description": "Example Headset", "node.nick": "Headset"},
            )
        )
        self.assertEqual(node.display_name, "Example Headset")

    def test_ignores_non_audio_objects(self):
        self.assertIsNone(node_from_object({"id": 1, "type": "PipeWire:Interface:Port"}))
        self.assertIsNone(node_from_object(node_object(2, "Video/Source", "camera")))
        self.assertIsNone(node_from_object({"id": 3, "type": "PipeWire:Interface:Node"}))

    def test_detects_monitor_sources(self):
        node = node_from_object(node_object(5, "Audio/Source", f"{SINK_NAME}.monitor"))
        self.assertTrue(node.is_monitor)

    def test_stream_keys_include_the_serial(self):
        first = node_from_object(node_object(10, "Stream/Output/Audio", "Brave", serial=685))
        second = node_from_object(node_object(11, "Stream/Output/Audio", "Brave", serial=686))
        self.assertNotEqual(first.stable_key, second.stable_key)

    def test_device_keys_survive_identifier_changes(self):
        first = node_from_object(node_object(59, "Audio/Sink", SINK_NAME, serial=60))
        after_replug = node_from_object(node_object(77, "Audio/Sink", SINK_NAME, serial=94))
        self.assertEqual(first.stable_key, after_replug.stable_key)


class ScreenReaderIdentityTest(unittest.TestCase):
    """Screen reader audio must be recognized, without false positives."""

    def test_recognizes_speech_identities(self):
        self.assertTrue(is_screen_reader_identity("orca"))
        self.assertTrue(is_screen_reader_identity("speech-dispatcher-espeak-ng"))
        self.assertTrue(is_screen_reader_identity(None, "sd_dummy"))

    def test_does_not_match_unrelated_names(self):
        self.assertFalse(is_screen_reader_identity("OrcaSlicer"))
        self.assertFalse(is_screen_reader_identity("Firefox", "firefox"))


class GroupingTest(unittest.TestCase):
    """One application appears once, even with several streams."""

    def test_groups_playback_and_capture_of_one_process(self):
        streams = [
            node_from_object(
                node_object(
                    106,
                    "Stream/Output/Audio",
                    "Brave",
                    props={
                        "application.name": "Brave",
                        "application.process.id": 16169,
                        "application.process.binary": "brave",
                    },
                )
            ),
            node_from_object(
                node_object(
                    113,
                    "Stream/Input/Audio",
                    "Brave input",
                    props={
                        "application.name": "Brave input",
                        "application.process.id": 16169,
                        "application.process.binary": "brave",
                    },
                )
            ),
        ]
        groups = group_streams(streams)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].display_name, "Brave")
        self.assertTrue(groups[0].has_playback)
        self.assertTrue(groups[0].has_capture)

    def test_does_not_merge_anonymous_streams(self):
        streams = [
            node_from_object(node_object(1, "Stream/Output/Audio", "one")),
            node_from_object(node_object(2, "Stream/Output/Audio", "two")),
        ]
        self.assertEqual(len(group_streams(streams)), 2)

    def test_application_id_wins_over_process_identity(self):
        node = node_from_object(
            node_object(
                3,
                "Stream/Output/Audio",
                "music",
                props={"application.id": "org.example.Player", "application.process.id": 42},
            )
        )
        self.assertEqual(group_key_for(node), "app-id:org.example.Player")


class SnapshotTest(unittest.TestCase):
    """Snapshots classify nodes and mark the current defaults."""

    def setUp(self):
        self.objects = [
            default_metadata(),
            node_object(59, "Audio/Sink", SINK_NAME, volumes=[0.125, 0.125]),
            node_object(43, "Audio/Sink", "alsa_output.hdmi", volumes=[1.0, 1.0]),
            node_object(60, "Audio/Source", SOURCE_NAME, volumes=[1.0]),
            node_object(61, "Audio/Source", f"{SINK_NAME}.monitor"),
            node_object(
                94,
                "Stream/Output/Audio",
                "speech-dispatcher-espeak-ng",
                props={"application.process.binary": "speech-dispatcher"},
            ),
            node_object(
                106,
                "Stream/Output/Audio",
                "Brave",
                props={"application.name": "Brave", "application.process.id": 16169},
            ),
            {"id": 7, "type": "PipeWire:Interface:Link", "info": {"props": {}}},
        ]
        self.snapshot = build_snapshot(self.objects, generation=1)

    def test_classifies_nodes(self):
        self.assertEqual(len(self.snapshot.sinks), 2)
        self.assertEqual(len(self.snapshot.sources), 2)
        self.assertEqual(len(self.snapshot.playback_streams), 2)

    def test_marks_defaults(self):
        self.assertEqual(self.snapshot.default_sink().node_name, SINK_NAME)
        self.assertEqual(self.snapshot.default_source().node_name, SOURCE_NAME)

    def test_hides_monitors_and_screen_reader_by_default(self):
        self.assertEqual(len(self.snapshot.visible_sources()), 1)
        self.assertEqual(len(self.snapshot.visible_sources(include_monitors=True)), 2)
        visible = self.snapshot.visible_applications()
        self.assertEqual([group.display_name for group in visible], ["Brave"])
        self.assertEqual(len(self.snapshot.visible_applications(include_screen_reader=True)), 2)

    def test_removed_objects_are_not_parsed(self):
        remaining = [obj for obj in self.objects if obj.get("id") != 59]
        snapshot = build_snapshot(remaining)
        self.assertIsNone(snapshot.node_by_key(f"Audio/Sink|{SINK_NAME}"))

    def test_parses_only_the_default_metadata_object(self):
        objects = [
            {
                "id": 42,
                "type": "PipeWire:Interface:Metadata",
                "props": {"metadata.name": "route-settings"},
                "metadata": [{"subject": 0, "key": "default.audio.sink", "value": {"name": "no"}}],
            },
            default_metadata(),
        ]
        self.assertEqual(parse_default_names(objects), (SINK_NAME, SOURCE_NAME))


if __name__ == "__main__":
    unittest.main()
