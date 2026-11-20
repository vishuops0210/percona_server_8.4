"""
Dashboard plugin.

Serves a small HTML dashboard on proxy1 showing the agent logs, segregated
by plugin. Open http://192.168.8.55:<port> in a browser, click a plugin
button, and it shows only that plugin's live logs, so you do not have to
grep journalctl by hand.

Config, under "dashboard":
  enabled: true or false (default false)
  port: the port to listen on (default 8000)
  host: the address to bind (default 0.0.0.0)

The dashboard reads logs with journalctl -u recevo-agent, filters by the
plugin name in brackets, e.g. [minio], [backup], [binlog], [discovery],
and returns the matching lines. It auto-refreshes every 5 seconds.
"""

import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from plugin import Plugin

PLUGINS = ["backup", "binlog", "discovery", "heartbeat", "minio"]

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Recevo Agent Dashboard</title>
<style>
body { font-family: monospace; background: #111; color: #ddd; margin: 0; padding: 16px; }
h1 { font-size: 18px; margin: 0 0 12px 0; }
.buttons { margin-bottom: 12px; }
button { background: #222; color: #ddd; border: 1px solid #444; padding: 8px 14px; margin-right: 6px; cursor: pointer; font-family: monospace; font-size: 14px; }
button.active { background: #0a5; color: #fff; border-color: #0a5; }
#logs { background: #000; border: 1px solid #333; padding: 12px; white-space: pre-wrap; word-break: break-all; min-height: 400px; max-height: 70vh; overflow-y: auto; font-size: 13px; line-height: 1.5; }
.meta { color: #888; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<h1>Recevo Agent Dashboard</h1>
<div class="buttons" id="buttons"></div>
<div id="logs">Select a plugin to see its logs.</div>
<div class="meta" id="meta"></div>
<script>
const plugins = ["all", "backup", "binlog", "discovery", "heartbeat", "minio"];
let current = "all";
const btns = document.getElementById("buttons");
const logsEl = document.getElementById("logs");
const metaEl = document.getElementById("meta");
plugins.forEach(function(name) {
  const b = document.createElement("button");
  b.textContent = name;
  b.onclick = function() { current = name; refresh(); };
  b.id = "btn-" + name;
  btns.appendChild(b);
});
function refresh() {
  plugins.forEach(function(name) {
    document.getElementById("btn-" + name).classList.toggle("active", name === current);
  });
  fetch("/logs?plugin=" + current)
    .then(function(r) { return r.text(); })
    .then(function(text) {
      logsEl.textContent = text || "No logs for this plugin yet.";
      metaEl.textContent = "Plugin: " + current + " | refreshed: " + new Date().toLocaleTimeString() + " | auto-refresh every 5s";
    });
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class DashboardPlugin(Plugin):
    name = "dashboard"

    def run(self, stop_event):
        dashboard_cfg = self.config.get("dashboard", {})
        if not dashboard_cfg.get("enabled"):
            print("[dashboard] disabled -- plugin idle")
            return
        port = int(dashboard_cfg.get("port", 8000))
        host = dashboard_cfg.get("host", "0.0.0.0")

        server = HTTPServer((host, port), _Handler)
        print(f"[dashboard] serving on http://{host}:{port}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stop_event.wait()
        server.shutdown()
        print("[dashboard] stopped")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/logs":
            plugin = parse_qs(parsed.query).get("plugin", ["all"])[0]
            text = _read_logs(plugin)
            body = text.encode("utf-8", errors="replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _read_logs(plugin):
    """Return the last 200 journalctl lines for the agent, filtered by plugin."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "recevo-agent", "-n", "200", "--no-pager"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        lines = result.stdout.splitlines()
    except Exception as error:
        return f"Failed to read logs: {error}"
    if plugin == "all":
        return "\n".join(lines)
    needle = f"[{plugin}]"
    return "\n".join(line for line in lines if needle in line)
