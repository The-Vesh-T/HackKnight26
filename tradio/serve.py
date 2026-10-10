"""Tradio local server: the website plus Start/Stop buttons for the LIVE tab.

    python tradio/serve.py                       # http://localhost:8000/tradio.html
    TRADIO_ALLOW_LIVE=1 python tradio/serve.py   # also allow the real-money button

Replaces `cd web && python3 -m http.server 8000`. Same static files, plus a tiny API the
LIVE tab uses to start and stop Tradio's own commands, so nobody has to juggle terminals:

  practice  python tradio/live.py --simulate <event> --speed <n>          (offline)
  live      python speechtxt.py --url <stream>                           (transcribe, optional)
            python live.py --speaker <s> --fast --playbook --mode <m> --venues kalshi  (desk)
            python tradio/live.py --speaker <s> --t0 now                 (market tape)

Safety:
  - listens on 127.0.0.1 only; requests must carry the X-Tradio header from this page
    (a cross-site page can't send it without a CORS preflight, which this server refuses);
  - runs only the fixed commands above; every argument is checked against a whitelist
    (events on disk, speakers with a baseline, numeric speed, https stream URL);
  - mode "dry" sends nothing; "demo" needs kalshikey_demo.txt + privkey_demo.txt (fake money);
    "live" (real money) needs ALL of: this server started with TRADIO_ALLOW_LIVE=1, the real
    Kalshi key files, and the word LIVE typed on the page. The desk's own limits still
    apply ($10 per order, $50 per day, trading_common.py).
"""

import argparse
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(ROOT, "tradio", "data")
FED_LIVE_PAGE = "https://www.federalreserve.gov/live-broadcast.htm"
MODES = ("dry", "demo", "live")
SPEEDS = (1, 10, 30, 60, 120)
LOG_LINES = 60


def has(*names) -> bool:
    return all(os.path.isfile(os.path.join(ROOT, n)) and os.path.getsize(os.path.join(ROOT, n)) > 0 for n in names)


def speakers() -> list:
    try:
        with open(os.path.join(ROOT, "stance_baselines.json"), encoding="utf-8") as f:
            return sorted(json.load(f))
    except (OSError, ValueError):
        return []


def events() -> list:
    try:
        return sorted(e for e in os.listdir(DATA)
                      if e != "live" and os.path.isfile(os.path.join(DATA, e, "timeline.json")))
    except OSError:
        return []


def _child_setup():
    """Own process group (so Stop reaches the whole command), and Ctrl+C behaviour restored:
    a server started in the background inherits 'ignore SIGINT', which would make Stop wait."""
    os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class Proc:
    def __init__(self, name: str, label: str, argv: list, env: dict):
        self.name, self.label, self.argv = name, label, argv
        self.log = collections.deque(maxlen=LOG_LINES)
        self.started = time.time()
        self.p = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, preexec_fn=_child_setup)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.log.append(line.rstrip()[:300])

    def running(self) -> bool:
        return self.p.poll() is None

    def stop(self):
        if self.running():
            try:
                os.killpg(self.p.pid, signal.SIGINT)      # live.py / tradio/live.py finish cleanly on Ctrl+C
                self.p.wait(timeout=8)
            except Exception:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except Exception:
                    pass

    def status(self) -> dict:
        return {"label": self.label, "running": self.running(), "exit_code": self.p.poll(),
                "started": self.started, "log": list(self.log)[-12:]}


