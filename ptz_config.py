#!/usr/bin/env python3
"""Shared camera configuration loading and atomic persistence."""
import json
import os
import tempfile
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("PTZPAD_CONFIG", "~/.config/ptzpad/config.json")).expanduser()


def _camera(value):
    if not isinstance(value, dict):
        raise ValueError("camera must be an object")
    host = str(value.get("host", "")).strip()
    proto = str(value.get("protocol", value.get("proto", "tcp"))).lower()
    port = value.get("port", 5678 if proto == "tcp" else 1259)
    if not host or len(host) > 253 or proto not in ("tcp", "udp"):
        raise ValueError("invalid camera host or protocol")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid camera port")
    return {"host": host, "protocol": proto, "port": port,
            "name": str(value.get("name", host))[:80], "model": str(value.get("model", ""))[:80]}


def validate_camera(value):
    """Validate and normalize one camera entry."""
    return _camera(value)


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    cams = value.get("cameras", [])
    if not isinstance(cams, list) or not cams or len(cams) > 64:
        raise ValueError("cameras must be a non-empty list")
    out = {"cameras": [_camera(c) for c in cams]}
    deck = value.get("streamdeck", {})
    if not isinstance(deck, dict):
        raise ValueError("streamdeck must be an object")
    enabled = deck.get("enabled", True)
    brightness = deck.get("brightness", 35)
    if not isinstance(enabled, bool):
        raise ValueError("invalid streamdeck enabled")
    if not isinstance(brightness, int) or isinstance(brightness, bool) or not 0 <= brightness <= 100:
        raise ValueError("invalid streamdeck brightness")
    out["streamdeck"] = {"enabled": enabled, "brightness": brightness}
    for key, default in (("max_speed", 24), ("deadzone", 0.15), ("zoom_speed", 7)):
        if key in value:
            out[key] = value[key]
        else:
            out[key] = default
    if not isinstance(out["max_speed"], int) or not 1 <= out["max_speed"] <= 24:
        raise ValueError("invalid max_speed")
    if not isinstance(out["zoom_speed"], int) or not 0 <= out["zoom_speed"] <= 7:
        raise ValueError("invalid zoom_speed")
    if not isinstance(out["deadzone"], (int, float)) or not 0 <= out["deadzone"] <= .5:
        raise ValueError("invalid deadzone")
    return out


def load_config(env=None):
    env = os.environ if env is None else env
    path = Path(env.get("PTZPAD_CONFIG", str(config_path()))).expanduser()
    try:
        with path.open(encoding="utf-8") as fh:
            return validate_config(json.load(fh))
    except (OSError, ValueError, json.JSONDecodeError):
        raw = env.get("PTZ_CAMS", "tcp:192.168.1.150")
        cams = []
        for item in raw.split(","):
            p = item.strip().split(":")
            proto = p[0].lower() if p and p[0].lower() in ("tcp", "udp") else "tcp"
            if proto != "tcp": p = p[1:]
            elif p and p[0].lower() == "tcp": p = p[1:]
            if not p or not p[0]: continue
            try: port = int(p[1]) if len(p) > 1 else (5678 if proto == "tcp" else 1259)
            except ValueError: continue
            try: cams.append(_camera({"host": p[0], "protocol": proto, "port": port}))
            except ValueError: pass
        return validate_config({"cameras": cams or [{"host": "192.168.1.150", "protocol": "tcp", "port": 5678}]})


def save_config(value, path=None):
    path = Path(path or config_path()).expanduser()
    value = validate_config(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
        os.chmod(tmp, 0o600); os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
