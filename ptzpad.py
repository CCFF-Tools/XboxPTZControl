#!/usr/bin/env python3
# Xbox-One → PTZOptics VISCA-over-IP bridge
import os
import sys

# Force SDL to use the headless video driver to avoid XDG runtime complaints on
# systems without a graphical session (e.g., the service unit).
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")


def ensure_runtime_dir() -> str:
    """Guarantee SDL has a writable runtime directory before importing pygame."""

    xdg_dir = os.environ.get("XDG_RUNTIME_DIR") or "/run/ptzpad"
    os.environ["XDG_RUNTIME_DIR"] = xdg_dir
    try:
        os.makedirs(xdg_dir, mode=0o700, exist_ok=True)
        os.chmod(xdg_dir, 0o700)
    except OSError as exc:
        print(
            f"error: could not prepare XDG_RUNTIME_DIR {xdg_dir}: {exc}",
            file=sys.stderr,
        )
    return xdg_dir


ensure_runtime_dir()

import logging
import pygame
import signal
import socket
import sys
import time

from oled_status import OledStatus

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
        ip = parts[0]
        if len(parts) > 1 and parts[1]:
            try:
                port = int(parts[1])
            except ValueError:
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

CAMS = parse_cams(status_display)  # env override with proto:ip[:port]
MAX_SPEED = 0x18                 # 0x01 (slow) ... 0x18 (fast)
DEADZONE = 0.15                 # stick slack
FOCUS_DEADZONE = 0.20           # left stick focus deadzone
MAX_ZOOM_SPEED = 0x07           # 0x00 (slow) ... 0x07 (fast)
ZOOM_START_DEADZONE = 0.10      # trigger slack for zoom start
ZOOM_STOP_DEADZONE = 0.05       # smaller slack to stop zoom
ZOOM_REPEAT_MS = 200            # repeat zoom command every N ms
ZOOM_STOP_LOOPS = 3             # require this many loops below stop threshold
LOOP_MS = 50                    # command period (ms)
DEBUG_INPUT = os.environ.get("PTZPAD_DEBUG_INPUT", "").lower() in ("1", "true", "yes")
DEBUG_INPUT_INTERVAL = 0.25     # seconds between debug samples
# ---------------------------------------------------------------------------

running = True
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


def wait_for_joystick() -> pygame.joystick.Joystick:
    """Block until a joystick is available, returning it."""
    global bluetooth_linked
    status_display.joystick_wait()

    def reinit_joystick() -> None:
        pygame.joystick.quit()
        pygame.joystick.init()

    hidapi_env = os.environ.get("SDL_JOYSTICK_HIDAPI", "0")
    hidapi_enabled = hidapi_env not in ("0", "false", "no")
    hidapi_toggled = False
    attempts = 0

    while pygame.joystick.get_count() == 0 and running:
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
    bt_name = name.lower()
    bluetooth_linked = "bluetooth" in bt_name or "wireless" in bt_name
    if bluetooth_linked:
        status_display.bluetooth_connected(name)
    return js


js = wait_for_joystick()
cur = 0                           # current CAM index
max_speed = MAX_SPEED
deadzone = DEADZONE
zoom_speed = MAX_ZOOM_SPEED
last_zoom_dir = 0              # last zoom command sent
zoom_stop_count = 0            # loops below stop threshold
last_zoom_sent = 0.0           # ms timestamp of last zoom command
last_input_log = 0.0
last_send_log = 0.0
status_display.camera_active(cur, CAMS[cur][0])
status_display.boot("PTZ bridge ready")

