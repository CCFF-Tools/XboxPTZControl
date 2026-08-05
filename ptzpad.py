#!/usr/bin/env python3
# Xbox-One → PTZOptics VISCA-over-IP bridge
import os
import sys
import tempfile

# Force SDL to use the headless video driver to avoid XDG runtime complaints on
# systems without a graphical session (e.g., the service unit).
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")


def ensure_runtime_dir() -> str:
    """Guarantee SDL has a writable runtime directory before importing pygame."""

    configured = os.environ.get("XDG_RUNTIME_DIR") or "/run/ptzpad"
    try:
        candidates = [configured]
        for candidate in candidates:
            try:
                os.makedirs(candidate, mode=0o700, exist_ok=True)
                os.chmod(candidate, 0o700)
                if os.access(candidate, os.W_OK):
                    os.environ["XDG_RUNTIME_DIR"] = candidate
                    return candidate
            except OSError as exc:
                print(
                    f"warning: could not prepare XDG_RUNTIME_DIR {candidate}: {exc}",
                    file=sys.stderr,
                )
        fallback = tempfile.mkdtemp(prefix="ptzpad-")
        os.environ["XDG_RUNTIME_DIR"] = fallback
        print(
            f"warning: using private temporary XDG_RUNTIME_DIR {fallback}",
            file=sys.stderr,
        )
        return fallback
    except OSError as exc:
        print(
            f"warning: could not create temporary XDG_RUNTIME_DIR: {exc}",
            file=sys.stderr,
        )
    # Keep startup alive even on unusual read-only systems; SDL may still work.
    os.environ["XDG_RUNTIME_DIR"] = configured
    return configured


ensure_runtime_dir()

import logging
import pygame
import signal
import socket
import time
import json
import queue
from pathlib import Path
from ptz_config import load_config
from zoom_control import ZoomCommandState, next_zoom_command
from input_control import (
    ButtonEdges,
    MotionState,
    ZoomTriggerState,
    controller_layout,
    resolve_zoom_direction,
)

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from oled_status import OledStatus
from streamdeck_control import (
    ActionKind,
    DeckAction,
    StreamDeckController,
    resolve_deck_action,
)

# ---- CONFIG ---------------------------------------------------------------
def parse_cams(status: OledStatus | None = None) -> list[tuple[str, str, int]]:
    """Return list of (ip, proto, port) triples from PTZ_CAMS env.

    Invalid entries are skipped and reported via the OLED when available.
    """
    cams = []
    raw = os.environ.get("PTZ_CAMS", "192.168.1.150").split(",")
    for entry in raw:
        entry = entry.strip()
        if not entry:
            continue
        proto = "tcp"
        port = None
        parts = entry.split(":")
        if parts[0].lower() in ("tcp", "udp"):
            proto = parts[0].lower()
            parts = parts[1:]
        if not parts:
            msg = f"Invalid host in PTZ_CAMS entry: {entry}"
            print(msg)
            if status:
                status.error("Bad PTZ_CAMS host")
            continue
        ip = parts[0].strip()
        if not ip:
            msg = f"Invalid host in PTZ_CAMS entry: {entry}"
            print(msg)
            if status:
                status.error("Bad PTZ_CAMS host")
            continue
        if len(parts) > 2:
            msg = f"Invalid PTZ_CAMS entry: {entry}"
            print(msg)
            if status:
                status.error("Bad PTZ_CAMS format")
            continue
        if len(parts) > 1 and not parts[1].strip():
            msg = f"Invalid port in PTZ_CAMS entry: {entry}"
            print(msg)
            if status:
                status.error("Bad PTZ_CAMS port")
            continue
        if len(parts) > 1 and parts[1]:
            try:
                port = int(parts[1])
            except ValueError:
                msg = f"Invalid port in PTZ_CAMS entry: {entry}"
                print(msg)
                if status:
                    status.error("Bad PTZ_CAMS port")
                continue
            if not 1 <= port <= 65535:
                msg = f"Invalid port in PTZ_CAMS entry: {entry}"
                print(msg)
                if status:
                    status.error("Bad PTZ_CAMS port")
                continue
        if port is None:
            port = 5678 if proto == "tcp" else 1259
        cams.append((ip, proto, port))
    if not cams:
        fallback = ("192.168.1.150", "tcp", 5678)
        cams.append(fallback)
        print(">>> PTZ_CAMS invalid; using default", fallback[0])
        if status:
            status.error("PTZ_CAMS invalid")
    return cams
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
status_display = OledStatus()
status_display.boot("Parsing cameras...")

