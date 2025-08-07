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

## Default controls

| Input | Action |
|-------|--------|
| Right stick | Pan / tilt (speed scales logarithmically with stick deflection) |
| Left stick up/down | Focus far/near (medium deadzone) |
| Left stick click | One-time autofocus |
| RT | Zoom in |
| LT | Zoom out |
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

- Adjust speed / dead-zone / zoom speed: use the D-pad or RB/LB bumpers, or edit `MAX_SPEED`, `DEADZONE` and `MAX_ZOOM_SPEED` in `~/ptzpad.py`.

## Service management

```bash
sudo systemctl status ptzpad
sudo systemctl restart ptzpad
sudo journalctl -u ptzpad -f   # live logs
```

The bridge handles `SIGTERM`/`SIGINT`, allowing `systemctl stop ptzpad` or `Ctrl+C` to terminate it quickly.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `pygame.error: No joystick` | Check USB cable/port; `lsusb` should list the Xbox controller. |
| `Connection refused` | Wrong port or VISCA-TCP disabled in camera web UI. |
| Jerky / slow moves | Keep ≥40 ms between VISCA packets (`LOOP_MS`), use wired LAN. |
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
