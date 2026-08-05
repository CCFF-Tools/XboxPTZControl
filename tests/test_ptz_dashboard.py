import importlib
import json
import os
import tempfile
import unittest
from http.client import HTTPConnection
from threading import Thread
from unittest.mock import patch


class DashboardHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["PTZPAD_TOKEN_FILE"] = os.path.join(cls.tmp.name, "token")
        os.environ["PTZPAD_CONFIG"] = os.path.join(cls.tmp.name, "config.json")
        os.environ["PTZPAD_STATE"] = os.path.join(cls.tmp.name, "state.json")
        cls.mod = importlib.import_module("ptz_dashboard")
        cls.mod.save_config({"cameras": [{"host": "127.0.0.1", "protocol": "tcp", "port": 1}]})
        cls.server = cls.mod.ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.tmp.cleanup()

    def request(self, path, **headers):
        connection = HTTPConnection(*self.server.server_address)
        connection.request("GET", path, headers=headers)
        return connection.getresponse()

    def post(self, path, payload, **headers):
        connection = HTTPConnection(*self.server.server_address)
        headers.setdefault("Authorization", "Bearer " + self.mod.TOKEN)
        headers.setdefault("Content-Type", "application/json")
        connection.request("POST", path, body=json.dumps(payload), headers=headers)
        return connection.getresponse()

    def test_api_requires_constant_time_token(self):
        self.assertEqual(self.request("/api/health").status, 401)
        self.assertEqual(self.request("/api/health", Authorization="Bearer " + self.mod.TOKEN).status, 200)

    def test_log_filters_are_bounded(self):
        response = self.request("/api/logs?lines=not-a-number&search=%27%3B%20rm%20-rf", Authorization="Bearer " + self.mod.TOKEN)
        self.assertEqual(response.status, 200)

    def test_config_put_requires_json_content_type(self):
        c = HTTPConnection(*self.server.server_address)
        c.request("PUT", "/api/config", body=json.dumps({"cameras": []}), headers={"Authorization": "Bearer " + self.mod.TOKEN, "Content-Type": "text/plain"})
        self.assertEqual(c.getresponse().status, 415)

    def test_camera_test_endpoint_uses_probe(self):
        result = {"reachable": True, "latency_ms": 12.5, "model_id": "1234"}
        with patch.object(self.mod, "test_camera", return_value=result) as camera_test:
            response = self.post(
                "/api/cameras/test",
                {"host": "192.168.1.20", "protocol": "tcp", "port": 5678},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["model_id"], "1234")
            camera_test.assert_called_once()

    def test_discovery_endpoint_uses_bounded_scanner(self):
        found = [{"host": "192.168.1.20", "protocol": "tcp", "port": 5678}]
        with patch.object(self.mod, "discover_network", return_value=found):
            response = self.post(
                "/api/cameras/discover",
                {"subnet": "192.168.1.0/24", "protocol": "tcp", "port": 5678},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response)["results"], found)


class DashboardHelperTests(unittest.TestCase):
    def test_public_or_overly_broad_discovery_is_rejected(self):
        import ptz_dashboard

        with self.assertRaises(ValueError):
            ptz_dashboard.discover_network("8.8.8.0/24", "tcp", 5678)
        with self.assertRaises(ValueError):
            ptz_dashboard.discover_network("192.168.0.0/16", "tcp", 5678)

    def test_discovery_and_testing_require_an_attached_network(self):
        import ipaddress
        import ptz_dashboard

        attached = [ipaddress.ip_network("192.168.10.0/24")]
        accepted = ptz_dashboard.validate_discovery_subnet(
            "192.168.10.0/24", networks=attached
        )
        self.assertEqual(str(accepted), "192.168.10.0/24")
        with self.assertRaises(ValueError):
            ptz_dashboard.validate_discovery_subnet(
                "10.0.0.0/24", networks=attached
            )
        with self.assertRaises(ValueError):
            ptz_dashboard.require_local_camera(
                {"host": "127.0.0.1"}, networks=attached
            )

    def test_visca_version_response_is_parsed(self):
        import ptz_dashboard

        response = bytes.fromhex("90 50 00 01 12 34 00 02 00 01 ff")
        self.assertEqual(ptz_dashboard.parse_visca_version(response)["model_id"], "1234")

    def test_html_uses_structured_safe_camera_controls(self):
        import ptz_dashboard

        self.assertIn("cameraFromRow", ptz_dashboard.HTML)
        self.assertIn("Add camera", ptz_dashboard.HTML)
        self.assertIn("Discover cameras", ptz_dashboard.HTML)
        self.assertNotIn("innerHTML", ptz_dashboard.HTML)

    def test_html_contains_streamdeck_status_and_controls(self):
        import ptz_dashboard

        self.assertIn("deckBrightness", ptz_dashboard.HTML)
        self.assertIn("deckEnabled", ptz_dashboard.HTML)
        self.assertIn("last_render_at", ptz_dashboard.HTML)
        self.assertIn("editGeneration", ptz_dashboard.HTML)
        self.assertIn("markDirty", ptz_dashboard.HTML)
        self.assertIn("generation===editGeneration", ptz_dashboard.HTML)


if __name__ == "__main__":
    unittest.main()