_cfg = load_config()
CAMS = [(c["host"], c["protocol"], c["port"]) for c in _cfg["cameras"]]
CAMERA_NAMES = [c.get("name") or c["host"] for c in _cfg["cameras"]]
MAX_SPEED = 0x18                 # 0x01 (slow) ... 0x18 (fast)
DEADZONE = 0.15                 # stick slack
FOCUS_DEADZONE = 0.20           # left stick focus deadzone
MAX_ZOOM_SPEED = 0x07           # 0x00 (slow) ... 0x07 (fast)
ZOOM_START_DEADZONE = 0.10      # trigger slack for zoom start
ZOOM_STOP_PACKETS = 3            # total normal-trigger stop packets
UDP_STOP_PACKETS = 3              # total lifecycle stop packets for UDP
ZOOM_STOP_LOOPS = 3             # require this many loops below stop threshold
LOOP_MS = 50                    # command period (ms)
DEBUG_INPUT_RAW = os.environ.get("PTZPAD_DEBUG_INPUT", "")
DEBUG_INPUT = DEBUG_INPUT_RAW.lower() in ("1", "true", "yes")
DEBUG_INPUT_INTERVAL = 0.25     # seconds between debug samples
# ---------------------------------------------------------------------------

running = True
cur = 0
max_speed = MAX_SPEED
deadzone = DEADZONE
zoom_speed = MAX_ZOOM_SPEED
js = None
controller_connected = False
_started = time.time()
_state_path = Path(os.environ.get("PTZPAD_STATE", "/run/ptzpad/status.json"))
_last_state_write = 0.0
_camera_send = {}
_input_telemetry = {"lt": None, "rt": None, "zoom_value": None, "zoom_direction": 0, "protocol": None}
_deck_actions = queue.Queue()
_streamdeck = None
_preset_save_armed = False


def publish_state(force=False):
    global _last_state_write
    now = time.time()
    if not force and now - _last_state_write < 1:
        return
    payload = {"service": "running", "started": _started, "heartbeat": now,
               "active_camera": cur, "controller": {"name": js.get_name() if js else "",
               "connected": controller_connected, "wireless": bluetooth_linked},
               "max_speed": max_speed, "deadzone": deadzone, "zoom_speed": zoom_speed,
               "camera_send": _camera_send, "input": _input_telemetry}
    try:
        _state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = _state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _state_path)
        _last_state_write = now
    except OSError:
        pass
bluetooth_linked = False


def handle_signal(signum, frame):
    """Flip running flag to exit main loop."""
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

status_display.boot("Starting pygame...")
pygame.init()
status_display.boot("Waiting for joystick")
print(
    f">>> INPUT debug {'enabled' if DEBUG_INPUT else 'disabled'} "
    f"(PTZPAD_DEBUG_INPUT={'<unset>' if not DEBUG_INPUT_RAW else DEBUG_INPUT_RAW})"
)


