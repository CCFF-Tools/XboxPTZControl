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
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ActionKind(str, Enum):
    PREVIOUS_CAMERA = "previous_camera"
    NEXT_CAMERA = "next_camera"
    TOGGLE_SAVE = "toggle_save"
    PRESET = "preset"


def key_layout(key_count: int) -> dict[int, tuple[str, int | None]]:
    """Return semantic key mapping; Original V2 reserves its left column."""
    if key_count == 15:
        preset_keys = [1, 2, 3, 6, 7, 8, 9, 11, 12, 13, 14]
        return {
            0: ("status_next", None),
            5: ("status", None),
            10: ("status", None),
            4: ("save", None),
            **{
                key: ("preset", index + 1)
                for index, key in enumerate(preset_keys)
            },
        }
    return {
        0: ("previous", None),
        1: ("next", None),
        2: ("save", None),
        **{key: ("preset", key - 2) for key in range(3, key_count)},
    }


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
    def __init__(self, root=None, sleeper=time.sleep):
        self.root = Path(root or os.environ.get("PTZPAD_CACHE", "~/.cache/ptzpad/thumbnails")).expanduser()
        self.sleeper = sleeper
        self._lock = threading.Lock()
        self._generations = {}

    def path(self, camera, preset):
        identity = tuple(camera[:3]) if isinstance(camera, tuple) else (str(camera), "tcp", 80)
        key = hashlib.sha256(repr(identity).encode()).hexdigest()[:20]
        return self.root / f"{key}-{int(preset)}.jpg"

    def reserve(self, camera, preset):
        target = self.path(camera, preset)
        with self._lock:
            token = self._generations.get(target, 0) + 1
            self._generations[target] = token
        return target, token

    def capture(self, camera, preset, timeout=1.5, reservation=None):
        host = camera[0] if isinstance(camera, tuple) else str(camera)
        target, generation = reservation or self.reserve(camera, preset)
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("snapshot redirect rejected")

        def fetch():
            query = urlencode({"ptzpad_ts": time.time_ns()})
            request = Request(
                f"http://{host}/snapshot.jpg?{query}",
                headers={
                    "Accept": "image/jpeg,image/png",
                    "Cache-Control": "no-cache, no-store, max-age=0",
                    "Pragma": "no-cache",
                },
            )
            with build_opener(NoRedirect).open(request, timeout=timeout) as response:
                return validate_snapshot(
                    response.read(2 * 1024 * 1024 + 1),
                    response.headers.get("Content-Type", ""),
                )

        fetch()
        self.sleeper(0.25)
        data = fetch()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".snapshot-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            with self._lock:
                if self._generations.get(target) != generation:
                    return target
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


def camera_label_lines(name: str, max_chars: int = 10) -> list[str]:
    """Fit camera names/IPs into at most two key-display lines."""
    text = str(name or "Camera")
    if len(text) <= max_chars:
        return [text]
    return [text[:max_chars], text[max_chars : max_chars * 2]]


