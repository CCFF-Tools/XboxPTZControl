import queue
import json
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamdeck_control import (
    ActionKind,
    DeckAction,
    StreamDeckController,
    ThumbnailStore,
    camera_label_lines,
    map_key_action,
    preset_recall_packet,
    preset_set_packet,
    resolve_deck_action,
)


class StreamDeckControlTests(unittest.TestCase):
    def test_key_mapping_adapts_to_key_count(self):
        self.assertEqual(map_key_action(0, 6).kind, ActionKind.PREVIOUS_CAMERA)
        self.assertEqual(map_key_action(1, 6).kind, ActionKind.NEXT_CAMERA)
        self.assertEqual(map_key_action(2, 6).kind, ActionKind.TOGGLE_SAVE)
        self.assertEqual(map_key_action(3, 6), DeckAction(ActionKind.PRESET, 1))
        self.assertEqual(map_key_action(5, 6), DeckAction(ActionKind.PRESET, 3))
        self.assertIsNone(map_key_action(6, 6))

    def test_exact_visca_preset_packets_and_bounds(self):
        self.assertEqual(preset_set_packet(1), bytes.fromhex("81 01 04 3f 01 01 ff"))
        self.assertEqual(preset_recall_packet(12), bytes.fromhex("81 01 04 3f 02 0c ff"))
        for helper in (preset_set_packet, preset_recall_packet):
            for value in (0, 100):
                with self.assertRaises(ValueError):
                    helper(value)

    def test_callback_enqueues_save_toggle_and_preset(self):
        actions = queue.Queue()
        controller = StreamDeckController(actions)

        class Deck:
            def key_count(self):
                return 5

        controller._key_callback(Deck(), 2, True)
        controller._key_callback(Deck(), 3, True)
        self.assertEqual(actions.get_nowait(), DeckAction(ActionKind.TOGGLE_SAVE))
        self.assertEqual(actions.get_nowait(), DeckAction(ActionKind.PRESET, 1))

    def test_save_toggle_then_preset_sets_and_disarms(self):
        armed, packet, label = resolve_deck_action(DeckAction(ActionKind.TOGGLE_SAVE), False)
        self.assertTrue(armed)
        self.assertIsNone(packet)
        armed, packet, label = resolve_deck_action(DeckAction(ActionKind.PRESET, 4), armed)
        self.assertFalse(armed)
        self.assertEqual(packet, preset_set_packet(4))
        self.assertEqual(label, "preset-set")

    def test_normal_preset_recalls(self):
        armed, packet, label = resolve_deck_action(DeckAction(ActionKind.PRESET, 2), False)
        self.assertFalse(armed)
        self.assertEqual(packet, preset_recall_packet(2))
        self.assertEqual(label, "preset-recall")

    def test_snapshot_is_json_safe(self):
        controller = StreamDeckController(queue.Queue())
        controller._device = controller._device_name(type("Fake", (), {"id": lambda self: "Deck Mini"})())
        json.dumps(controller.snapshot())
        self.assertEqual(controller.snapshot()["device"], "Deck Mini")

    def test_ptzpad_starts_deck_before_joystick_wait(self):
        source = Path(__file__).parents[1].joinpath("ptzpad.py").read_text()
        self.assertLess(source.index("_streamdeck.start()"), source.rindex("js = wait_for_joystick()"))
        body = source[source.index("def wait_for_joystick"):source.index("_streamdeck = StreamDeckController")]
        self.assertIn("process_streamdeck_actions()", body)

    def test_configure_disable_closes_fake_and_snapshot_is_safe(self):
        class FakeDeck:
            def __init__(self):
                self.reset_count = 0
                self.close_count = 0
                self.brightness = None
            def reset(self): self.reset_count += 1
            def close(self): self.close_count += 1
            def set_brightness(self, value): self.brightness = value

        controller = StreamDeckController(queue.Queue())
        fake = FakeDeck()
        controller._deck = fake
        controller.configure(enabled=False, brightness=20)
        self.assertEqual((fake.reset_count, fake.close_count), (1, 1))
        self.assertFalse(controller.snapshot()["enabled"])
        json.dumps(controller.snapshot())

    def test_configure_brightness_applies_to_open_fake(self):
        class FakeDeck:
            def set_brightness(self, value): self.brightness = value
        controller = StreamDeckController(queue.Queue())
        fake = FakeDeck(); controller._deck = fake
        controller.configure(enabled=True, brightness=72)
        self.assertEqual(fake.brightness, 72)
        self.assertEqual(controller.snapshot()["brightness"], 72)

    def test_installer_prefers_verified_os_package_then_pip_fallback(self):
        source = Path(__file__).parents[1].joinpath("install.sh").read_text()
        import_check = source.index("/usr/bin/python3 -c 'import StreamDeck'")
        apt_package = source.index("python3-elgato-streamdeck")
        pip_fallback = source.index("pip3 install streamdeck")
        self.assertLess(import_check, apt_package)
        self.assertLess(apt_package, pip_fallback)
        self.assertIn("apt-cache show python3-elgato-streamdeck", source)

    def test_renderer_supports_legacy_pilhelper_api(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is unavailable in this environment")

        class LegacyHelper:
            @staticmethod
            def create_image(deck):
                return Image.new("RGBA", (64, 32))

            @staticmethod
            def to_native_format(deck, image):
                return image.tobytes()

        class FakeDeck:
            def __init__(self):
                self.images = []

            def key_count(self):
                return 4

            def set_key_image(self, key, image):
                self.images.append((key, image))

        streamdeck_module = types.ModuleType("StreamDeck")
        helpers_module = types.ModuleType("StreamDeck.ImageHelpers")
        helpers_module.PILHelper = LegacyHelper
        streamdeck_module.ImageHelpers = helpers_module
        deck = FakeDeck()
        controller = StreamDeckController(queue.Queue())
        controller._deck = deck
        with patch.dict(sys.modules, {"StreamDeck": streamdeck_module, "StreamDeck.ImageHelpers": helpers_module}):
            controller._render()
        self.assertEqual(len(deck.images), 4)
        self.assertIsNotNone(controller.snapshot()["last_render_at"])
        self.assertIsNone(controller.snapshot()["last_error"])

    def test_renderer_uses_native_image_size(self):
        source = Path(__file__).parents[1].joinpath("streamdeck_control.py").read_text()
        self.assertIn("native_image.size", source)
        self.assertNotIn('size["width"]', source)
        self.assertNotIn('size["height"]', source)

    def test_snapshot_validation_and_safe_thumbnail_path(self):
        from streamdeck_control import ThumbnailStore, validate_snapshot
        jpeg = b"\xff\xd8" + b"x" * 20 + b"\xff\xd9"
        self.assertEqual(validate_snapshot(jpeg), jpeg)
        with self.assertRaises(ValueError):
            validate_snapshot(b"not-an-image")
        path = ThumbnailStore("/tmp/ptz-thumb-test").path(("camera/../bad", "tcp", 1), 2)
        self.assertNotIn("..", path.name)
        store = ThumbnailStore("/tmp/ptz-thumb-test")
        self.assertNotEqual(store.path(("cam", "tcp", 1), 1), store.path(("cam", "tcp", 2), 1))

    def test_thumbnail_reservation_invalidates_older_capture(self):
        class Response:
            headers = {"Content-Type": "image/jpeg"}

            def __init__(self, payload):
                self.payload = payload

            def read(self, _):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class Opener:
            def __init__(self, payload):
                self.payload = payload
                self.request = None

            def open(self, request, timeout):
                self.request = request
                return Response(self.payload)

        old = b"\xff\xd8" + b"old-image-data" * 2 + b"\xff\xd9"
        new = b"\xff\xd8" + b"new-image-data" * 2 + b"\xff\xd9"
        with tempfile.TemporaryDirectory() as root:
            store = ThumbnailStore(root)
            target = store.path(("cam", "tcp", 1), 1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(old)
            opener = Opener(new)
            with patch("streamdeck_control.build_opener", return_value=opener):
                first = store.reserve(("cam", "tcp", 1), 1)
                second = store.reserve(("cam", "tcp", 1), 1)
                store.capture(("cam", "tcp", 1), 1, reservation=first)
                self.assertEqual(target.read_bytes(), old)
                store.capture(("cam", "tcp", 1), 1, reservation=second)
            self.assertEqual(target.read_bytes(), new)
            self.assertIn("ptzpad_ts=", opener.request.full_url)
            self.assertEqual(
                opener.request.headers["Cache-control"],
                "no-cache, no-store, max-age=0",
            )
            self.assertEqual(opener.request.headers["Pragma"], "no-cache")

    def test_telemetry_worker_wiring_and_render_surface(self):
        from unittest.mock import patch
        controller = StreamDeckController(queue.Queue())
        controller.set_telemetry_camera(("camera", "tcp", 5678))
        with patch("streamdeck_control.poll_visca_telemetry", return_value={"wb_mode": "Auto", "ae_mode": "SAE"}):
            controller._poll_telemetry_once()
        self.assertEqual(controller.snapshot()["telemetry"]["wb_mode"], "Auto")
        controller.close()

    def test_telemetry_mode_labels(self):
        from streamdeck_control import telemetry_mode
        self.assertEqual(telemetry_mode("wb_mode", "00"), "Auto")
        self.assertEqual(telemetry_mode("wb_mode", "05"), "Manual")
        self.assertEqual(telemetry_mode("wb_mode", "20"), "ColorTemp")
        self.assertEqual(telemetry_mode("ae_mode", "0a"), "SAE")
        self.assertEqual(telemetry_mode("ae_mode", "0d"), "Bright")
        self.assertEqual(telemetry_mode("ae_mode", "ff"), "ff")

    def test_camera_label_layout_preserves_full_ipv4(self):
        lines = camera_label_lines("192.168.10.44")
        self.assertEqual("".join(lines), "192.168.10.44")
        wrapped = camera_label_lines("A very long camera name")
        self.assertLessEqual(len(wrapped), 2)
        self.assertTrue(all(len(line) <= 10 for line in wrapped))

    def test_telemetry_switch_does_not_commit_stale_poll(self):
        controller = StreamDeckController(queue.Queue())
        controller.set_telemetry_camera(("a", "tcp", 1))
        def stale_poll(camera):
            controller.set_telemetry_camera(("b", "tcp", 2))
            return {"wb_mode": "Auto"}
        with patch("streamdeck_control.poll_visca_telemetry", side_effect=stale_poll):
            controller._poll_telemetry_once()
        self.assertEqual(controller.snapshot()["telemetry"], {})


if __name__ == "__main__":
    unittest.main()