def wait_for_joystick() -> pygame.joystick.Joystick:
    """Block until a joystick is available, returning it."""
    global bluetooth_linked, controller_connected, js
    status_display.joystick_wait()

    def reinit_joystick() -> None:
        pygame.joystick.quit()
        pygame.joystick.init()

    hidapi_env = os.environ.get("SDL_JOYSTICK_HIDAPI", "0")
    hidapi_enabled = hidapi_env not in ("0", "false", "no")
    hidapi_toggled = False
    attempts = 0

    while pygame.joystick.get_count() == 0 and running:
        publish_state()
        attempts += 1
        print(">>> Waiting for joystick connection...")
        status_display.joystick_wait()

        devs = sorted(p for p in os.listdir("/dev/input") if p.startswith("js")) if os.path.isdir("/dev/input") else []
        if devs:
            print(f">>> /dev/input devices present: {', '.join(devs)}")

            for dev in devs:
                path = os.path.join("/dev/input", dev)
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError as exc:
                    print(f">>> Unable to open {path}: {exc}")
                    status_display.error("Joystick open failed")
                    continue
                else:
                    os.close(fd)
                    print(f">>> {path} is readable (perm ok)")
        else:
            print(">>> No /dev/input/js* devices found")

        time.sleep(1)
        reinit_joystick()

        if (
            not hidapi_enabled
            and not hidapi_toggled
            and attempts >= 5
            and pygame.joystick.get_count() == 0
        ):
            os.environ["SDL_JOYSTICK_HIDAPI"] = "1"
            hidapi_toggled = True
            print(">>> No joystick via evdev; retrying with HIDAPI enabled")
            status_display.error("Retrying HIDAPI driver")
            reinit_joystick()
    if not running:
        sys.exit(0)
    js = pygame.joystick.Joystick(0)
    js.init()
    name = js.get_name()
    print(">>> Joystick connected", name)
    status_display.joystick_connected(name)
    controller_connected = True
    bt_name = name.lower()
    bluetooth_linked = "bluetooth" in bt_name or "wireless" in bt_name
    if bluetooth_linked:
        status_display.bluetooth_connected(name)
    publish_state(force=True)
    return js


js = wait_for_joystick()
max_speed = MAX_SPEED
deadzone = DEADZONE
zoom_speed = MAX_ZOOM_SPEED
zoom_state = ZoomCommandState()
zoom_trigger_state = ZoomTriggerState()
motion_state = MotionState()
button_edges = ButtonEdges()
last_input_log = 0.0
status_display.camera_active(cur, CAMS[cur][0])
status_display.boot("PTZ bridge ready")

last_send_log = 0.0
_cfg_mtime = 0.0


def reload_config_if_changed():
    global CAMS, CAMERA_NAMES, cur, max_speed, deadzone, zoom_speed, _cfg_mtime
    path = Path(os.environ.get("PTZPAD_CONFIG", "~/.config/ptzpad/config.json")).expanduser()
    try: mtime = path.stat().st_mtime
    except OSError: return
    if mtime <= _cfg_mtime: return
    _cfg_mtime = mtime
    try: cfg = load_config()
    except ValueError: return
    new = [(c["host"], c["protocol"], c["port"]) for c in cfg["cameras"]]
    if new != CAMS:
        stop_all_motion(CAMS[cur]); CAMS = new; cur = min(cur, len(CAMS) - 1); reset_input_state(); status_display.camera_active(cur, CAMS[cur][0])
    CAMERA_NAMES = [c.get("name") or c["host"] for c in cfg["cameras"]]
    if _streamdeck:
        _streamdeck.update(cur, _camera_label(cur), len(CAMS), _preset_save_armed)
    max_speed, deadzone, zoom_speed = cfg["max_speed"], cfg["deadzone"], cfg["zoom_speed"]


