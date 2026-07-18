#!/usr/bin/env python3
"""Home Network Monitor - tiny LAN web server for the dashboard.

Lets everyone on your home network open the dashboard from any device:

    http://<this-machine's-ip>:8080/

It serves ONLY dashboard.html and the two vendored chart libraries.
Nothing else in this folder (database, logs, config files) is reachable.
Stdlib only; runs on Windows, macOS, and Linux. On Windows, setup.ps1
registers it as the "NetMon Web" scheduled task and opens TCP 8080 on
the Private firewall profile. Change the port with the NETMON_WEB_PORT
environment variable if 8080 is taken.
"""
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from version import __version__
except ImportError:  # partially-copied install
    __version__ = "0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("NETMON_WEB_PORT", "8080"))

# Under pythonw.exe (no console) stdout/stderr are None - send them to logs/
# like monitor.py does, so crashes are visible somewhere.
if sys.stdout is None or sys.stderr is None:
    _log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(os.path.join(_log_dir, "web.out.log"), "a", buffering=1, encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.path.join(_log_dir, "web.err.log"), "a", buffering=1, encoding="utf-8")

# Whitelist: URL path -> (file on disk, content type). Anything else is 404.
ROUTES = {
    "/": (os.path.join(BASE_DIR, "dashboard.html"), "text/html; charset=utf-8"),
    "/dashboard.html": (os.path.join(BASE_DIR, "dashboard.html"), "text/html; charset=utf-8"),
    "/vendor/chart.umd.min.js": (
        os.path.join(BASE_DIR, "vendor", "chart.umd.min.js"),
        "application/javascript; charset=utf-8"),
    "/vendor/chartjs-adapter-date-fns.bundle.min.js": (
        os.path.join(BASE_DIR, "vendor", "chartjs-adapter-date-fns.bundle.min.js"),
        "application/javascript; charset=utf-8"),
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "NetMonWeb/" + __version__

    def _serve(self, send_body):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        route = ROUTES.get(path)
        if route is None or not os.path.exists(route[0]):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        file_path, ctype = route
        try:
            with open(file_path, "rb") as f:
                body = f.read()
        except OSError:
            # dashboard.py may be rewriting the file right now - retry shortly
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ctype.startswith("text/html"):
            # always re-fetch the page so the 60s auto-refresh sees new data
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)

    def log_message(self, fmt, *args):
        pass  # quiet - no per-request log spam


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"serving the dashboard on port {PORT} (all interfaces)")
    server.serve_forever()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    main()
