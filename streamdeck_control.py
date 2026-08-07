"""Optional Elgato Stream Deck input and display support.

The bridge remains usable when the package, USB device, or Pillow is absent.
HID callbacks only enqueue :class:`DeckAction` values; callers own state changes.
"""
import hashlib
import logging
import os
import queue
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener


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


def validate_snapshot(data: bytes, content_type: str = "") -> bytes:
    """Accept only bounded JPEG/PNG snapshot payloads."""
    if len(data) > 2 * 1024 * 1024 or len(data) < 16:
        raise ValueError("snapshot size rejected")
    if data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
        return data
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82"):
        return data
    raise ValueError("snapshot type rejected")


class ThumbnailStore:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("PTZPAD_CACHE", "~/.cache/ptzpad/thumbnails")).expanduser()

    def path(self, camera, preset):
        identity = tuple(camera[:3]) if isinstance(camera, tuple) else (str(camera), "tcp", 80)
        key = hashlib.sha256(repr(identity).encode()).hexdigest()[:20]
        return self.root / f"{key}-{int(preset)}.jpg"

    def capture(self, camera, preset, timeout=1.5):
        host = camera[0] if isinstance(camera, tuple) else str(camera)
        request = Request(f"http://{host}/snapshot.jpg", headers={"Accept": "image/jpeg,image/png"})
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("snapshot redirect rejected")
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            data = validate_snapshot(response.read(2 * 1024 * 1024 + 1), response.headers.get("Content-Type", ""))
        target = self.path(camera, preset)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".snapshot-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, target)
        finally:
            try: os.unlink(temp)
            except FileNotFoundError: pass
        return target


def parse_visca_telemetry(response: bytes) -> dict:
    """Parse common VISCA inquiry payloads when cameras support them."""
    if not response or response[0] & 0xF0 != 0x90:
        return {}
    values = {}
    if len(response) >= 5 and response[1] == 0x50:
        raw = response[2:-1]
        values["value"] = raw.hex()
    return values


def telemetry_mode(label: str, raw: str) -> str:
    maps = {
        "wb_mode": {"00": "Auto", "01": "Indoor", "02": "Outdoor", "03": "OnePush", "05": "Manual", "20": "ColorTemp"},
        "ae_mode": {"00": "Auto", "03": "Manual", "0a": "SAE", "0b": "AAE", "0d": "Bright"},
    }
    return maps.get(label, {}).get(raw.lower(), raw)


WB_INQUIRY = b"\x81\x09\x04\x35\xff"
AE_INQUIRY = b"\x81\x09\x04\x39\xff"


def inquiry_packet(name: str) -> bytes:
    if name == "wb_mode":
        return WB_INQUIRY
    if name == "ae_mode":
        return AE_INQUIRY
    raise ValueError("unsupported inquiry")


