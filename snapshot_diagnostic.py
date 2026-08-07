#!/usr/bin/env python3
"""Capture repeated PTZOptics snapshots without touching the bridge process."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ptz_config import load_config
from streamdeck_control import validate_snapshot


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect rejected")


def capture_frame(host, timeout=1.5, opener=None):
    request = Request(
        f"http://{host}/snapshot.jpg?ptzpad_ts={time.time_ns()}",
        headers={"Accept": "image/jpeg,image/png", "Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"},
    )
    client = opener or build_opener(_NoRedirect)
    with client.open(request, timeout=timeout) as response:
        data = validate_snapshot(response.read(2 * 1024 * 1024 + 1), response.headers.get("Content-Type", ""))
        return data, dict(response.headers.items())


def run(host, count, interval, output, opener=None, sleeper=time.sleep):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    hashes = []
    records = []
    for index in range(1, count + 1):
        data, headers = capture_frame(host, opener=opener)
        digest = hashlib.sha256(data).hexdigest()
        path = output / f"frame-{index:03d}.jpg"
        path.write_bytes(data)
        (output / f"frame-{index:03d}.headers.json").write_text(
            json.dumps(headers, indent=2) + "\n", encoding="utf-8"
        )
        hashes.append(digest)
        records.append({"frame": index, "file": path.name, "sha256": digest, "headers": headers})
        print(f"frame {index}/{count}: {path} sha256={digest[:16]}")
        if index < count:
            sleeper(interval)
    (output / "index.html").write_text(_gallery(records), encoding="utf-8")
    duplicate = len(set(hashes)) < len(hashes)
    print("WARN: duplicate snapshot hashes detected" if duplicate else "PASS: all snapshot hashes differ")
    return duplicate


def _gallery(records):
    cards = []
    for record in records:
        label = html.escape(f"Frame {record['frame']} — {record['sha256']}")
        cards.append(f'<figure><img src="{html.escape(record["file"])}" alt="{label}"><figcaption>{label}</figcaption></figure>')
    return "<!doctype html><meta charset='utf-8'><title>PTZ snapshots</title><p>Change the camera scene during capture, then compare frames.</p>" + "".join(cards)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera")
    parser.add_argument("--camera-index", type=int, default=1)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 2 <= args.count <= 100 or not 0 <= args.interval <= 60:
        parser.error("count must be 2..100 and interval 0..60 seconds")
    if args.camera:
        host = args.camera
    else:
        cameras = load_config()["cameras"]
        if not 1 <= args.camera_index <= len(cameras):
            parser.error("camera index is out of range")
        host = cameras[args.camera_index - 1]["host"]
    output = args.output or Path(tempfile.mkdtemp(prefix="ptz-snapshot-"))
    try:
        run(host, args.count, args.interval, output)
    except Exception as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1
    print(f"Gallery: {output / 'index.html'}")
    print(f"Serve with: python3 -m http.server --directory {shlex.quote(str(output))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
