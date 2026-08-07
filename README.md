# XboxPTZControl

## LAN dashboard

The installer also enables `ptzpad-dashboard.service`, a dependency-free browser dashboard on port 8080. Open `http://<raspberry-pi-ip>:8080/` and enter the token from `~/.config/ptzpad/token` (mode 600). The dashboard shows bridge health, host load and uptime, camera reachability/address/model metadata, connected joystick devices, live tuning values, and searchable journal logs.

Camera and tuning settings are stored atomically in `~/.config/ptzpad/config.json`. The dashboard validates edits and ptzpad hot-reloads them, stopping motion on a replaced camera. Existing `PTZ_CAMS` remains supported as a fallback. Runtime state is published to `/run/ptzpad/status.json`; if permissions prevent that path, choose a user-writable `PTZPAD_STATE`.

The token protects every API, including status and logs. Keep port 8080 on a trusted LAN; this service does not provide TLS. Set `PTZPAD_BIND`, `PTZPAD_PORT`, `PTZPAD_TOKEN_FILE`, or `PTZPAD_STATE` in the dashboard unit to customize deployment. Rotate the token by deleting the token file and restarting `ptzpad-dashboard`.

If the dashboard reports stale/offline, check `systemctl status ptzpad-dashboard ptzpad` and `journalctl -u ptzpad.service`.

### Adding, testing, and discovering cameras

Use **Add camera** to create an editable camera row, then enter its name, model, address, VISCA protocol, and port. **Test** opens the configured endpoint and sends the read-only VISCA version inquiry; it never moves the camera. A successful connection is still reported when a camera does not implement the version inquiry.

**Discover cameras** suggests a subnet attached directly to the Raspberry Pi and scans the selected VISCA port. For safety, discovery accepts only directly attached RFC 1918 IPv4 networks with a `/24` or narrower prefix, scans at most 256 addresses with bounded concurrency, and has a cooldown. Manual camera tests have the same local-network restriction. Review the results and click **Add camera**, then **Save changes** to hot-reload the bridge configuration.
Turn any Raspberry Pi 3 B (or newer) into a headless VISCA-over-IP joystick server that lets an Xbox One / Series X|S controller drive one or many PTZOptics cameras.

## Repository structure

The main deliverable is a single installation script (`install.sh`) that:

- Installs Python 3, pip and `pygame`
- Writes the `ptzpad.py` controller bridge to the invoking user's home directory
- Creates and enables a `ptzpad.service` so the bridge starts on boot

The installer copies `ptzpad.py`, its `zoom_control.py` and `input_control.py` schedulers, and `oled_status.py` into the invoking user's home directory. The driver reads camera IP/port from environment variables, polls the controller with `pygame`, and sends VISCA-over-IP commands over TCP or UDP.

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
- Optional: Elgato Stream Deck with at least four visual keys (USB)

## Stream Deck controls (optional)

### Snapshot diagnostic

Run the standalone diagnostic to capture repeated camera snapshots without a Stream Deck or Xbox controller:

```bash
python3 ~/snapshot_diagnostic.py --camera-index 1 --count 5 --interval 2
```

Use `--camera 192.168.10.44` or `--output /tmp/ptz-check` to override selection/output. The tool saves numbered images, response headers, SHA-256 metadata, and an escaped `index.html` gallery. Change the camera scene during the run; duplicate hashes produce a WARN (capture failures return nonzero). It never starts a server; optionally inspect the gallery with `python3 -m http.server --directory <output>`.

On Raspberry Pi OS Bookworm, the installer prefers Debian's `python3-elgato-streamdeck` package and verifies `import StreamDeck` with the service interpreter. On older Bullseye images where that package is unavailable, it falls back to the `streamdeck` Python package via pip. It also installs `libhidapi-libusb0` and a scoped udev rule for Elgato's vendor ID (`0fd9`) granting the existing `input` group access. If installation/import fails, the bridge still starts without Stream Deck support.

The first detected visual deck is used. Target a model with at least four keys (such as Stream Deck Mini, standard, or XL). Three-key Pedal devices have no preset key or useful display and are not a supported target. The Standard 15-key deck reserves its left column for status:

