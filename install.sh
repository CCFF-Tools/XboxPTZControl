#!/usr/bin/env bash
# -----------------------------------------------------------
#  install.sh — Xbox PTZOptics controller installer
#  Usage: sudo bash install.sh
# -----------------------------------------------------------
set -euo pipefail

# Determine the non-root user and home directory
if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: run this installer as root (for example: sudo bash install.sh)" >&2
    exit 1
fi
for required_cmd in apt-get apt-cache systemctl getent id; do
    if ! command -v "${required_cmd}" >/dev/null 2>&1; then
        echo "error: required command not found: ${required_cmd}" >&2
        exit 1
    fi
done

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"
TARGET_USER="${TARGET_USER:-root}"
if ! PASSWD_ENTRY="$(getent passwd "${TARGET_USER}")"; then
    echo "error: could not resolve account ${TARGET_USER}" >&2
    exit 1
fi
TARGET_HOME="$(printf '%s\n' "${PASSWD_ENTRY}" | cut -d: -f6)"
if [[ -z "${TARGET_HOME}" || ! -d "${TARGET_HOME}" ]]; then
    echo "error: could not resolve home directory for ${TARGET_USER}" >&2
    exit 1
fi
if ! TARGET_GROUP="$(id -gn "${TARGET_USER}")"; then
    echo "error: could not resolve primary group for ${TARGET_USER}" >&2
    exit 1
fi

# Path to this script for referencing bundled files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ==== USER-CONFIGURABLE SECTION ============================================
CAMS=("tcp:192.168.1.150")    # proto:ip[:port], e.g., udp:192.168.1.151:1259

# ==== NO CHANGES NORMALLY NEEDED BELOW =====================================

# 1. Packages ---------------------------------------------------------------
echo "[1/5] Updating APT and installing packages…"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-pygame python3-pil i2c-tools libhidapi-libusb0

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

# Stream Deck support is optional and must not block bridge installation.
STREAMDECK_STATUS="success"
if /usr/bin/python3 -c 'import StreamDeck' >/dev/null 2>&1; then
    echo " • Stream Deck library already importable by /usr/bin/python3"
elif apt-cache show python3-elgato-streamdeck >/dev/null 2>&1; then
    if DEBIAN_FRONTEND=noninteractive apt-get install -y python3-elgato-streamdeck \
        && /usr/bin/python3 -c 'import StreamDeck' >/dev/null 2>&1; then
        echo " • Installed python3-elgato-streamdeck from APT"
    else
        STREAMDECK_STATUS="issues"
        echo " • WARNING: APT Stream Deck package install/import failed; bridge will run without it"
    fi
elif pip3 install streamdeck \
    && /usr/bin/python3 -c 'import StreamDeck' >/dev/null 2>&1; then
    echo " • Installed streamdeck Python package via pip"
else
    STREAMDECK_STATUS="issues"
    echo " • WARNING: Stream Deck package unavailable or not importable by /usr/bin/python3; bridge will run without it"
fi

if command -v raspi-config >/dev/null 2>&1; then
    if raspi-config nonint do_i2c 0; then
        echo " • I2C enabled via raspi-config"
    else
        OLED_STATUS="issues"
        OLED_NOTES+=("raspi-config could not enable I2C")
    fi
else
    echo " • raspi-config not available; enable I2C manually if using an OLED"
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

# Joystick permissions (needed for Bluetooth/USB gamepads)
if getent group input >/dev/null 2>&1; then
    if id -nG "${TARGET_USER}" | tr ' ' '\n' | grep -q '^input$'; then
        echo " • ${TARGET_USER} already in input group for joystick access"
    else
        if usermod -aG input "${TARGET_USER}"; then
            echo " • Added ${TARGET_USER} to input group for joystick access"
        else
            echo " • WARNING: failed to add ${TARGET_USER} to input group; joystick access may be blocked"
        fi
    fi
else
    echo " • WARNING: input group missing; joystick permissions may be blocked"
fi

# 3. Python joystick driver -------------------------------------------------
echo "[3/5] Installing ${TARGET_HOME}/ptzpad.py and dependencies …"
install -m 755 "${SCRIPT_DIR}/ptzpad.py" "${TARGET_HOME}/ptzpad.py"
install -m 644 "${SCRIPT_DIR}/zoom_control.py" "${TARGET_HOME}/zoom_control.py"
install -m 644 "${SCRIPT_DIR}/input_control.py" "${TARGET_HOME}/input_control.py"
install -m 644 "${SCRIPT_DIR}/oled_status.py" "${TARGET_HOME}/oled_status.py"
install -m 644 "${SCRIPT_DIR}/streamdeck_control.py" "${TARGET_HOME}/streamdeck_control.py"
install -m 755 "${SCRIPT_DIR}/snapshot_diagnostic.py" "${TARGET_HOME}/snapshot_diagnostic.py"
install -m 755 "${SCRIPT_DIR}/ptz_dashboard.py" "${TARGET_HOME}/ptz_dashboard.py"
install -m 644 "${SCRIPT_DIR}/ptz_config.py" "${TARGET_HOME}/ptz_config.py"
chown "${TARGET_USER}:${TARGET_GROUP}" "${TARGET_HOME}/ptzpad.py" "${TARGET_HOME}/streamdeck_control.py" "${TARGET_HOME}/snapshot_diagnostic.py" "${TARGET_HOME}/zoom_control.py" "${TARGET_HOME}/input_control.py" "${TARGET_HOME}/oled_status.py" "${TARGET_HOME}/ptz_dashboard.py" "${TARGET_HOME}/ptz_config.py"

