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

    def test_tuning_defaults_and_bounds(self):
        cfg = validate_config({"cameras": [{"host": "cam"}]})
        self.assertEqual((cfg["max_speed"], cfg["deadzone"], cfg["zoom_speed"]), (12, 0.15, 3))
        self.assertEqual(cfg["controls"], {"y_button_zoom_speed_up": False})
        explicit = validate_config({"cameras": [{"host": "cam"}], "max_speed": 24, "zoom_speed": 7})
        self.assertEqual((explicit["max_speed"], explicit["zoom_speed"]), (24, 7))

    def test_y_button_zoom_toggle(self):
        cfg = validate_config({"cameras": [{"host": "cam"}], "controls": {"y_button_zoom_speed_up": True}})
        self.assertTrue(cfg["controls"]["y_button_zoom_speed_up"])

        with self.assertRaises(ValueError):
            validate_config({"cameras": [{"host": "cam"}], "controls": None})
        with self.assertRaises(ValueError):
            validate_config({"cameras": [{"host": "cam"}], "controls": {"y_button_zoom_speed_up": 1}})

    def test_y_button_zoom_toggle_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            save_config({"cameras": [{"host": "cam"}], "controls": {"y_button_zoom_speed_up": True}}, path)
            self.assertTrue(load_config({"PTZPAD_CONFIG": str(path)})["controls"]["y_button_zoom_speed_up"])

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

    def test_streamdeck_defaults_and_validation(self):
        config = validate_config({"cameras": [{"host": "cam"}]})
        self.assertEqual(config["streamdeck"], {"enabled": True, "brightness": 35})
        with self.assertRaises(ValueError):
            validate_config({"cameras": [{"host": "cam"}], "streamdeck": {"brightness": 101}})
        with self.assertRaises(ValueError):
            validate_config({"cameras": [{"host": "cam"}], "streamdeck": None})
        with self.assertRaises(ValueError):
            validate_config({"cameras": [{"host": "cam"}], "streamdeck": {"brightness": True}})

    def test_streamdeck_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            save_config({"cameras": [{"host": "cam"}], "streamdeck": {"enabled": False, "brightness": 12}}, path)
            self.assertEqual(load_config({"PTZPAD_CONFIG": str(path)})["streamdeck"], {"enabled": False, "brightness": 12})


if __name__ == "__main__":
    unittest.main()