def poll_visca_telemetry(camera, timeout=0.25) -> dict:
    """Best-effort TCP inquiries; UDP cameras are intentionally skipped."""
    host, proto, port = camera
    if proto.lower() != "tcp":
        return {}
    result = {}
    try:
        for name in ("wb_mode", "ae_mode"):
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(inquiry_packet(name))
                response = sock.recv(64)
            parsed = parse_visca_telemetry(response)
            if parsed:
                result[name] = telemetry_mode(name, parsed.get("value", ""))
    except OSError:
        return result
    return result


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
        self._camera_host = None
        self._thumbnails = ThumbnailStore()
        self._telemetry = {}
        self._telemetry_camera = None
        self._telemetry_thread = None
        self.telemetry_interval = 5.0

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
                    "camera_index": self._camera_index, "camera_name": self._camera_name,
                    "telemetry": dict(self._telemetry)}

    def capture_thumbnail(self, camera, preset):
        """Capture asynchronously; network failures never affect controls."""
        with self._lock:
            self._camera_host = camera[0] if isinstance(camera, tuple) else str(camera)
        threading.Thread(target=self._capture_thumbnail, args=(camera, preset), daemon=True).start()

    def _capture_thumbnail(self, camera, preset):
        try:
            self._thumbnails.capture(camera, preset)
            self._render()
        except Exception as exc:
            self._record_error("thumbnail: " + str(exc))

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

    def set_telemetry_camera(self, camera):
        with self._lock:
            if camera == self._telemetry_camera:
                return
            self._telemetry_camera = camera
            self._telemetry = {}
        if self._telemetry_thread is None:
            self._telemetry_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
            self._telemetry_thread.start()

    def _poll_telemetry_once(self):
        with self._lock:
            camera = self._telemetry_camera
        if not camera:
            return
        values = poll_visca_telemetry(camera)
        with self._lock:
            if camera != self._telemetry_camera:
                return
            changed = values != self._telemetry
            self._telemetry = values
        if changed:
            self._render()

    def _telemetry_loop(self):
        while not self._stop.wait(self.telemetry_interval):
            with self._lock:
                camera = self._telemetry_camera
            if not camera:
                continue
            self._poll_telemetry_once()

    def update(self, camera_index: int, camera_name: str, camera_count: int, armed: bool, camera_host=None) -> None:
        with self._lock:
            self._camera_index = camera_index
            self._camera_name = camera_name
            self._camera_count = max(1, camera_count)
            self._armed = armed
            self._camera_host = camera_host
        if isinstance(camera_host, tuple):
            self.set_telemetry_camera(camera_host)
        with self._device_lock:
            self._render()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._telemetry_thread:
            self._telemetry_thread.join(timeout=2)
        with self._device_lock:
            self._close_deck_locked()

    def _run(self) -> None:
        try:
            from StreamDeck.DeviceManager import DeviceManager
            with self._lock:
                self._library_available = True
        except ImportError:
            with self._lock:
                self._library_available = False
            self._record_error("streamdeck Python package unavailable")
            logging.info("Stream Deck support unavailable; install the streamdeck Python package to enable")
            return
        while not self._stop.is_set():
            with self._lock:
                enabled = self._enabled
                deck = self._deck
            if not enabled:
                self._stop.wait(self.retry_seconds)
                continue
            if deck is not None:
                with self._device_lock:
                    with self._lock:
                        deck = self._deck
                    if deck is not None and not self._deck_connected(deck):
                        self._close_deck_locked()
            with self._lock:
                deck = self._deck
            if deck is None:
                candidate = None
                try:
                    decks = DeviceManager().enumerate()
                    if decks:
                        candidate = decks[0]
                        with self._device_lock:
                            with self._lock:
                                should_open = (
                                    self._enabled
                                    and not self._stop.is_set()
                                    and self._deck is None
                                )
                                brightness = self._brightness
                            if not should_open:
                                continue
                            candidate.open()
                            device_name = self._device_name(candidate)
                            key_count = int(candidate.key_count())
                            if key_count < 4:
                                raise RuntimeError("unsupported Stream Deck with fewer than 4 keys")
                            candidate.set_brightness(brightness)
                            candidate.set_key_callback(self._key_callback)
                            with self._lock:
                                self._deck = candidate
                                self._device = device_name
                                self._key_count = key_count
                            self._render_locked()
                        logging.info("Stream Deck connected (%s keys)", key_count)
                    else:
                        with self._lock:
                            still_enabled = self._enabled
                        if still_enabled:
                            self._record_error("No Stream Deck detected")
                except Exception as exc:  # optional hardware must never stop bridge
                    self._record_error(exc)
                    logging.info("Stream Deck unavailable: %s", exc)
                    with self._device_lock:
                        with self._lock:
                            published = candidate is not None and self._deck is candidate
                        if published:
                            self._close_deck_locked()
                        elif candidate is not None:
                            self._close_device(candidate)
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
        with self._lock:
            deck, self._deck = self._deck, None
            self._key_count = 0
        self._close_device(deck)

    @staticmethod
    def _close_device(deck) -> None:
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
        with self._lock:
            self._last_event_at = time.time()
        try:
            action = map_key_action(key, int(deck.key_count()))
        except Exception as exc:
            self._record_error("key event: " + str(exc))
            return
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
            from PIL import ImageDraw, ImageFont
            from StreamDeck.ImageHelpers import PILHelper
            font = ImageFont.load_default()
            with self._lock:
                idx, name, total, armed = self._camera_index, self._camera_name, self._camera_count, self._armed
            labels = [f"< Cam", "Cam >", "SAVE" + ("*" if armed else "")]
            labels.extend(str(i) for i in range(1, max(1, int(deck.key_count()) - 2) + 1))
            for key in range(int(deck.key_count())):
                create = getattr(PILHelper, "create_key_image", None)
                native = getattr(PILHelper, "to_native_key_format", None)
                if native is None:
                    native = PILHelper.to_native_format
                native_image = create(deck) if create is not None else PILHelper.create_image(deck)
                width, height = native_image.size
                draw = ImageDraw.Draw(native_image)
                draw.rectangle((0, 0, width, height), fill=(120, 40, 20) if key == 2 and armed else (20, 20, 20))
                thumbnail = None
                if key >= 3 and self._camera_host:
                    thumbnail = self._thumbnails.path(self._camera_host, key - 2)
                if thumbnail and thumbnail.exists():
                    try:
                        from PIL import Image
                        thumb = Image.open(thumbnail).convert(native_image.mode)
                        thumb.thumbnail((width, height))
                        native_image.paste(thumb, ((width - thumb.width) // 2, (height - thumb.height) // 2))
                        draw = ImageDraw.Draw(native_image)
                    except Exception as exc:
                        self._record_error("thumbnail: " + str(exc))
                label = labels[key] if key < len(labels) else ""
                if key == 0:
                    label = f"< {idx + 1}/{total}"
                elif key == 1:
                    label = f"{idx + 1}/{total} >"
                draw.text((4, height // 3), label, fill="white", font=font)
                if key == 2:
                    draw.text((4, height // 2 + 8), name[:12], fill="white", font=font)
                    with self._lock:
                        telemetry = dict(self._telemetry)
                    draw.text((4, height - 12), f"WB {telemetry.get('wb_mode','-')} AE {telemetry.get('ae_mode','-')}", fill="white", font=font)
                deck.set_key_image(key, native(deck, native_image))
            with self._lock:
                self._last_render_at = time.time()
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
