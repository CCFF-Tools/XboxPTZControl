import unittest
from unittest.mock import patch

from oled_status import OledStatus


class _Font:
    def getbbox(self, _text):
        return (0, -2, 8, 9)


class _LegacyFont:
    def getsize(self, _text):
        return (8, 7)


class _Draw:
    def __init__(self):
        self.positions = []

    def rectangle(self, *_args, **_kwargs):
        pass

    def text(self, position, _line, **_kwargs):
        self.positions.append(position)


class _Canvas:
    def __init__(self, draw):
        self.draw = draw

    def __enter__(self):
        return self.draw

    def __exit__(self, *_args):
        return False


class OledRenderingTests(unittest.TestCase):
    def test_render_uses_supplied_lines(self):
        display = OledStatus.__new__(OledStatus)
        display._available = True
        display._last_lines = []
        display._last_update = 0.0
        display._min_interval = 0
        display._keepalive_interval = 30
        display._failed_once = False
        display._log = __import__("logging").getLogger(__name__)
        with patch.object(display, "show") as show:
            display._render(["Joystick connected", "Pad"])
        show.assert_called_once_with(["Joystick connected", "Pad"], force=False)

    def _show_positions(self, font):
        display = OledStatus.__new__(OledStatus)
        display._available = True
        display._font = font
        display._device = type(
            "Device",
            (),
            {"bounding_box": (0, 0, 127, 63), "show": lambda _self: None},
        )()
        draw = _Draw()
        with patch("oled_status.canvas", return_value=_Canvas(draw)):
            display.show(["one", "two"])
        return draw.positions

    def test_show_uses_font_bbox_for_line_height(self):
        self.assertEqual(self._show_positions(_Font()), [(0, 2), (0, 15)])

    def test_show_falls_back_to_legacy_getsize(self):
        self.assertEqual(self._show_positions(_LegacyFont()), [(0, 2), (0, 11)])


if __name__ == "__main__":
    unittest.main()