class Runner:
    def __init__(self, allow_live: bool):
        self.allow_live = allow_live
        self.procs = {}
        self.session = None
        self.lock = threading.Lock()

    def stop_all(self):
        with self.lock:
            for p in self.procs.values():
                p.stop()
            self.procs = {}
            self.session = None

    def _spawn(self, name, label, argv, env=None):
        self.procs[name] = Proc(name, label, argv, env or dict(os.environ, PYTHONUNBUFFERED="1"))

    def start(self, req: dict) -> dict:
        kind = req.get("kind")
        py = sys.executable
        if kind == "practice":
            ev, speed = req.get("event"), req.get("speed", 30)
            if ev not in events():
                raise ValueError("unknown event")
            if speed not in SPEEDS:
                raise ValueError(f"speed must be one of {SPEEDS}")
            self.stop_all()
            with self.lock:
                self._spawn("tape", f"practice replay of {ev} at {speed}x",
                            [py, "tradio/live.py", "--simulate", ev, "--speed", str(speed)])
                self.session = {"kind": "practice", "event": ev, "speed": speed}
            return self.status()

        if kind == "live":
            sp, mode = req.get("speaker"), req.get("mode", "dry")
            if sp not in speakers():
                raise ValueError("unknown speaker (no stance baseline)")
            if mode not in MODES:
                raise ValueError("mode must be dry, demo or live")
            if mode == "demo" and not has("kalshikey_demo.txt", "privkey_demo.txt"):
                raise ValueError("demo trading needs kalshikey_demo.txt and privkey_demo.txt (from demo.kalshi.co)")
            if mode == "live":
                if not self.allow_live:
                    raise ValueError("real money is off: restart the server with TRADIO_ALLOW_LIVE=1")
                if req.get("confirm") != "LIVE":
                    raise ValueError("type LIVE to confirm real-money trading")
                if not has("kalshikey.txt", "privkey.txt"):
                    raise ValueError("real trading needs kalshikey.txt and privkey.txt (kalshi.com API key)")
            transcribe = bool(req.get("transcribe", True))
            url = req.get("url") or FED_LIVE_PAGE
            if transcribe:
                if not re.fullmatch(r"https://[\w.\-]+(/[\w.\-~/%?=&+#:]*)?", url):
                    raise ValueError("stream URL must be a plain https:// link")
                if not has("elevenapi.txt"):
                    raise ValueError("transcribing needs elevenapi.txt (ElevenLabs key)")
            if not has("gemapi.txt"):
                raise ValueError("scoring needs gemapi.txt (Gemini key)")
            self.stop_all()
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            with self.lock:
                if transcribe:
                    self._spawn("transcriber", f"transcribing {url}", [py, "speechtxt.py", "--url", url], env)
                self._spawn("desk", f"scoring {sp}" + {"dry": ", guide only (no orders)",
                                                       "demo": ", trading on Kalshi DEMO (fake money)",
                                                       "live": ", trading REAL MONEY"}[mode],
                            [py, "live.py", "--speaker", sp, "--fast", "--playbook", "--mode", mode,
                             "--venues", "kalshi"], env)
                self._spawn("tape", "live Kalshi tape + reaction checks",
                            [py, "tradio/live.py", "--speaker", sp, "--t0", "now"], env)
                self.session = {"kind": "live", "speaker": sp, "mode": mode, "transcribe": transcribe, "url": url}
            return self.status()
        raise ValueError("kind must be practice or live")

    def status(self) -> dict:
        with self.lock:
            procs = {n: p.status() for n, p in self.procs.items()}
        return {
            "server": "tradio", "allow_live": self.allow_live, "session": self.session, "processes": procs,
            "keys": {"gemini": has("gemapi.txt"), "elevenlabs": has("elevenapi.txt"),
                     "kalshi_demo": has("kalshikey_demo.txt", "privkey_demo.txt"),
                     "kalshi_live": has("kalshikey.txt", "privkey.txt")},
            "speakers": speakers(), "events": events(), "speeds": list(SPEEDS), "fed_live_page": FED_LIVE_PAGE,
        }


def make_handler(runner: Runner):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=WEB, **k)

        def log_message(self, fmt, *args):            # keep the terminal quiet except for the API
            if self.path.startswith("/api/"):
                super().log_message(fmt, *args)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _trusted(self) -> bool:
            if self.headers.get("X-Tradio") != "1":
                return False
            origin = self.headers.get("Origin")
            return origin is None or urlparse(origin).hostname in ("localhost", "127.0.0.1")

        def do_GET(self):
            if self.path == "/api/status":
                return self._json(200, runner.status()) if self._trusted() else self._json(403, {"error": "forbidden"})
            return super().do_GET()

        def do_POST(self):
            if not self._trusted():
                return self._json(403, {"error": "forbidden"})
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 10000)
                req = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/start":
                    return self._json(200, runner.start(req))
                if self.path == "/api/stop":
                    runner.stop_all()
                    return self._json(200, runner.status())
                return self._json(404, {"error": "not found"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})

        def do_OPTIONS(self):                         # no CORS: cross-site pages can't call the API
            self.send_response(403)
            self.end_headers()

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tradio website + LIVE tab controls (localhost only)")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    runner = Runner(allow_live=os.environ.get("TRADIO_ALLOW_LIVE") == "1")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(runner))
    print(f"Tradio: http://localhost:{a.port}/tradio.html   (LIVE tab has Start/Stop buttons)")
    print("real-money button: " + ("ENABLED (TRADIO_ALLOW_LIVE=1)" if runner.allow_live else "off"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop_all()
        srv.server_close()


if __name__ == "__main__":
    main()
