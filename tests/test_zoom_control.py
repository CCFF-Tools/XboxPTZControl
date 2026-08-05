import unittest

from zoom_control import ZoomCommandState, next_zoom_command


class ZoomCommandTests(unittest.TestCase):
    def test_tcp_repeats_held_start(self):
        state = ZoomCommandState()
        self.assertEqual(next_zoom_command(1, "tcp", 0, state), 1)
        self.assertIsNone(next_zoom_command(1, "tcp", 100, state))
        self.assertEqual(next_zoom_command(1, "tcp", 200, state), 1)

    def test_udp_start_only_on_direction_change(self):
        state = ZoomCommandState()
        self.assertEqual(next_zoom_command(1, "udp", 0, state), 1)
        self.assertIsNone(next_zoom_command(1, "udp", 200, state))
        self.assertEqual(next_zoom_command(-1, "udp", 250, state), -1)

    def test_udp_stop_retries_are_bounded(self):
        state = ZoomCommandState()
        next_zoom_command(1, "udp", 0, state)
        stops = [
            next_zoom_command(0, "udp", now, state, udp_stop_packets=3)
            for now in (50, 100, 150)
        ]
        self.assertEqual(stops, [0, 0, 0])
        self.assertIsNone(next_zoom_command(0, "udp", 200, state))

    def test_reset_clears_udp_retry_state(self):
        state = ZoomCommandState()
        next_zoom_command(1, "udp", 0, state)
        next_zoom_command(0, "udp", 50, state)
        state.reset()
        self.assertEqual(state.last_direction, 0)
        self.assertEqual(state.stop_retries_remaining, 0)
        self.assertIsNone(next_zoom_command(0, "udp", 100, state))

    def test_reset_allows_same_direction_restart(self):
        state = ZoomCommandState()
        self.assertEqual(next_zoom_command(1, "udp", 0, state), 1)
        self.assertIsNone(next_zoom_command(1, "udp", 100, state))
        state.reset()
        self.assertEqual(next_zoom_command(1, "udp", 200, state), 1)


if __name__ == "__main__":
    unittest.main()