def status_key_lines(
    key,
    index,
    total,
    name,
    camera,
    max_speed,
    zoom_speed,
    telemetry,
):
    """Return compact lines for the three Standard-deck status keys."""
    if key == 0:
        return [f"Cam {index + 1}/{total}", *camera_label_lines(name)]
    if key == 5:
        return [f"PT {max_speed}", f"Zoom {zoom_speed}"]
    if key == 10:
        host = camera[0] if isinstance(camera, tuple) else str(camera or "")
        return [
            *camera_label_lines(host),
            f"WB {telemetry.get('wb_mode', '-')}",
            f"AE {telemetry.get('ae_mode', '-')}",
        ]
    return []


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
        self._max_speed = 24
        self._zoom_speed = 7
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
        reservation = self._thumbnails.reserve(camera, preset)
        threading.Thread(target=self._capture_thumbnail, args=(camera, preset, reservation), daemon=True).start()

    def _capture_thumbnail(self, camera, preset, reservation):
        try:
            time.sleep(0.4)
            self._thumbnails.capture(camera, preset, reservation=reservation)
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

    def update(self, camera_index: int, camera_name: str, camera_count: int, armed: bool, camera_host=None, max_speed=24, zoom_speed=7) -> None:
        with self._lock:
            self._camera_index = camera_index
            self._camera_name = camera_name
            self._camera_count = max(1, camera_count)
            self._armed = armed
            self._camera_host = camera_host
            self._max_speed, self._zoom_speed = max_speed, zoom_speed
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
                idx = self._camera_index
                name = self._camera_name
                total = self._camera_count
                armed = self._armed
                camera = self._camera_host
                max_speed = self._max_speed
                zoom_speed = self._zoom_speed
                telemetry = dict(self._telemetry)
            key_count = int(deck.key_count())
            layout = key_layout(key_count)
            for key in range(key_count):
                kind, preset_slot = layout.get(key, ("none", None))
                create = getattr(PILHelper, "create_key_image", None)
                native = getattr(PILHelper, "to_native_key_format", None)
                if native is None:
                    native = PILHelper.to_native_format
                native_image = create(deck) if create is not None else PILHelper.create_image(deck)
                width, height = native_image.size
                draw = ImageDraw.Draw(native_image)
                background = (
                    (120, 40, 20)
                    if kind == "save" and armed
                    else (20, 20, 20)
                )
                draw.rectangle((0, 0, width, height), fill=background)
                thumbnail = None
                if kind == "preset" and camera:
                    thumbnail = self._thumbnails.path(camera, preset_slot)
                if thumbnail and thumbnail.exists():
                    try:
                        from PIL import Image
                        thumb = Image.open(thumbnail).convert(native_image.mode)
                        thumb.thumbnail((width, height))
                        native_image.paste(
                            thumb,
                            ((width - thumb.width) // 2, (height - thumb.height) // 2),
                        )
                        draw = ImageDraw.Draw(native_image)
                    except Exception as exc:
                        self._record_error("thumbnail: " + str(exc))
                if kind in ("status", "status_next"):
                    lines = status_key_lines(
                        key,
                        idx,
                        total,
                        name,
                        camera,
                        max_speed,
                        zoom_speed,
                        telemetry,
                    )
                    for line_no, line in enumerate(lines):
                        draw.text((4, 4 + line_no * 9), line, fill="white", font=font)
                elif kind == "save" and key_count != 15:
                    label = "SAVE" + ("*" if armed else "")
                    draw.text((4, 4), label, fill="white", font=font)
                    lines = camera_label_lines(name)
                    for line_no, line in enumerate(lines):
                        draw.text(
                            (4, 20 + line_no * 10),
                            line,
                            fill="white",
                            font=font,
                        )
                    status = (
                        f"WB {telemetry.get('wb_mode', '-')} "
                        f"AE {telemetry.get('ae_mode', '-')}"
                    )
                    draw.text(
                        (4, height - 12),
                        status,
                        fill="white",
                        font=font,
                    )
                else:
                    labels = {
                        "previous": f"< {idx + 1}/{total}",
                        "next": f"{idx + 1}/{total} >",
                        "save": "SAVE" + ("*" if armed else ""),
                        "preset": str(preset_slot),
                    }
                    label = labels.get(kind, "")
                    draw.text((4, height // 3), label, fill="white", font=font)
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
    kind, preset = key_layout(key_count).get(key, ("none", None))
    if kind == "status_next":
        return DeckAction(ActionKind.NEXT_CAMERA)
    if kind == "previous":
        return DeckAction(ActionKind.PREVIOUS_CAMERA)
    if kind == "next":
        return DeckAction(ActionKind.NEXT_CAMERA)
    if kind == "save":
        return DeckAction(ActionKind.TOGGLE_SAVE)
    if kind == "preset":
        return DeckAction(ActionKind.PRESET, preset)
    return None
