# XboxPTZControl

Turn any Raspberry Pi 3 B (or newer) into a headless VISCA-over-IP joystick server that lets an Xbox One / Series X|S controller drive one or many PTZOptics cameras.

## Repository structure

The main deliverable is a single installation script (`install.sh`) that:

- Installs Python 3, pip and `pygame`
- Writes the `ptzpad.py` controller bridge to `/home/pi`
- Creates and enables a `ptzpad.service` so the bridge starts on boot

The Python driver is embedded within the script. It reads camera IP/port from environment variables, polls the controller with `pygame`, and sends VISCA-over-IP commands over TCP.

## Quick start

```bash
git clone https://github.com/CCFF-Tools/XboxPTZControl.git
cd XboxPTZControl
sudo bash install.sh            # edit CAMS array at top if needed
```

Hardware you need:

- Raspberry Pi 3 B or newer running Raspberry Pi OS (32-bit, bullseye or bookworm)
- Xbox One / Series X|S controller (wired USB recommended)
- PTZOptics camera(s) with VISCA-over-IP enabled (default TCP 5678)

## Default controls

| Input | Action |
|-------|--------|
| Left stick | Pan / tilt (variable speed) |
| RT | Zoom in |
| LT | Zoom out |
| LB | Cycle to next camera |

## Customising after install

- Change camera IPs/ports:

```bash
export PTZ_CAMS=192.168.10.100,192.168.10.101
export PTZ_PORT=5678
```

- Adjust speed / dead-zone: edit `MAX_SPEED` and `DEADZONE` constants in `/home/pi/ptzpad.py`.

## Service management

```bash
sudo systemctl status ptzpad
sudo systemctl restart ptzpad
sudo journalctl -u ptzpad -f   # live logs
```

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

