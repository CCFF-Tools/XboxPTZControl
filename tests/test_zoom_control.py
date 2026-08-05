import unittest

from zoom_control import ZoomCommandState, next_zoom_command


class ZoomCommandTests(unittest.TestCase):
    def test_tcp_held_start_is_not_repeated(self):
        state = ZoomCommandState()
        self.assertEqual(next_zoom_command(1, state), 1)
        self.assertIsNone(next_zoom_command(1, state))

    def test_udp_start_only_on_direction_change(self):
        state = ZoomCommandState()
        self.assertEqual(next_zoom_command(1, state), 1)
        self.assertIsNone(next_zoom_command(1, state))
        self.assertEqual(next_zoom_command(-1, state), -1)

    def test_udp_stop_retries_are_bounded(self):
        state = ZoomCommandState()
        next_zoom_command(1, state)
        stops = [
            next_zoom_command(0, state, stop_packets=3)
            for _ in (50, 100, 150)
        ]
        self.assertEqual(stops, [0, 0, 0])
        self.assertIsNone(next_zoom_command(0, state))

    def test_tcp_stop_packets_are_bounded(self):
        state = ZoomCommandState()
        next_zoom_command(1, state)
        stops = [next_zoom_command(0, state, stop_packets=3) for _ in range(4)]
        self.assertEqual(stops, [0, 0, 0, None])

    def test_reset_clears_udp_retry_state(self):
        state = ZoomCommandState()
        next_zoom_command(1, state)
        next_zoom_command(0, state)
        state.reset()
        self.assertEqual(state.last_direction, 0)
        self.assertEqual(state.stop_retries_remaining, 0)
        self.assertIsNone(next_zoom_command(0, state))

    def test_reset_allows_same_direction_restart(self):
        state = ZoomCommandState()
        self.assertEqual(next_zoom_command(1, state), 1)
        self.assertIsNone(next_zoom_command(1, state))
        state.reset()
        self.assertEqual(next_zoom_command(1, state), 1)


if __name__ == "__main__":
    unittest.main()
