#!/usr/bin/env bash
# -----------------------------------------------------------
#  xbox_ptzoptics_setup.sh  —  Zero-to-working in one shot
#  Usage:  sudo bash xbox_ptzoptics_setup.sh
# -----------------------------------------------------------
set -euo pipefail

# Determine the non-root user and home directory
TARGET_USER="${SUDO_USER:-$(whoami)}"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"

# Path to this script for referencing bundled files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ==== USER-CONFIGURABLE SECTION ============================================
CAMS=("tcp:192.168.1.150")    # proto:ip[:port], e.g., udp:192.168.1.151:1259

# ==== NO CHANGES NORMALLY NEEDED BELOW =====================================

# 1. Packages ---------------------------------------------------------------
echo "[1/4] Updating APT and installing packages…"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-pygame

# 2. Python joystick driver -------------------------------------------------
echo "[2/4] Installing ${TARGET_HOME}/ptzpad.py …"
install -m 755 "${SCRIPT_DIR}/ptzpad.py" "${TARGET_HOME}/ptzpad.py"
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
TimeoutStopSec=5

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