def send(pkt, cam, label: str | None = None):
    """Send VISCA packet and optionally log the action when debugging."""

    global last_send_log
    ip, proto, port = cam
    camera_state = _camera_send.setdefault(ip, {})
    camera_state.update({"last_command": label or "command", "protocol": proto,
                         "last_command_at": time.time()})
    if DEBUG_INPUT:
        now = time.time()
        if now - last_send_log >= DEBUG_INPUT_INTERVAL:
            label_safe = label or "command"
            print(
                ">>> SEND",
                label_safe.upper(),
                f"to {ip}:{port} ({proto})",
                f"len={len(pkt)}",
                "bytes:",
                pkt.hex(" "),
            )
            last_send_log = now
    try:
        if proto == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(pkt, (ip, port))
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect((ip, port))
                s.sendall(pkt)
        camera_state["last_success"] = time.time()
    except OSError as exc:
        camera_state["last_error"] = time.time()
        print(f">> Socket error to {ip}:{port}: {exc}")
        status_display.error("Socket send failed")
        publish_state(force=True)

def visca_move(x, y, cam):
    """Drive pan/tilt according to joystick input."""
    def speed(v: float) -> int:
        # Scale speed with stick deflection using a cubic curve for a very smooth ramp
        norm = (abs(v) - deadzone) / (1 - deadzone)
        norm = max(0.0, min(norm, 1.0))
        curve = norm ** 3
        return max(1, int(curve * (max_speed - 1)) + 1)

    pan_dir = 0x03
    tilt_dir = 0x03
    pan_speed = 0x00
    tilt_speed = 0x00

    if x < -deadzone:
        pan_dir = 0x01
        pan_speed = speed(x)
    elif x > deadzone:
        pan_dir = 0x02
        pan_speed = speed(x)

    if y > deadzone:  # y is inverted earlier
        tilt_dir = 0x01
        tilt_speed = speed(y)
    elif y < -deadzone:
        tilt_dir = 0x02
        tilt_speed = speed(y)

    command = (pan_speed, tilt_speed, pan_dir, tilt_dir)
    if motion_state.move_changed(command, cam[1], UDP_STOP_PACKETS):
        send(bytes([0x81, 0x01, 0x06, 0x01, *command, 0xFF]), cam, "move")

def visca_stop(cam):
    send(b"\x81\x01\x06\x01\x00\x00\x03\x03\xFF", cam, "stop")

def zoom(direction, cam):          # direction: 1 tele, -1 wide, 0 stop
    if direction > 0:
        cmd = bytes([0x20 + zoom_speed])
    elif direction < 0:
        cmd = bytes([0x30 + zoom_speed])
    else:
        cmd = b"\x00"
    send(b"\x81\x01\x04\x07" + cmd + b"\xFF", cam, "zoom")

def focus(direction, cam):         # direction: 1 far, -1 near, 0 stop
    if direction > 0:
        cmd = b"\x02"
    elif direction < 0:
        cmd = b"\x03"
    else:
        cmd = b"\x00"
    send(b"\x81\x01\x04\x08" + cmd + b"\xFF", cam, "focus")

def autofocus(cam):
    send(b"\x81\x01\x04\x18\x01\xFF", cam, "autofocus")


def stop_all_motion(cam):
    """Stop pan/tilt, zoom, and focus with bounded UDP stop-set retries."""
    stop_packets = UDP_STOP_PACKETS if cam[1].lower() == "udp" else 1
    for packet_index in range(stop_packets):
        visca_stop(cam)
        zoom(0, cam)
        focus(0, cam)
        if packet_index + 1 < stop_packets:
            time.sleep(LOOP_MS / 1000)


def reset_input_state() -> None:
    """Clear command suppression and trigger state after lifecycle changes."""

    zoom_state.reset()
    zoom_trigger_state.reset()
    motion_state.reset()
    button_edges.reset()
    _input_telemetry.update({
        "lt": None,
        "rt": None,
        "zoom_value": None,
        "zoom_direction": 0,
        "protocol": None,
    })


def _camera_label(index: int) -> str:
    return CAMERA_NAMES[index] if CAMERA_NAMES and index < len(CAMERA_NAMES) else (CAMS[index][0] if CAMS else "Camera")


def switch_camera(new_index: int) -> int:
    """Stop all motion on the current camera before selecting another."""
    old_cam = CAMS[cur]
    stop_all_motion(old_cam)
    reset_input_state()
    return new_index


