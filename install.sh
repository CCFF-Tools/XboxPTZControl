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
echo "[1/5] Updating APT and installing packages…"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-pygame python3-pil i2c-tools

# 2. OLED + I2C setup -------------------------------------------------------
echo "[2/5] Configuring OLED dependencies and I2C…"

OLED_STATUS="success"
OLED_NOTES=()

if pip3 show luma.oled >/dev/null 2>&1; then
    echo " • luma.oled already present"
else
    if pip3 install luma.oled; then
        echo " • Installed luma.oled via pip"
    else
        OLED_STATUS="issues"
        OLED_NOTES+=("pip install luma.oled failed")
    fi
fi

if command -v raspi-config >/dev/null 2>&1; then
    if raspi-config nonint do_i2c 0; then
        echo " • I2C enabled via raspi-config"
    else
        OLED_STATUS="issues"
        OLED_NOTES+=("raspi-config could not enable I2C")
    fi
else
    OLED_STATUS="issues"
    OLED_NOTES+=("raspi-config not available; enable I2C manually")
fi

if getent group i2c >/dev/null 2>&1; then
    if id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -q '^i2c$'; then
        echo " • ${TARGET_USER} already in i2c group"
    else
        if usermod -aG i2c "${TARGET_USER}"; then
            echo " • Added ${TARGET_USER} to i2c group for /dev/i2c-* access"
        else
            OLED_STATUS="issues"
            OLED_NOTES+=("failed to add ${TARGET_USER} to i2c group")
        fi
    fi
else
    OLED_STATUS="issues"
    OLED_NOTES+=("i2c group missing; cannot set device permissions")
fi

# 3. Python joystick driver -------------------------------------------------
echo "[3/5] Installing ${TARGET_HOME}/ptzpad.py and oled_status.py …"
install -m 755 "${SCRIPT_DIR}/ptzpad.py" "${TARGET_HOME}/ptzpad.py"
install -m 644 "${SCRIPT_DIR}/oled_status.py" "${TARGET_HOME}/oled_status.py"
chown "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/ptzpad.py" "${TARGET_HOME}/oled_status.py"

# 4. systemd unit -----------------------------------------------------------
echo "[4/5] Creating systemd service…"
CAM_LIST=$(IFS=,; echo "${CAMS[*]}")
if [[ ! -f /etc/default/ptzpad ]]; then
    printf "PTZ_CAMS=%s\n" "${CAM_LIST}" > /etc/default/ptzpad
fi
cat > /etc/systemd/system/ptzpad.service <<UNIT
[Unit]
Description=Xbox-to-PTZOptics bridge
After=network-online.target
StartLimitIntervalSec=0

[Service]
User=${TARGET_USER}
ExecStart=/usr/bin/python3 ${TARGET_HOME}/ptzpad.py
WorkingDirectory=${TARGET_HOME}
Restart=always
RestartSec=2
EnvironmentFile=-/etc/default/ptzpad
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
UNIT

# 5. Enable + start ---------------------------------------------------------
echo "[5/5] Enabling and starting service…"
systemctl daemon-reload
systemctl enable --now ptzpad.service

echo "--------------------------------------------------------------------"
echo "Done!  The service is active.  Default camera(s): ${CAMS[*]}"
echo "• To check logs:  journalctl -u ptzpad.service -f"
echo "• To edit camera IPs later:  update /etc/default/ptzpad and restart the service (or edit ${TARGET_HOME}/ptzpad.py)"
echo "• Reboot test:    sudo reboot"
if [[ "${OLED_STATUS}" == "success" ]]; then
    echo "OLED setup: success (luma.oled + I2C ready)"
else
    echo "OLED setup: issues encountered"
    printf ' - %s\n' "${OLED_NOTES[@]}"
fi
