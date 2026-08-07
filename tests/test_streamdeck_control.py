import queue
import json
import unittest
from pathlib import Path

from streamdeck_control import (
    ActionKind,
    DeckAction,
    StreamDeckController,
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


if __name__ == "__main__":
    unittest.main()
