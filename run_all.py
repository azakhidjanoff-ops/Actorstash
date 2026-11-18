import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).parent
TOKEN_ENV = {"bot.py": "BOT_TOKEN", "promo_bot.py": "PROMO_BOT_TOKEN"}
SCRIPTS = sys.argv[1:] or ["bot.py", "promo_bot.py"]


class Health(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(b"ok")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(body=False)

    def log_message(self, *args):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


def keep_running(script):
    env_name = TOKEN_ENV.get(script)
    if env_name and not os.environ.get(env_name):
        print(f"[{script}] {env_name} berilmagan, o'tkazib yuborildi", flush=True)
        return
    while True:
        code = subprocess.run([sys.executable, str(BASE / script)]).returncode
        print(f"[{script}] to'xtadi (kod {code}), 5 soniyadan keyin qayta yoqiladi", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    threads = [threading.Thread(target=serve, daemon=True)]
    threads += [threading.Thread(target=keep_running, args=(s,), daemon=True) for s in SCRIPTS]
    for t in threads:
        t.start()
    while threads[0].is_alive():
        time.sleep(1)
