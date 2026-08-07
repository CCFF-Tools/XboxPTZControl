import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import snapshot_diagnostic


class Response:
    headers = {"Content-Type": "image/jpeg"}

    def __init__(self, data):
        self.data = data

    def read(self, _):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, data):
        self.data = data
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return Response(self.data)


class SnapshotDiagnosticTests(unittest.TestCase):
    def test_run_writes_gallery_and_warns_duplicate(self):
        data = b"\xff\xd8" + b"snapshot-data" * 2 + b"\xff\xd9"
        opener = Opener(data)
        with tempfile.TemporaryDirectory() as root:
            duplicate = snapshot_diagnostic.run("camera", 2, 0, Path(root), opener=opener, sleeper=lambda _: None)
            self.assertTrue(duplicate)
            self.assertTrue((Path(root) / "frame-001.jpg").exists())
            self.assertTrue((Path(root) / "index.html").exists())
        self.assertNotEqual(opener.requests[0].full_url, opener.requests[1].full_url)
        self.assertEqual(opener.requests[0].headers["Cache-control"], "no-cache, no-store, max-age=0")
        self.assertEqual(opener.requests[0].headers["Pragma"], "no-cache")
        self.assertIn("ptzpad_ts=", opener.requests[0].full_url)

    def test_capture_uses_fake_response(self):
        opener = Opener(b"\xff\xd8" + b"x" * 20 + b"\xff\xd9")
        data, _ = snapshot_diagnostic.capture_frame("camera", opener=opener)
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_main_camera_selection_and_override(self):
        config = {"cameras": [{"host": "first"}, {"host": "second"}]}
        with patch("snapshot_diagnostic.load_config", return_value=config), patch("snapshot_diagnostic.run") as capture:
            self.assertEqual(snapshot_diagnostic.main(["--camera-index", "2", "--count", "3", "--interval", "1", "--output", "/tmp/out"]), 0)
            capture.assert_called_once_with("second", 3, 1.0, Path("/tmp/out"))
            capture.reset_mock()
            self.assertEqual(snapshot_diagnostic.main(["--camera", "override"]), 0)
            self.assertEqual(capture.call_args.args[0], "override")

    def test_override_skips_config_and_default_output_is_unique(self):
        with patch("snapshot_diagnostic.load_config", side_effect=RuntimeError("no config")), patch("snapshot_diagnostic.run") as capture, patch("snapshot_diagnostic.tempfile.mkdtemp", return_value="/tmp/ptz-snapshot-unique"):
            self.assertEqual(snapshot_diagnostic.main(["--camera", "override"]), 0)
            self.assertEqual(capture.call_args.args[3], Path("/tmp/ptz-snapshot-unique"))


if __name__ == "__main__": unittest.main()
