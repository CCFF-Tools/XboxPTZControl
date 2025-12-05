# XboxPTZControl

Turn any Raspberry Pi 3 B (or newer) into a headless VISCA-over-IP joystick server that lets an Xbox One / Series X|S controller drive one or many PTZOptics cameras.

## Repository structure

The main deliverable is a single installation script (`install.sh`) that:

- Installs Python 3, pip and `pygame`
- Writes the `ptzpad.py` controller bridge to the invoking user's home directory
- Creates and enables a `ptzpad.service` so the bridge starts on boot

The Python driver is embedded within the script. It reads camera IP/port from environment variables, polls the controller with `pygame`, and sends VISCA-over-IP commands over TCP or UDP.

## Quick start

```bash
git clone https://github.com/CCFF-Tools/XboxPTZControl.git
cd XboxPTZControl
sudo bash install.sh            # edit CAMS array at top if needed
```

Camera addresses can be changed by editing the `CAMS` array at the top of `install.sh` or by exporting the `PTZ_CAMS` environment variable before running the service, for example:

```bash
export PTZ_CAMS=tcp:192.168.10.44,udp:192.168.10.54
```

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
- **Packages and configuration:** `install.sh` installs `python3-pil`, `i2c-tools`, and `luma.oled` (via pip) and enables I2C via `raspi-config`. If you are setting up manually, install those packages and ensure your user is in the `i2c` group.
- **What you should see:**
  - Boot: “Parsing cameras…”, “Starting pygame…”, and “Waiting for joystick…” as setup progresses.
  - Runtime: “Joystick connected” with the controller name, “Bluetooth linked” (for wireless controllers), the active camera number/IP, and “PTZ bridge ready”.
  - Errors: configuration or socket issues render an “Error” banner with a brief code or message.
- **Disable the OLED:** Leave the display disconnected or uninstall `luma.oled`; the bridge will log “OLED display unavailable; running without screen” and operate normally with no OLED output.

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

- Change camera IPs/ports:

```bash
export PTZ_CAMS=tcp:192.168.10.44,udp:192.168.10.54
# format: proto:ip[:port] (defaults 5678 TCP, 1259 UDP)
```

- Adjust speed / dead-zone / zoom speed / zoom dead-zone: use the D-pad or RB/LB bumpers, or edit `MAX_SPEED`, `DEADZONE`, `MAX_ZOOM_SPEED` and `ZOOM_DEADZONE` in `~/ptzpad.py`.

## Service management

```bash
sudo systemctl status ptzpad
sudo systemctl restart ptzpad
sudo journalctl -u ptzpad -f   # live logs
```

The bridge handles `SIGTERM`/`SIGINT`, allowing `systemctl stop ptzpad` or `Ctrl+C` to terminate it quickly. The service is configured to restart automatically if the bridge crashes.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Service prints `Waiting for joystick connection…` | Check USB cable/port; `lsusb` should list the Xbox controller. |
| OLED stays blank or shows garbled text | Confirm the display answers at `0x3C` with `i2cdetect -y 1`, and recheck SDA (GPIO 2) / SCL (GPIO 3) wiring, 3.3 V power, and ground. |
| `Connection refused` | Wrong port or VISCA-TCP disabled in camera web UI. |
| Jerky / slow moves | Keep ≥40 ms between VISCA packets (`LOOP_MS`), use wired LAN. |
| Zoom jitter or stops while holding trigger | Tweak `ZOOM_START_DEADZONE`/`ZOOM_STOP_DEADZONE` to filter trigger noise and adjust `ZOOM_REPEAT_MS` for repeat rate. Zoom continues until the trigger rests inside the stop deadzone for a few loops. |
| Lag after 30 s idle | Some cameras drop idle TCP; the script sends periodic keep-alives – ensure they aren’t blocked by a firewall. |

## Where to go next

- Explore the VISCA protocol to add more camera features.
- Expand controller mapping to handle additional buttons or advanced behaviors.
- Learn more about `systemd` for tuning how the service runs and logs.

## Uninstall

```bash
sudo systemctl disable --now ptzpad
sudo rm /etc/systemd/system/ptzpad.service
```

Delete `~/ptzpad.py` if it's no longer needed.
