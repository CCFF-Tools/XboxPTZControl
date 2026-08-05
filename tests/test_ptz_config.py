import json
import tempfile
import unittest
from pathlib import Path

from ptz_config import load_config, save_config, validate_config


class ConfigTests(unittest.TestCase):
    def test_validation_rejects_bad_port(self):
        with self.assertRaises(ValueError):
            validate_config({"cameras": [{"host": "cam", "protocol": "tcp", "port": 0}]})

    def test_atomic_save_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            value = {"cameras": [{"host": "10.0.0.2", "protocol": "udp", "port": 1259}]}
            save_config(value, path)
            self.assertEqual(load_config({"PTZPAD_CONFIG": str(path)})["cameras"][0]["host"], "10.0.0.2")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_legacy_environment_fallback(self):
        cfg = load_config({"PTZPAD_CONFIG": "/missing/config", "PTZ_CAMS": "tcp:cam.example:5678"})
        self.assertEqual(cfg["cameras"][0]["protocol"], "tcp")

    def test_camera_model_metadata_is_preserved(self):
        config = validate_config(
            {
                "cameras": [
                    {
                        "host": "192.168.1.20",
                        "protocol": "tcp",
                        "port": 5678,
                        "name": "Stage left",
                        "model": "Move 4K",
                    }
                ]
            }
        )
        self.assertEqual(config["cameras"][0]["model"], "Move 4K")


if __name__ == "__main__":
    unittest.main()
