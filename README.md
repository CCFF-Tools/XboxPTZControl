# XboxPTZControl

## LAN dashboard

The installer also enables `ptzpad-dashboard.service`, a dependency-free browser dashboard on port 8080. Open `http://<raspberry-pi-ip>:8080/` and enter the token from `~/.config/ptzpad/token` (mode 600). The dashboard shows bridge health, host load and uptime, camera reachability/address/model metadata, connected joystick devices, live tuning values, and searchable journal logs.

Camera and tuning settings are stored atomically in `~/.config/ptzpad/config.json`. The dashboard validates edits and ptzpad hot-reloads them, stopping motion on a replaced camera. Existing `PTZ_CAMS` remains supported as a fallback. Runtime state is published to `/run/ptzpad/status.json`; if permissions prevent that path, choose a user-writable `PTZPAD_STATE`.

The token protects every API, including status and logs. Keep port 8080 on a trusted LAN; this service does not provide TLS. Set `PTZPAD_BIND`, `PTZPAD_PORT`, `PTZPAD_TOKEN_FILE`, or `PTZPAD_STATE` in the dashboard unit to customize deployment. Rotate the token by deleting the token file and restarting `ptzpad-dashboard`.

If the dashboard reports stale/offline, check `systemctl status ptzpad-dashboard ptzpad` and `journalctl -u ptzpad.service`.
Turn any Raspberry Pi 3 B (or newer) into a headless VISCA-over-IP joystick server that lets an Xbox One / Series X|S controller drive one or many PTZOptics cameras.

## Repository structure

The main deliverable is a single installation script (`install.sh`) that:

- Installs Python 3, pip and `pygame`
- Writes the `ptzpad.py` controller bridge to the invoking user's home directory
- Creates and enables a `ptzpad.service` so the bridge starts on boot

The installer copies `ptzpad.py` and `oled_status.py` into the invoking user's home directory. The driver reads camera IP/port from environment variables, polls the controller with `pygame`, and sends VISCA-over-IP commands over TCP or UDP.

## Quick start

```bash
git clone https://github.com/CCFF-Tools/XboxPTZControl.git
cd XboxPTZControl
sudo bash install.sh            # edit CAMS array at top if needed
```

Camera addresses can be changed by editing the `CAMS` array at the top of `install.sh` or by exporting `PTZ_CAMS` when launching `ptzpad.py` directly, for example:

```bash
PTZ_CAMS=tcp:192.168.10.44,udp:192.168.10.54 python3 ~/ptzpad.py
```

The installer seeds `/etc/default/ptzpad` with the current `CAMS` values, and the systemd service (running as the invoking non-root user) reads `PTZ_CAMS` from that environment file on startup. A shell `export` affects only commands launched from that shell; edit `/etc/default/ptzpad` to persist service settings.

Hardware you need:

- Raspberry Pi 3 B or newer running Raspberry Pi OS (32-bit, bullseye or bookworm)
- Xbox One / Series X|S controller (wired USB recommended)
- PTZOptics camera(s) with VISCA-over-IP enabled (default TCP 5678)
- Optional: 128×64 SSD1306 I2C OLED (for live status: boot, joystick/Bluetooth link, active camera, errors)

## OLED status display

The OLED is optional. When present and reachable at I2C address `0x3C`, it shows boot progress, joystick/Bluetooth link state, the active camera index/IP, and socket or configuration errors. Missing hardware or driver issues are handled gracefully: the service logs one message and continues without screen output.

- **Hardware wiring (SSD1306 128×64 over I2C):**
  - VCC → 3.3 V (e.g., pin 1 or 17 on the 40-pin header)
  - GND → any ground (e.g., pin 6)
  - SDA → GPIO 2 (pin 3)
  - SCL → GPIO 3 (pin 5)
  - Keep the display on the 3.3 V rail; most SSD1306 breakout boards default to I2C address `0x3C`.
- **Packages and configuration:** `install.sh` installs `python3-pil`, `i2c-tools`, and `luma.oled` (via pip), and attempts to enable I2C via `raspi-config` when that Raspberry Pi utility is available. If you are setting up manually, install those packages and ensure the service user is in the `i2c` group.
- **What you should see:**
  - Boot: “Parsing cameras…”, “Starting pygame…”, and “Waiting for joystick…” as setup progresses.
  - Runtime: “Joystick connected” with the controller name, “Bluetooth linked” (for wireless controllers), the active camera number/IP, and “PTZ bridge ready”.
  - Errors: configuration or socket issues render an “Error” banner with a brief code or message.
- **Disable the OLED:** Leave the display disconnected or uninstall `luma.oled`; the bridge will log “OLED display unavailable; running without screen” and operate normally with no OLED output.

OLED settings can be overridden with environment variables if you use a different I2C bus or address:

```bash
export OLED_I2C_BUS=3          # defaults to bus 3
export OLED_I2C_ADDRESS=0x3C   # accepts hex (0x3C) or decimal (60)
```

