"""Optional Elgato Stream Deck input and display support.

The bridge remains usable when the package, USB device, or Pillow is absent.
HID callbacks only enqueue :class:`DeckAction` values; callers own state changes.
"""
from dataclasses import dataclass
from enum import Enum
import logging
import queue
import threading
import time


class ActionKind(str, Enum):
    PREVIOUS_CAMERA = "previous_camera"
    NEXT_CAMERA = "next_camera"
    TOGGLE_SAVE = "toggle_save"
    PRESET = "preset"


@dataclass(frozen=True)
class DeckAction:
    kind: ActionKind
    preset: int | None = None


def preset_set_packet(preset: int) -> bytes:
    """Return the VISCA memory-set packet for a 1-based preset number."""
    if not 1 <= int(preset) <= 99:
        raise ValueError("preset must be between 1 and 99")
    return bytes((0x81, 0x01, 0x04, 0x3F, 0x01, int(preset), 0xFF))


def preset_recall_packet(preset: int) -> bytes:
    """Return the VISCA memory-recall packet for a 1-based preset number."""
    if not 1 <= int(preset) <= 99:
        raise ValueError("preset must be between 1 and 99")
    return bytes((0x81, 0x01, 0x04, 0x3F, 0x02, int(preset), 0xFF))


def resolve_deck_action(action: DeckAction, armed: bool) -> tuple[bool, bytes | None, str | None]:
    """Resolve save-mode state and preset packet without touching hardware."""
    if action.kind == ActionKind.TOGGLE_SAVE:
        return not armed, None, None
    if action.kind == ActionKind.PRESET and action.preset is not None:
        try:
            if armed:
                return False, preset_set_packet(action.preset), "preset-set"
            return armed, preset_recall_packet(action.preset), "preset-recall"
        except (TypeError, ValueError):
            return armed, None, None
    return armed, None, None