def send(pkt, cam, action: str | None = None):
    global last_send_log
    ip, proto, port = cam
    if DEBUG_INPUT:
        now = time.time()
        if now - last_send_log >= DEBUG_INPUT_INTERVAL:
            print(
                ">>> SEND",
                (action or "command").upper(),
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
    except OSError as exc:
        print(f">> Socket error to {ip}:{port}: {exc}")
        status_display.error("Socket send failed")

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

    pkt = bytes([0x81,0x01,0x06,0x01, pan_speed, tilt_speed, pan_dir, tilt_dir, 0xFF])
    send(pkt, cam, action="pan/tilt")

def visca_stop(cam):
    send(b"\x81\x01\x06\x01\x00\x00\x03\x03\xFF", cam, action="pan/tilt")

def zoom(direction, cam):          # direction: 1 tele, -1 wide, 0 stop
    if direction > 0:
        cmd = bytes([0x20 + zoom_speed])
    elif direction < 0:
        cmd = bytes([0x30 + zoom_speed])
    else:
        cmd = b"\x00"
    send(b"\x81\x01\x04\x07" + cmd + b"\xFF", cam, action="zoom")

def focus(direction, cam):         # direction: 1 far, -1 near, 0 stop
    if direction > 0:
        cmd = b"\x02"
    elif direction < 0:
        cmd = b"\x03"
    else:
        cmd = b"\x00"
    send(b"\x81\x01\x04\x08" + cmd + b"\xFF", cam, action="focus")

def autofocus(cam):
    send(b"\x81\x01\x04\x18\x01\xFF", cam, action="focus")

print(">>> PTZ bridge running.  Cameras:", ", ".join(ip for ip, _, _ in CAMS))
while running:
    pygame.event.pump()
    status_display.refresh()
    if pygame.joystick.get_count() == 0:
        print(">>> Joystick disconnected")
        status_display.joystick_disconnected()
        if bluetooth_linked:
            status_display.bluetooth_disconnected()
            bluetooth_linked = False
        visca_stop(CAMS[cur])
        js = wait_for_joystick()
        status_display.camera_active(cur, CAMS[cur][0])
        continue
    # camera cycling – A button (#0)
    if js.get_button(0):
        cur = (cur + 1) % len(CAMS)
        time.sleep(0.25)          # debounce
        print(">> Control switched to CAM", cur + 1, CAMS[cur][0])
        status_display.camera_active(cur, CAMS[cur][0])

    # adjust max speed / deadzone with D-pad
    hat_x, hat_y = js.get_hat(0)
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
    if js.get_button(5):
        zoom_speed = min(zoom_speed + 1, MAX_ZOOM_SPEED)
        time.sleep(0.25)
        print(">> ZOOM_SPEED", zoom_speed)
    elif js.get_button(4):
        zoom_speed = max(zoom_speed - 1, 0x00)
        time.sleep(0.25)
        print(">> ZOOM_SPEED", zoom_speed)

    cam = CAMS[cur]
    x, y = js.get_axis(2), -js.get_axis(3)   # right stick (invert Y)
    if abs(x) > deadzone or abs(y) > deadzone:
        visca_move(x, y, cam)
    else:
        visca_stop(cam)

    fy = -js.get_axis(1)                     # left stick Y for focus
    if fy > FOCUS_DEADZONE:
        focus(1, cam)
    elif fy < -FOCUS_DEADZONE:
        focus(-1, cam)
    else:
        focus(0, cam)

    if js.get_button(9):                     # left stick click
        autofocus(cam)
        time.sleep(0.25)

    rt = (js.get_axis(4) + 1) / 2  # right trigger (0..1)
    lt = (js.get_axis(5) + 1) / 2  # left trigger (0..1)
    zoom_val = rt - lt              # combine triggers

    if abs(zoom_val) > ZOOM_START_DEADZONE:
        zoom_dir = 1 if zoom_val > 0 else -1
        zoom_stop_count = 0
    elif abs(zoom_val) < ZOOM_STOP_DEADZONE:
        zoom_stop_count += 1
        if zoom_stop_count >= ZOOM_STOP_LOOPS:
            zoom_dir = 0
        else:
            zoom_dir = last_zoom_dir
    else:
        zoom_dir = last_zoom_dir
        zoom_stop_count = 0

    now_ms = time.time() * 1000
    send_cmd = False
    if zoom_dir != last_zoom_dir:
        send_cmd = True
    elif zoom_dir != 0 and now_ms - last_zoom_sent >= ZOOM_REPEAT_MS:
        send_cmd = True
    if send_cmd:
        zoom(zoom_dir, cam)
        last_zoom_sent = now_ms
    last_zoom_dir = zoom_dir

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
            buttons = {"A": js.get_button(0), "LB": js.get_button(4), "RB": js.get_button(5), "LS": js.get_button(9)}
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
                last_zoom_dir,
                "max_speed=",
                max_speed,
                "deadzone=",
                f"{deadzone:.2f}",
                "buttons=",
                buttons,
            )
            last_input_log = now

    time.sleep(LOOP_MS / 1000)

pygame.quit()