For example, when using a software I2C overlay on GPIO 23/24 configured as bus 3, set `OLED_I2C_BUS=3` before launching the service so the display driver opens the correct bus.

## Default controls

| Input | Action |
|-------|--------|
| Right stick | Pan / tilt (speed scales with a cubic curve for a smoother ramp) |
| Left stick up/down | Focus far/near (medium deadzone) |
| Left stick click | One-time autofocus |
| RT | Zoom in (repeats while held) |
| LT | Zoom out (repeats while held) |
| A | Cycle to next camera |
| D-pad up/down | Increase / decrease max speed |
| D-pad left/right | Increase / decrease deadzone |
| RB / LB | Increase / decrease zoom speed |

## Customising after install

- Change camera names, models, IPs, ports, protocol, or tuning values in the dashboard. The bridge validates and hot-reloads `~/.config/ptzpad/config.json` without a restart.

- Change camera IPs/ports (one-off):

```bash
export PTZ_CAMS=tcp:192.168.10.44,udp:192.168.10.54
# format: proto:ip[:port] (defaults 5678 TCP, 1259 UDP)
```

- Adjust speed / dead-zone / zoom speed / zoom dead-zone: use the D-pad or RB/LB bumpers, or edit `MAX_SPEED`, `DEADZONE`, `MAX_ZOOM_SPEED`, `ZOOM_START_DEADZONE` and `ZOOM_STOP_DEADZONE` in `~/ptzpad.py`.

## Service management

```bash
sudo systemctl status ptzpad
sudo systemctl restart ptzpad
sudo journalctl -u ptzpad -f   # live logs
systemctl show -p Environment ptzpad.service
```

The bridge handles `SIGTERM`/`SIGINT`, allowing `systemctl stop ptzpad` or `Ctrl+C` to terminate it quickly. The service is configured to restart automatically if the bridge crashes.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Service prints `Waiting for joystick connection…` | Check USB cable/port; `lsusb` should list the Xbox controller. |
| Controller works with `jstest` but OLED stays on “Waiting for joystick” | Ensure the service user (the account that ran `sudo`) is in the `input` group so it can open `/dev/input/js*`, then restart the service or replug the controller. The service now forces the headless SDL “dummy” video driver and will automatically retry with `SDL_JOYSTICK_HIDAPI=1` if evdev devices (e.g., `/dev/input/js0`) exist but pygame still reports zero joysticks. |
| `/dev/input/js0` exists but still waiting for joystick | Logs now show whether `/dev/input/js*` are readable (or why they fail to open). If you see “Joystick open failed”, fix permissions/udev and restart. To stream joystick input samples and throttled VISCA sends, set `PTZPAD_DEBUG_INPUT=1` in the service environment (e.g., add `PTZPAD_DEBUG_INPUT=1` to `/etc/default/ptzpad`, `sudo systemctl daemon-reload`, and `sudo systemctl restart ptzpad`). |
| Journal shows `XDG_RUNTIME_DIR is invalid or not set` | Install via `install.sh` or set `XDG_RUNTIME_DIR=/run/ptzpad`, `RuntimeDirectory=ptzpad`, and `RuntimeDirectoryMode=0700` in the service so SDL/pygame have a writable runtime directory. At startup the script creates the configured directory; if that path is unavailable, it falls back to a private directory under the system temporary directory. |
| OLED stays blank or shows garbled text | Confirm the display answers at `0x3C` on the configured bus (default `i2cdetect -y 3`), and recheck SDA (GPIO 2) / SCL (GPIO 3) wiring, 3.3 V power, and ground. |
| `Connection refused` | Wrong port or VISCA-TCP disabled in camera web UI. |
| Jerky / slow moves | Keep ≥40 ms between VISCA packets (`LOOP_MS`), use wired LAN. |
| Zoom jitter or stops while holding trigger | Tweak `ZOOM_START_DEADZONE`/`ZOOM_STOP_DEADZONE` to filter trigger noise and adjust `ZOOM_REPEAT_MS` for repeat rate. Zoom continues until the trigger rests inside the stop deadzone for a few loops. |
| Lag after 30 s idle | Some cameras drop idle TCP; check the camera's network timeout and use wired LAN where possible. |

## Where to go next

- Explore the VISCA protocol to add more camera features.
- Expand controller mapping to handle additional buttons or advanced behaviors.
- Learn more about `systemd` for tuning how the service runs and logs.

## Uninstall

```bash
sudo systemctl disable --now ptzpad-dashboard ptzpad
sudo rm /etc/systemd/system/ptzpad-dashboard.service /etc/systemd/system/ptzpad.service
sudo rm -f /etc/default/ptzpad
sudo systemctl daemon-reload
rm -f ~/ptzpad.py ~/ptz_dashboard.py ~/ptz_config.py ~/oled_status.py
# Optional: remove saved configuration and the dashboard token.
rm -rf ~/.config/ptzpad
```