| Keys | Action |
|---|---|
| 0 (top-left) | Selected camera index and name |
| 5 (middle-left) | Live pan/tilt and zoom speeds |
| 10 (bottom-left) | Camera address, white balance, and exposure mode |
| 1 / 2 / 3 | Previous camera / next camera / toggle **Save** mode |
| 4, 6–9, 11–14 | Presets 1–9 (normal press recalls; Save armed stores then disarms) |

Other supported deck sizes keep the legacy adaptive layout: keys 0, 1, and 2 are Previous, Next, and Save; keys 3 onward are presets.

The display shows the selected camera index/name and armed state. Presets use VISCA memory commands and are stored in the camera itself; available slot count and behavior are camera/model dependent. Troubleshoot with `journalctl -u ptzpad -f`, `lsusb`, and `id -nG` (the latter must include `input`).

The dashboard Stream Deck card reports package/driver availability, connection and key count, brightness, last event/render times, selected camera, Save arming, and the latest error. It also polls active TCP cameras at low rate for WB and AE mode; unsupported or UDP cameras are tolerated. On the Standard deck these values appear in the bottom-left status key. Enabled and brightness are saved in the nested `streamdeck` config object and hot-reload without restarting the service. Stream Deck input is independent of the Xbox controller: camera selection and presets remain available while the joystick is disconnected. If a connected deck remains on its factory logo, inspect the card and `journalctl -u ptzpad`; then recover with `sudo apt update`, `sudo apt install -y python3-elgato-streamdeck`, and `sudo systemctl restart ptzpad` (or rerun the installer), and replug the deck. The dashboard Library status should become available; also confirm `input` group membership and the udev rule.

Preset thumbnails intentionally perform two cache-busted snapshot requests, using the second settled frame.

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
| RT | Zoom in (both protocols start on direction changes; release sends 3 stops) |
| LT | Zoom out (both protocols start on direction changes; release sends 3 stops) |
| A | Cycle to next camera |
| D-pad up/down | Increase / decrease max speed |
| D-pad left/right | Increase / decrease deadzone |
| RB / LB | Increase / decrease zoom speed |

Zoom speed value `0` (including the dashboard setting) is the slowest zoom
speed; it does not disable zoom. Trigger depression ramps from slowest to the
configured maximum, and changing depression reissues the active command. Both
TCP and UDP cameras receive a start packet only when the zoom direction or
speed changes, followed by a bounded three-packet stop burst
when the trigger is released to tolerate packet loss.

## Customising after install

- Change camera names, models, IPs, ports, protocol, or tuning values in the dashboard. The bridge validates and hot-reloads `~/.config/ptzpad/config.json` without a restart.

- Change camera IPs/ports (one-off):

```bash
export PTZ_CAMS=tcp:192.168.10.44,udp:192.168.10.54
# format: proto:ip[:port] (defaults 5678 TCP, 1259 UDP)
```

- Defaults are a moderate pan/tilt max speed of 12 (range 1–24) and zoom speed 3 (range 0–7). Adjust speed / dead-zone / zoom speed with the D-pad or RB/LB bumpers, through the dashboard, or in `~/.config/ptzpad/config.json`.

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
| Zoom jitter or stops while holding trigger | Tweak `ZOOM_START_DEADZONE` to filter trigger noise. Both TCP and UDP send starts only on direction changes, then issue three stop packets when released. A dashboard zoom speed of `0` is slowest, not disabled. |
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
rm -f ~/ptzpad.py ~/streamdeck_control.py ~/zoom_control.py ~/input_control.py ~/ptz_dashboard.py ~/ptz_config.py ~/oled_status.py
sudo rm -f /etc/udev/rules.d/99-ptzpad-streamdeck.rules
# Optional: remove saved configuration and the dashboard token.
rm -rf ~/.config/ptzpad
```
The Cameras card supports VISCA version testing and private-LAN discovery. Discovery accepts only private or link-local IPv4 `/24` networks (up to 256 addresses), uses bounded concurrent probes, and never sends motion commands. Review results before adding them to configuration. Keep access restricted to a trusted LAN; all API mutations require the per-install token and same-origin requests.