if getent group input >/dev/null 2>&1; then
    printf 'SUBSYSTEM=="usb", ATTR{idVendor}=="0fd9", MODE="0660", GROUP="input"\n' > /etc/udev/rules.d/99-ptzpad-streamdeck.rules
    if command -v udevadm >/dev/null 2>&1; then
        udevadm control --reload-rules || true
        udevadm trigger --subsystem-match=usb --attr-match=idVendor=0fd9 || true
    fi
    echo " • Installed Stream Deck udev rule (/etc/udev/rules.d/99-ptzpad-streamdeck.rules)"
else
    echo " • WARNING: input group missing; Stream Deck permissions may be blocked"
fi
CONFIG_DIR="${TARGET_HOME}/.config/ptzpad"
install -d -m 700 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
    printf '{"cameras":[' > "${CONFIG_DIR}/config.json"
    first=1; for cam in "${CAMS[@]}"; do proto="${cam%%:*}"; rest="${cam#*:}"; host="${rest%%:*}"; port="${rest#*:}"; [[ "${rest}" == "${host}" ]] && port=5678; [[ "${proto}" == udp ]] && [[ "${rest}" == "${host}" ]] && port=1259; [[ $first -eq 0 ]] && printf ',' >> "${CONFIG_DIR}/config.json"; first=0; printf '{"host":"%s","protocol":"%s","port":%s,"name":"%s","model":""}' "$host" "$proto" "$port" "$host" >> "${CONFIG_DIR}/config.json"; done
    printf '],"max_speed":12,"deadzone":0.15,"zoom_speed":3,"controls":{"y_button_zoom_speed_up":false}}\n' >> "${CONFIG_DIR}/config.json"; chmod 600 "${CONFIG_DIR}/config.json"; chown "${TARGET_USER}:${TARGET_GROUP}" "${CONFIG_DIR}/config.json"
fi
if getent group systemd-journal >/dev/null 2>&1; then usermod -aG systemd-journal "${TARGET_USER}" || true; echo " • Added ${TARGET_USER} to systemd-journal (log out/in to apply)"; fi

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
Environment=PTZPAD_CONFIG=${TARGET_HOME}/.config/ptzpad/config.json
Environment=PTZPAD_STATE=/run/ptzpad/status.json
Environment=SDL_JOYSTICK_HIDAPI=0
Environment=SDL_VIDEODRIVER=dummy
Environment=XDG_RUNTIME_DIR=/run/ptzpad
ExecStart=/usr/bin/python3 ${TARGET_HOME}/ptzpad.py
WorkingDirectory=${TARGET_HOME}
Restart=always
RestartSec=2
EnvironmentFile=-/etc/default/ptzpad
RuntimeDirectory=ptzpad
RuntimeDirectoryMode=0700
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
UNIT

# 5. Enable + start ---------------------------------------------------------
echo "[5/5] Enabling and starting service…"
systemctl daemon-reload
systemctl enable ptzpad.service
systemctl restart ptzpad.service

cat > /etc/systemd/system/ptzpad-dashboard.service <<UNIT
[Unit]
Description=PTZPad LAN dashboard
After=network-online.target ptzpad.service
[Service]
User=${TARGET_USER}
Environment=PTZPAD_CONFIG=${TARGET_HOME}/.config/ptzpad/config.json
Environment=PTZPAD_STATE=/run/ptzpad/status.json
ExecStart=/usr/bin/python3 ${TARGET_HOME}/ptz_dashboard.py
WorkingDirectory=${TARGET_HOME}
Environment=PTZPAD_BIND=0.0.0.0
Environment=PTZPAD_PORT=8080
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable ptzpad-dashboard.service
systemctl restart ptzpad-dashboard.service

echo "--------------------------------------------------------------------"
echo "Done!  The service is active.  Default camera(s): ${CAMS[*]}"
echo "• To check logs:  journalctl -u ptzpad.service -f"
echo "• To edit camera IPs later: edit /etc/default/ptzpad and restart the service"
echo "• Installed files: ${TARGET_HOME}/ptzpad.py, ${TARGET_HOME}/streamdeck_control.py, ${TARGET_HOME}/zoom_control.py, ${TARGET_HOME}/input_control.py, and ${TARGET_HOME}/oled_status.py"
echo "• Dashboard: http://<this-host>:8080/ (token stored in ${TARGET_HOME}/.config/ptzpad/token)"
echo "• Reboot test:    sudo reboot"
if [[ "${OLED_STATUS}" == "success" ]]; then
    echo "OLED setup: success (luma.oled + I2C ready)"
else
    echo "OLED setup: issues encountered"
    printf ' - %s\n' "${OLED_NOTES[@]}"
fi
if [[ "${STREAMDECK_STATUS}" == "success" ]]; then
    echo "Stream Deck setup: success (Python library import verified + libhidapi-libusb0)"
else
    echo "Stream Deck setup: optional package unavailable; bridge remains functional without a deck"
fi
