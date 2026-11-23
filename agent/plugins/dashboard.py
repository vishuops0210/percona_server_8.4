"""
Dashboard plugin.

Serves a small HTML dashboard on proxy1 showing the agent logs, segregated
by plugin. Open http://192.168.8.55:<port> in a browser, click a plugin
button, and it shows only that plugin's live logs, so you do not have to
grep journalctl by hand.

The header shows a live heartbeat: a green ALIVE dot when the agent logged
in the last 30 seconds, the time of the last log line, and the agent uptime.
Uptime is calculated from the systemd service start time, not from the
journal window, so it stays correct even after many log lines.

Config, under "dashboard":
  enabled: true or false (default false)
  port: the port to listen on (default 8000)
  host: the address to bind (default 0.0.0.0)

The dashboard reads logs with journalctl -u recevo-agent. For a specific
plugin it fetches a large window and filters by the plugin name in brackets,
e.g. [minio], [backup], [binlog], [discovery], then returns the last 200
matching lines. This way a noisy plugin like heartbeat does not push a
quiet plugin like backup out of the window. It auto-refreshes every 1 second.
"""

import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from plugin import Plugin

PLUGINS = ["backup", "binlog", "discovery", "heartbeat", "minio", "cleanup", "restore"]

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Automation Engine Logs</title>
<style>
body { font-family: monospace; background: #111; color: #ddd; margin: 0; padding: 16px; }
h1 { font-size: 18px; margin: 0 0 8px 0; display: inline-block; }
.status { display: inline-block; margin-left: 16px; font-size: 14px; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; background: #0a5; margin-right: 6px; vertical-align: middle; }
.dot.dead { background: #c00; }
#uptime { color: #888; font-size: 12px; margin-bottom: 12px; }
.buttons { margin-bottom: 12px; }
button { background: #222; color: #ddd; border: 1px solid #444; padding: 8px 14px; margin-right: 6px; cursor: pointer; font-family: monospace; font-size: 14px; }
button.active { background: #0a5; color: #fff; border-color: #0a5; }
#logs { background: #000; border: 1px solid #333; padding: 12px; white-space: pre-wrap; word-break: break-all; min-height: 400px; max-height: 70vh; overflow-y: auto; font-size: 13px; line-height: 1.5; }
.meta { color: #888; font-size: 12px; margin-top: 8px; }
</style>
</head>
<body>
<h1>Automation Engine Logs</h1>
<span class="status"><span class="dot" id="dot"></span><span id="alive">...</span></span>
<div id="uptime"></div>
<div class="buttons" id="buttons"></div>
<div id="logs">Select a plugin to see its logs.</div>
<div class="meta" id="meta"></div>
<script>
const plugins = ["all", "backup", "binlog", "cleanup", "discovery", "heartbeat", "minio", "restore"];
let current = "all";
const btns = document.getElementById("buttons");
const logsEl = document.getElementById("logs");
const metaEl = document.getElementById("meta");
const dotEl = document.getElementById("dot");
const aliveEl = document.getElementById("alive");
const uptimeEl = document.getElementById("uptime");
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
      metaEl.textContent = "Plugin: " + current + " | refreshed: " + new Date().toLocaleTimeString() + " | auto-refresh every 1s";
    });
  fetch("/status")
    .then(function(r) { return r.json(); })
    .then(function(s) {
      dotEl.classList.toggle("dead", !s.alive);
      aliveEl.textContent = s.alive ? "ALIVE" : "DEAD";
      uptimeEl.textContent = "Last log: " + s.last_log + " | Agent started: " + s.started + " | Uptime: " + s.uptime;
    });
}
refresh();
setInterval(refresh, 1000);
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
        if parsed.path == "/status":
            import json as _json
            status = _agent_status()
            body = _json.dumps(status).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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


def _journal_lines(limit=5000):
    try:
        result = subprocess.run(
            ["journalctl", "-u", "recevo-agent", "-n", str(limit), "--no-pager"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
        return result.stdout.splitlines()
    except Exception:
        return []


def _read_logs(plugin):
    """Return the last 200 journalctl lines for the plugin.

    Fetches a large window (5000 lines) and filters by plugin first, then
    takes the last 200 matching lines. This way a noisy plugin like
    heartbeat (every 10s) and discovery (every 30s) does not push a quiet
    plugin like backup (hourly) out of the window.
    """
    lines = _journal_lines(limit=5000)
    if not lines:
        return "Failed to read logs."
    if plugin == "all":
        return "\n".join(lines[-200:])
    needle = f"[{plugin}]"
    matching = [line for line in lines if needle in line]
    if not matching:
        return ""
    return "\n".join(matching[-200:])


def _service_start_time():
    """Return the systemd service start time, or None if it cannot be read."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "recevo-agent", "--property=ActiveEnterTimestamp"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("ActiveEnterTimestamp="):
                value = line.split("=", 1)[1].strip()
                if not value:
                    return None
                try:
                    return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z")
                except Exception:
                    try:
                        return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S")
                    except Exception:
                        return None
        return None
    except Exception:
        return None


def _agent_status():
    """Return alive flag, last log time, start time, and uptime."""
    lines = _journal_lines(limit=200)
    now = datetime.now()
    if not lines:
        return {"alive": False, "last_log": "never", "started": "unknown", "uptime": "unknown"}
    last = lines[-1]
    last_time = _parse_journal_time(last, now)
    alive = False
    last_log = "unknown"
    if last_time:
        last_log = last_time.strftime("%H:%M:%S")
        alive = (now - last_time).total_seconds() < 30

    started_dt = _service_start_time()
    if started_dt is None:
        started_dt = _parse_journal_time(lines[0], now)
    started = "unknown"
    uptime = "unknown"
    if started_dt:
        started = started_dt.strftime("%H:%M:%S")
        delta = now - started_dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            total_seconds = 0
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        if days > 0:
            uptime = f"{days}d {hours}h {minutes}m {seconds}s"
        elif hours > 0:
            uptime = f"{hours}h {minutes}m {seconds}s"
        else:
            uptime = f"{minutes}m {seconds}s"
    return {"alive": alive, "last_log": last_log, "started": started, "uptime": uptime}


def _parse_journal_time(line, now):
    try:
        parts = line.split()
        if len(parts) < 3:
            return None
        stamp = " ".join(parts[0:3]) + f" {now.year}"
        return datetime.strptime(stamp, "%b %d %H:%M:%S %Y")
    except Exception:
        return None