class StreamDeckController:
    """Best-effort first-device controller with retry and clean shutdown."""

    def __init__(self, actions: "queue.Queue[DeckAction]", retry_seconds: float = 3.0):
        self.actions = actions
        self.retry_seconds = retry_seconds
        self._stop = threading.Event()
        self._thread = None
        self._deck = None
        self._armed = False
        self._camera_index = 0
        self._camera_name = "Camera"
        self._camera_count = 1
        self._lock = threading.Lock()
        self._device_lock = threading.RLock()
        self._enabled = True
        self._brightness = 35
        self._library_available = None
        self._last_error = None
        self._last_render_at = None
        self._last_event_at = None
        self._device = ""
        self._key_count = 0

    def configure(self, enabled=True, brightness=35):
        with self._device_lock:
            with self._lock:
                self._enabled = bool(enabled)
                self._brightness = int(brightness)
                deck = self._deck
                if not enabled:
                    self._last_error = None
            if not enabled:
                self._close_deck_locked()
            elif deck is not None:
                try:
                    deck.set_brightness(int(brightness))
                except Exception as exc:
                    self._record_error(str(exc))

    def snapshot(self):
        with self._lock:
            return {"enabled": self._enabled, "library_available": self._library_available,
                    "connected": self._deck is not None, "device": self._device,
                    "key_count": self._key_count, "brightness": self._brightness,
                    "last_error": self._last_error, "last_render_at": self._last_render_at,
                    "last_event_at": self._last_event_at, "save_armed": self._armed,
                    "camera_index": self._camera_index, "camera_name": self._camera_name}

    def _record_error(self, message):
        with self._lock:
            self._last_error = str(message)[:240]

    @staticmethod
    def _device_name(deck):
        for attr in ("deck_type", "get_serial_number", "id"):
            try:
                value = getattr(deck, attr, None)
                value = value() if callable(value) else value
                if value is not None and not isinstance(value, (dict, list, tuple, set)):
                    return str(value)
            except Exception:
                continue
        return ""

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="streamdeck", daemon=True)
            self._thread.start()

    def update(self, camera_index: int, camera_name: str, camera_count: int, armed: bool) -> None:
        with self._lock:
            self._camera_index = camera_index
            self._camera_name = camera_name
            self._camera_count = max(1, camera_count)
            self._armed = armed
        with self._device_lock:
            self._render()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._device_lock:
            self._close_deck_locked()

    def _run(self) -> None:
        try:
            from StreamDeck.DeviceManager import DeviceManager
            self._library_available = True
        except ImportError:
            self._library_available = False
            self._record_error("streamdeck Python package unavailable")
            logging.info("Stream Deck support unavailable; install the streamdeck Python package to enable")
            return
        while not self._stop.is_set():
            if not self._enabled:
                self._stop.wait(self.retry_seconds)
                continue
            if self._deck is not None and not self._deck_connected(self._deck):
                self._close_deck()
            if self._deck is None:
                try:
                    decks = DeviceManager().enumerate()
                    if decks:
                        self._deck = decks[0]
                        self._deck.open()
                        self._device = self._device_name(self._deck)
                        self._key_count = int(self._deck.key_count())
                        if self._key_count < 4:
                            raise RuntimeError("unsupported Stream Deck with fewer than 4 keys")
                        self._deck.set_brightness(self._brightness)
                        self._deck.set_key_callback(self._key_callback)
                        self._render()
                        logging.info("Stream Deck connected (%s keys)", self._deck.key_count())
                    else:
                        self._record_error("No Stream Deck detected")
                except Exception as exc:  # optional hardware must never stop bridge
                    self._record_error(exc)
                    logging.info("Stream Deck unavailable: %s", exc)
                    self._close_deck()
            self._stop.wait(self.retry_seconds)

    @staticmethod
    def _deck_connected(deck) -> bool:
        """Check optional connection APIs without assuming a package version."""
        try:
            for name in ("is_open", "connected"):
                check = getattr(deck, name, None)
                if callable(check) and not check():
                    return False
                if check is not None and not callable(check) and not check:
                    return False
            return True
        except Exception:
            return False

    def _close_deck(self) -> None:
        with self._device_lock:
            self._close_deck_locked()

    def _close_deck_locked(self) -> None:
        deck, self._deck = self._deck, None
        self._key_count = 0
        if deck is not None:
            try:
                deck.reset()
            except Exception:
                pass
            try:
                deck.close()
            except Exception:
                pass

    def _key_callback(self, deck, key: int, state: bool) -> None:
        if not state:
            return
        self._last_event_at = time.time()
        action = map_key_action(key, int(deck.key_count()))
        if action is not None:
            self.actions.put(action)

    def _render(self) -> None:
        with self._device_lock:
            self._render_locked()

    def _render_locked(self) -> None:
        deck = self._deck
        if deck is None:
            return
        try:
            from PIL import Image, ImageDraw, ImageFont
            from StreamDeck.ImageHelpers import PILHelper
            size = deck.key_image_format()
            width, height = int(size["width"]), int(size["height"])
            font = ImageFont.load_default()
            with self._lock:
                idx, name, total, armed = self._camera_index, self._camera_name, self._camera_count, self._armed
            labels = [f"< Cam", "Cam >", "SAVE" + ("*" if armed else "")]
            labels.extend(str(i) for i in range(1, max(1, int(deck.key_count()) - 2) + 1))
            for key in range(int(deck.key_count())):
                image = Image.new("RGB", (width, height), (120, 40, 20) if key == 2 and armed else (20, 20, 20))
                draw = ImageDraw.Draw(image)
                label = labels[key] if key < len(labels) else ""
                if key == 0:
                    label = f"< {idx + 1}/{total}"
                elif key == 1:
                    label = f"{idx + 1}/{total} >"
                draw.text((4, height // 3), label, fill="white", font=font)
                if key == 2:
                    draw.text((4, height // 2 + 8), name[:12], fill="white", font=font)
                create = getattr(PILHelper, "create_key_image", None)
                native = getattr(PILHelper, "to_native_key_format", None)
                if native is None:
                    native = PILHelper.to_native_format
                if create is not None:
                    native_image = create(deck)
                    native_image.paste(image)
                else:
                    native_image = PILHelper.create_image(deck)
                    native_image.paste(image)
                deck.set_key_image(key, native(deck, native_image))
            self._last_render_at = time.time()
            with self._lock:
                self._last_error = None
        except Exception as exc:
            self._record_error("render: " + str(exc))
            logging.info("Stream Deck render failed: %s", exc)
            return


def map_key_action(key: int, key_count: int) -> DeckAction | None:
    """Pure key mapping helper used by tests and callback implementations."""
    if key == 0:
        return DeckAction(ActionKind.PREVIOUS_CAMERA)
    if key == 1:
        return DeckAction(ActionKind.NEXT_CAMERA)
    if key == 2:
        return DeckAction(ActionKind.TOGGLE_SAVE)
    if 3 <= key < key_count:
        return DeckAction(ActionKind.PRESET, key - 2)
    return None
