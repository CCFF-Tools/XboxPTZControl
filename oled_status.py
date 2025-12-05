"""Helpers for writing status updates to an SSD1306 OLED display.

The module uses luma.oled if available and falls back to a no-op
implementation when the hardware or driver cannot be initialized.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable, List

try:
    from luma.core.interface.serial import i2c
    from luma.core.render import canvas
    from luma.oled.device import ssd1306
    from PIL import ImageFont
except (ImportError, FileNotFoundError, OSError):  # hardware not present or drivers missing
    canvas = None
    i2c = None
    ssd1306 = None
    ImageFont = None


class _NullDisplay:
    """Graceful fallback used when an OLED cannot be initialized."""

    def __init__(self) -> None:
        self.available = False

    def show(self, _lines: Iterable[str], force: bool = False) -> None:
        return


class OledStatus:
    """Lightweight status renderer for a 128x64 SSD1306 display."""

    def __init__(self, min_interval: float = 0.2) -> None:
        self._log = logging.getLogger(__name__)
        self._min_interval = min_interval
        self._keepalive_interval = 30.0
        self._last_lines: List[str] = []
        self._last_update = 0.0
        self._failed_once = False
        self._available = False

        if not all([canvas, i2c, ssd1306, ImageFont]):
            self._display = _NullDisplay()
            if not self._failed_once:
                self._log.info("OLED display unavailable; running without screen")
                self._failed_once = True
            return

        try:
            i2c_bus = int(os.environ.get("OLED_I2C_BUS", "3"))
            address_raw = os.environ.get("OLED_I2C_ADDRESS", "0x3C")
            i2c_addr = int(address_raw, 0)

            self._log.info("Initializing SSD1306 on I2C bus %s addr 0x%X", i2c_bus, i2c_addr)

            serial = i2c(port=i2c_bus, address=i2c_addr)
            self._serial = serial
            self._device = ssd1306(serial, width=128, height=64)  # type: ignore[arg-type]
            self._width = self._device.width
            self._height = self._device.height
            self._font = ImageFont.load_default()

            self._device.contrast(255)
            self._device.clear()
            self._device.show()
        except Exception as exc:  # pylint: disable=broad-except
            self._log.warning("Failed to initialize OLED display: %s", exc)
            self._display = _NullDisplay()
            self._failed_once = True
            return

        self._available = True
        self._display = self._device
        self._paint_boot_screen()

    @property
    def available(self) -> bool:
        return self._available

    def boot(self, message: str) -> None:
        self._render(["PTZ Bridge", message])

    def _paint_boot_screen(self) -> None:
        self._render(["PTZ Bridge", "Starting up..."], force=True)

    def joystick_wait(self) -> None:
        self._render(["Waiting for joystick", "Connect controller..."])

    def joystick_connected(self, name: str) -> None:
        self._render(["Joystick connected", name])

    def joystick_disconnected(self) -> None:
        self._render(["Joystick disconnected", "Waiting to reconnect"])

    def camera_active(self, idx: int, ip: str) -> None:
        self._render([f"Camera {idx + 1}", ip])

    def bluetooth_connected(self, name: str) -> None:
        self._render(["Bluetooth linked", name])

    def bluetooth_disconnected(self) -> None:
        self._render(["Bluetooth link lost", "Reconnect controller"])

    def error(self, message: str) -> None:
        self._render(["Error", message], force=True)

    def refresh(self) -> None:
        """Force a keepalive render when the screen already has content."""

        if not self.available or not self._last_lines:
            return

        self._render(self._last_lines)

    def _render(self, lines: Iterable[str], force: bool = False) -> None:
        if not self.available:
            return

        normalized = [line[:21] for line in lines]  # 21 chars fits default font
        now = time.time()
        if not force:
            if normalized == self._last_lines:
                if now - self._last_update < self._keepalive_interval:
                    return
            elif now - self._last_update < self._min_interval:
                return

        try:
            self.show(normalized, force=force)
            self._last_lines = normalized
            self._last_update = now
        except Exception as exc:  # pylint: disable=broad-except
            if not self._failed_once:
                self._log.warning("OLED write failed: %s", exc)
                self._failed_once = True
            self._display = _NullDisplay()
            self._available = False

    def show(self, lines: Iterable[str], force: bool = False) -> None:
        if not self.available:
            return

        padding = 2
        with canvas(self._device) as draw:  # type: ignore[arg-type]
            draw.rectangle(self._device.bounding_box, outline=0, fill=0)

            y = padding
            for line in lines:
                draw.text((0, y), line, font=self._font, fill=255)
                y += self._font.getsize(line)[1] + 2

        # Explicitly flush the buffer to the panel to mirror the working
        # standalone test sequence and avoid stale images on some adapters.
        self._device.show()
