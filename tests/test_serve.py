"""tradio/serve.py: the LIVE tab's Start/Stop API refuses anything outside its whitelist.

    python -m unittest tests.test_serve
"""

import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio import serve  # noqa: E402


class RunnerValidationTest(unittest.TestCase):
    def setUp(self):
        self.r = serve.Runner(allow_live=False)
        self.spawned = []
        self.r._spawn = lambda name, label, argv, env=None: self.spawned.append(argv)

    def err(self, req):
        with self.assertRaises(ValueError) as cm:
            self.r.start(req)
        self.assertEqual(self.spawned, [])
        return str(cm.exception)

    def test_rejects_unknown_kind_event_speed_and_speaker(self):
        self.err({"kind": "shell", "cmd": "rm -rf /"})
        self.assertIn("unknown event", self.err({"kind": "practice", "event": "../../etc", "speed": 30}))
        self.assertIn("speed", self.err({"kind": "practice", "event": "bowman20250623", "speed": 5}))
        self.assertIn("speaker", self.err({"kind": "live", "speaker": "x; ls", "mode": "dry"}))

    def test_real_money_needs_server_opt_in_then_confirm_then_keys(self):
        req = {"kind": "live", "speaker": "michelle_bowman", "mode": "live", "confirm": "LIVE"}
        self.assertIn("TRADIO_ALLOW_LIVE", self.err(req))
        self.r.allow_live = True
        self.assertIn("type LIVE", self.err(dict(req, confirm="live")))
        with mock.patch.object(serve, "has", side_effect=lambda *n: "kalshikey.txt" not in n):
            self.assertIn("kalshikey.txt", self.err(req))

    def test_stream_url_must_be_plain_https(self):
        with mock.patch.object(serve, "has", return_value=True):
            for bad in ("file:///etc/passwd", "http://x.com", "https://x.com/ --exec rm", "-o /tmp/x"):
                self.assertIn("https", self.err({"kind": "live", "speaker": "michelle_bowman", "mode": "dry", "url": bad}))

    def test_archive_checks_name_and_title(self):
        self.assertIn("name", self.err({"kind": "archive", "event_id": "../x"}))
        self.assertIn("already exists", self.err({"kind": "archive", "event_id": "bowman20250623"}))
        self.assertIn("title", self.err({"kind": "archive", "event_id": "waller20991015", "title": "x; rm -rf /"}))
        self.r.start({"kind": "archive", "event_id": "waller20991015", "title": "Waller, 2099-10-15"})
        self.assertEqual(self.spawned[0][1:], ["tradio/live.py", "archive", "waller20991015", "--title", "Waller, 2099-10-15"])

    def test_insights_runs_only_the_snowflake_query(self):
        with mock.patch.object(serve, "has", return_value=False):
            self.assertIn("Snowflake", self.err({"kind": "insights"}))
        with mock.patch.object(serve, "has", return_value=True):
            self.r.start({"kind": "insights", "sql": "DROP TABLE x"})
        self.assertEqual(self.spawned[0][1:], ["-m", "tradio.warehouse", "snowflake-insights"])

    def test_practice_runs_only_the_simulator(self):
        self.r.start({"kind": "practice", "event": "bowman20250623", "speed": 30})
        self.assertEqual(self.spawned[0][1:], ["tradio/live.py", "--simulate", "bowman20250623", "--speed", "30"])

    def test_guide_mode_starts_transcriber_desk_and_tape(self):
        with mock.patch.object(serve, "has", return_value=True):
            self.r.start({"kind": "live", "speaker": "michelle_bowman", "mode": "dry"})
        scripts = [a[1] for a in self.spawned]
        self.assertEqual(scripts, ["speechtxt.py", "live.py", "tradio/live.py"])
        desk = self.spawned[1]
        self.assertEqual(desk[desk.index("--mode") + 1], "dry")
        self.assertIn("--playbook", desk)


class ApiOriginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(serve.Runner(False)))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, headers, body=None):
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_api_needs_the_page_header_and_a_local_origin(self):
        self.assertEqual(self.call("/api/status", {})[0], 403)
        self.assertEqual(self.call("/api/status", {"X-Tradio": "1"})[0], 200)
        self.assertEqual(self.call("/api/stop", {"X-Tradio": "1", "Origin": "https://evil.example"}, b"{}")[0], 403)
        self.assertEqual(self.call("/api/stop", {"X-Tradio": "1", "Origin": "http://localhost:8000"}, b"{}")[0], 200)

    def test_still_serves_the_website(self):
        with urllib.request.urlopen(self.base + "/tradio.html") as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