def read_dpad(joystick) -> tuple[int, int]:
    """Read D-pad as an SDL hat, falling back to standard Xbox buttons."""
    try:
        if joystick.get_numhats() > 0:
            hat_x, hat_y = joystick.get_hat(0)
            if hat_x or hat_y:
                return hat_x, hat_y
    except (AttributeError, pygame.error):
        pass
    # HIDAPI exposes Xbox D-pad directions as buttons 11..14.  Guard the
    # lookup because some controllers advertise fewer buttons.
    try:
        button_count = joystick.get_numbuttons()
    except (AttributeError, pygame.error):
        button_count = 0
    if button_count < 15:
        return 0, 0
    return (
        int(joystick.get_button(14)) - int(joystick.get_button(13)),
        int(joystick.get_button(11)) - int(joystick.get_button(12)),
    )


def read_button(joystick, index: int) -> bool:
    """Read a button only when the controller advertises that index."""

    try:
        return bool(index < joystick.get_numbuttons() and joystick.get_button(index))
    except (AttributeError, pygame.error):
        return False


def process_streamdeck_actions() -> None:
    """Drain HID actions; all camera and VISCA state changes happen here."""
    global cur, _preset_save_armed
    while True:
        try:
            action = _deck_actions.get_nowait()
        except queue.Empty:
            break
        if action.kind == ActionKind.PREVIOUS_CAMERA and CAMS:
            cur = switch_camera((cur - 1) % len(CAMS))
            status_display.camera_active(cur, CAMS[cur][0])
        elif action.kind == ActionKind.NEXT_CAMERA and CAMS:
            cur = switch_camera((cur + 1) % len(CAMS))
            status_display.camera_active(cur, CAMS[cur][0])
        else:
            _preset_save_armed, packet, label = resolve_deck_action(action, _preset_save_armed)
            if packet is not None and label is not None and CAMS:
                send(packet, CAMS[cur], label)
        if _streamdeck:
            _streamdeck.update(cur, _camera_label(cur), len(CAMS), _preset_save_armed)


