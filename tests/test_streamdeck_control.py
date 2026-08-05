import queue
import unittest

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


if __name__ == "__main__":
    unittest.main()
