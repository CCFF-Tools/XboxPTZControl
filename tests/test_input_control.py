import unittest
from pathlib import Path

from input_control import (
    EVDEV_LAYOUT,
    HIDAPI_LAYOUT,
    ButtonEdges,
    MotionState,
    ZoomTriggerState,
    controller_layout,
    resolve_zoom_direction,
    zoom_speed_for_trigger,
)


class InputControlTests(unittest.TestCase):
    def test_midpoint_release_eventually_stops(self):
        state = ZoomTriggerState(direction=1)
        values = [resolve_zoom_direction(0.07, state) for _ in range(3)]
        self.assertEqual(values, [1, 1, 0])

    def test_active_trigger_continues(self):
        state = ZoomTriggerState(direction=1)
        self.assertEqual(resolve_zoom_direction(0.5, state), 1)

    def test_trigger_magnitude_ramps_zoom_speed(self):
        self.assertEqual(zoom_speed_for_trigger(0.0, 7), 0)
        self.assertEqual(zoom_speed_for_trigger(0.1, 7), 0)
        values = [zoom_speed_for_trigger(value, 7) for value in (0.2, 0.5, 0.8, 1.0)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(zoom_speed_for_trigger(0.2, 7), zoom_speed_for_trigger(-0.2, 7))
        self.assertEqual(zoom_speed_for_trigger(1.0, 3), 3)
        self.assertEqual(zoom_speed_for_trigger(1.0, 0), 0)

    def test_layouts_and_bumper_edges(self):
        self.assertEqual(controller_layout(12, 1), EVDEV_LAYOUT)
        self.assertEqual(controller_layout(15, 0), HIDAPI_LAYOUT)
        self.assertEqual((EVDEV_LAYOUT.lb, EVDEV_LAYOUT.rb, EVDEV_LAYOUT.ls), (4, 5, 9))
        self.assertEqual((HIDAPI_LAYOUT.lb, HIDAPI_LAYOUT.rb, HIDAPI_LAYOUT.ls), (9, 10, 7))
        edges = ButtonEdges()
        self.assertEqual(edges.rising({"RB": True}), {"RB"})
        self.assertEqual(edges.rising({"RB": True}), set())
        self.assertEqual(edges.rising({"RB": False}), set())
        self.assertEqual(edges.rising({"RB": True}), {"RB"})

    def test_motion_and_focus_suppress_idle_commands(self):
        state = MotionState()
        command = (0, 0, 3, 3)
        self.assertTrue(state.move_changed(command))
        self.assertFalse(state.move_changed(command))
        self.assertIsNone(state.next_focus(0))
        self.assertEqual(state.next_focus(1), 1)
        self.assertIsNone(state.next_focus(1))
        self.assertEqual(state.next_focus(0), 0)

    def test_udp_move_stop_retries_three_then_quiet(self):
        state = MotionState()
        active = (2, 2, 2, 2)
        self.assertTrue(state.move_changed(active, "udp"))
        neutral = [state.move_changed((0, 0, 3, 3), "udp") for _ in range(4)]
        self.assertEqual(neutral, [True, True, True, False])

    def test_udp_focus_stop_retries_three_then_quiet(self):
        state = MotionState()
        self.assertEqual(state.next_focus(1, "udp"), 1)
        stops = [state.next_focus(0, "udp") for _ in range(4)]
        self.assertEqual(stops, [0, 0, 0, None])

    def test_tcp_focus_has_one_stop_and_no_initial_stop(self):
        state = MotionState()
        self.assertIsNone(state.next_focus(0, "tcp"))
        self.assertEqual(state.next_focus(1, "tcp"), 1)
        self.assertEqual(state.next_focus(0, "tcp"), 0)
        self.assertIsNone(state.next_focus(0, "tcp"))

    def test_dashboard_distinguishes_live_and_saved_tuning(self):
        dashboard = Path(__file__).parents[1].joinpath("ptz_dashboard.py").read_text()
        self.assertIn("Live speed", dashboard)
        self.assertIn("Triggers LT", dashboard)
        self.assertIn("Triggers unavailable", dashboard)
        self.assertIn("0 = commanded stop", dashboard)
        self.assertIn("Saved maximum speed", dashboard)


if __name__ == "__main__":
    unittest.main()