_streamdeck = StreamDeckController(_deck_actions)
_streamdeck.start()
_streamdeck.update(cur, _camera_label(cur), len(CAMS), _preset_save_armed)
print(">>> PTZ bridge running.  Cameras:", ", ".join(ip for ip, _, _ in CAMS))
while running:
    reload_config_if_changed()
    process_streamdeck_actions()
    publish_state()
    pygame.event.pump()
    status_display.refresh()
    if pygame.joystick.get_count() == 0:
        print(">>> Joystick disconnected")
        controller_connected = False
        status_display.joystick_disconnected()
        if bluetooth_linked:
            status_display.bluetooth_disconnected()
            bluetooth_linked = False
        stop_all_motion(CAMS[cur])
        reset_input_state()
        publish_state(force=True)
        js = wait_for_joystick()
        status_display.camera_active(cur, CAMS[cur][0])
        continue
    # camera cycling – A button (#0)
    if js.get_button(0):
        cur = switch_camera((cur + 1) % len(CAMS))
        time.sleep(0.25)          # debounce
        print(">> Control switched to CAM", cur + 1, CAMS[cur][0])
        status_display.camera_active(cur, CAMS[cur][0])
        if _streamdeck:
            _streamdeck.update(cur, _camera_label(cur), len(CAMS), _preset_save_armed)

    # Adjust max speed / deadzone with D-pad.  SDL exposes the Xbox D-pad as
    # a hat on some drivers and as buttons on others (notably HIDAPI), so
    # accept either representation.
    hat_x, hat_y = read_dpad(js)
    try:
        button_count = js.get_numbuttons()
        hat_count = js.get_numhats()
    except (AttributeError, pygame.error):
        button_count, hat_count = 0, 0
    layout = controller_layout(button_count, hat_count)
    edges = button_edges.rising({
        "LB": read_button(js, layout.lb),
        "RB": read_button(js, layout.rb),
        "LS": read_button(js, layout.ls),
    })
    if hat_y == 1:
        max_speed = min(max_speed + 1, MAX_SPEED)
        time.sleep(0.25)
        print(">> MAX_SPEED", max_speed)
    elif hat_y == -1:
        max_speed = max(max_speed - 1, 1)
        time.sleep(0.25)
        print(">> MAX_SPEED", max_speed)

    if hat_x == 1:
        deadzone = min(deadzone + 0.01, 0.5)
        time.sleep(0.25)
        print(f">> DEADZONE {deadzone:.2f}")
    elif hat_x == -1:
        deadzone = max(deadzone - 0.01, 0.0)
        time.sleep(0.25)
        print(f">> DEADZONE {deadzone:.2f}")

    # adjust zoom speed with RB (increase) / LB (decrease) bumpers
    if "RB" in edges:
        zoom_speed = min(zoom_speed + 1, MAX_ZOOM_SPEED)
        print(">> ZOOM_SPEED", zoom_speed)
    elif "LB" in edges:
        zoom_speed = max(zoom_speed - 1, 0x00)
        print(">> ZOOM_SPEED", zoom_speed)

    cam = CAMS[cur]
    x, y = js.get_axis(2), -js.get_axis(3)   # right stick (invert Y)
    visca_move(x, y, cam)

    fy = -js.get_axis(1)                     # left stick Y for focus
    if fy > FOCUS_DEADZONE:
        focus_dir = 1
    elif fy < -FOCUS_DEADZONE:
        focus_dir = -1
    else:
        focus_dir = 0
    focus_cmd = motion_state.next_focus(focus_dir, cam[1], UDP_STOP_PACKETS)
    if focus_cmd is not None:
        focus(focus_cmd, cam)

    if "LS" in edges:                       # left stick click
        autofocus(cam)

    rt = (js.get_axis(4) + 1) / 2  # right trigger (0..1)
    lt = (js.get_axis(5) + 1) / 2  # left trigger (0..1)
    zoom_val = rt - lt              # combine triggers

    zoom_dir = resolve_zoom_direction(
        zoom_val,
        zoom_trigger_state,
        start_deadzone=ZOOM_START_DEADZONE,
        release_loops=ZOOM_STOP_LOOPS,
    )
    _input_telemetry.update({
        "lt": round(lt, 3),
        "rt": round(rt, 3),
        "zoom_value": round(zoom_val, 3),
        "zoom_direction": zoom_dir,
        "protocol": cam[1],
    })

    zoom_cmd = next_zoom_command(zoom_dir, zoom_state, stop_packets=ZOOM_STOP_PACKETS)
    if zoom_cmd is not None:
        zoom(zoom_cmd, cam)

    if DEBUG_INPUT:
        now = time.time()
        if now - last_input_log >= DEBUG_INPUT_INTERVAL:
            axes = {
                "rx": f"{x:.2f}",
                "ry": f"{y:.2f}",
                "lx": f"{js.get_axis(0):.2f}",
                "ly": f"{js.get_axis(1):.2f}",
                "lt": f"{lt:.2f}",
                "rt": f"{rt:.2f}",
            }
            buttons = {"A": read_button(js, 0), "LB": read_button(js, layout.lb), "RB": read_button(js, layout.rb), "LS": read_button(js, layout.ls)}
            print(
                ">>> INPUT",
                axes,
                "hat=(",
                hat_x,
                hat_y,
                ")",
                "zoom_dir=",
                zoom_dir,
                "last_zoom_dir=",
                zoom_state.last_direction,
                "max_speed=",
                max_speed,
                "deadzone=",
                f"{deadzone:.2f}",
                "buttons=",
                buttons,
            )
            last_input_log = now

    time.sleep(LOOP_MS / 1000)

if CAMS and "cur" in globals():
    stop_all_motion(CAMS[cur])
if _streamdeck:
    _streamdeck.close()
pygame.quit()
