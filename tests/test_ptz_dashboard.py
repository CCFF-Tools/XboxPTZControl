import importlib
import json
import os
import tempfile
import unittest
from http.client import HTTPConnection
from threading import Thread


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
        c = HTTPConnection(*self.server.server_address); c.request("GET", path, headers=headers); return c.getresponse()

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


if __name__ == "__main__":
    unittest.main()
