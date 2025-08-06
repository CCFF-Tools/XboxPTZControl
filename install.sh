#!/usr/bin/env bash
# -----------------------------------------------------------
#  xbox_ptzoptics_setup.sh  —  Zero-to-working in one shot
#  Usage:  sudo bash xbox_ptzoptics_setup.sh
# -----------------------------------------------------------
set -euo pipefail

# Determine the non-root user and home directory
TARGET_USER="${SUDO_USER:-$(whoami)}"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"

# ==== USER-CONFIGURABLE SECTION ============================================
CAMS=("192.168.1.150")        # Add more IPs in quotes as needed
CAM_PORT=5678                 # PTZOptics TCP VISCA port (UDP == 1259)

# ==== NO CHANGES NORMALLY NEEDED BELOW =====================================

# 1. Packages ---------------------------------------------------------------
echo "[1/4] Updating APT and installing packages…"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-pygame

# 2. Python joystick driver -------------------------------------------------
echo "[2/4] Writing ${TARGET_HOME}/ptzpad.py …"
cat > "${TARGET_HOME}/ptzpad.py" <<'PY'
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
    # x,y are −1…1 floats → speed-coded bytes
    def b(val): return int((val * 0.5 + 0.5) * MAX_SPEED + 1) & 0xFF
    spd = 0x05
    pkt = bytes([0x81,0x01,0x06,0x01, spd, spd, b(x), b(y), 0xFF])
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
PY
chmod +x "${TARGET_HOME}/ptzpad.py"
chown "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/ptzpad.py"

# 3. systemd unit -----------------------------------------------------------
echo "[3/4] Creating systemd service…"
cat > /etc/systemd/system/ptzpad.service <<UNIT
[Unit]
Description=Xbox-to-PTZOptics bridge
After=network-online.target

[Service]
User=${TARGET_USER}
ExecStart=/usr/bin/python3 ${TARGET_HOME}/ptzpad.py
Restart=on-failure
Environment="PTZ_CAMS=%i"

[Install]
WantedBy=multi-user.target
UNIT

# 4. Enable + start ---------------------------------------------------------
echo "[4/4] Enabling and starting service…"
systemctl daemon-reload
systemctl enable --now ptzpad.service

echo "--------------------------------------------------------------------"
echo "Done!  The service is active.  Default camera(s): ${CAMS[*]}"
echo "• To check logs:  journalctl -u ptzpad.service -f"
echo "• To edit camera IPs later:  sudo nano ${TARGET_HOME}/ptzpad.py  (or set PTZ_CAMS env)"
echo "• Reboot test:    sudo reboot"
