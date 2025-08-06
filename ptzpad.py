#!/usr/bin/env python3
# Xbox-One → PTZOptics VISCA-over-IP bridge
import pygame, socket, time, os, sys

# ---- CONFIG ---------------------------------------------------------------
CAMS = os.environ.get("PTZ_CAMS", "192.168.1.150").split(",")  # env override
CAM_PORT = int(os.environ.get("PTZ_PORT", "5678"))
MAX_SPEED = 0x18                 # 0x01 (slow) … 0x18 (fast)
DEADZONE  = 0.15                 # stick slack
LOOP_MS   = 50                   # command period (ms)
# ---------------------------------------------------------------------------

pygame.init()
if pygame.joystick.get_count() == 0:
    sys.exit(">>> No joystick detected – plug in the controller and retry.")
js = pygame.joystick.Joystick(0); js.init()
cur = 0                           # current CAM index

def send(pkt, ip):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        s.connect((ip, CAM_PORT))
        s.sendall(pkt)

def visca_move(x, y, ip):
    """Drive pan/tilt according to joystick input."""
    def speed(v: float) -> int:
        return max(1, min(int(abs(v) * MAX_SPEED), MAX_SPEED))

    pan_dir = 0x03
    tilt_dir = 0x03
    pan_speed = 0x00
    tilt_speed = 0x00

    if x < -DEADZONE:
        pan_dir = 0x01
        pan_speed = speed(x)
    elif x > DEADZONE:
        pan_dir = 0x02
        pan_speed = speed(x)

    if y > DEADZONE:  # y is inverted earlier
        tilt_dir = 0x01
        tilt_speed = speed(y)
    elif y < -DEADZONE:
        tilt_dir = 0x02
        tilt_speed = speed(y)

    pkt = bytes([0x81,0x01,0x06,0x01, pan_speed, tilt_speed, pan_dir, tilt_dir, 0xFF])
    send(pkt, ip)

def visca_stop(ip):
    send(b"\x81\x01\x06\x01\x00\x00\x03\x03\xFF", ip)

def zoom(cmd, ip):                # cmd: b'\x2F' tele, b'\x3F' wide, b'\x00' stop
    send(b"\x81\x01\x04\x07" + cmd + b"\xFF", ip)

print(">>> PTZ bridge running.  Cameras:", ", ".join(CAMS))
while True:
    pygame.event.pump()
    # camera cycling – LB button (#4)
    if js.get_button(4):
        cur = (cur + 1) % len(CAMS)
        time.sleep(0.25)          # debounce
        print(">> Control switched to CAM", cur+1, CAMS[cur])

    ip = CAMS[cur]
    x, y = js.get_axis(0), -js.get_axis(1)   # left stick (invert Y)
    if abs(x) > DEADZONE or abs(y) > DEADZONE:
        visca_move(x, y, ip)
    else:
        visca_stop(ip)

    rt, lt = js.get_axis(5), js.get_axis(2)  # triggers
    if rt < -0.3:   zoom(b"\x2F", ip)        # zoom tele
    elif lt < -0.3: zoom(b"\x3F", ip)        # zoom wide
    else:           zoom(b"\x00", ip)        # stop zoom

    time.sleep(LOOP_MS / 1000)
