"""
Heartbeat plugin.

Sends a heartbeat to a configured URL every interval_seconds, so a
monitoring server can tell the agent is alive. If no URL is configured,
the plugin still logs a heartbeat line every interval, so the dashboard
has something to show and you can see the agent is alive.

Config, under "heartbeat":
  interval_seconds: how often to send/log a heartbeat (default 10)
  url: the URL to POST to, empty means no external heartbeat, just log
"""

import time
import urllib.request

from plugin import Plugin


class HeartbeatPlugin(Plugin):
    name = "heartbeat"

    def run(self, stop_event):
        heartbeat_cfg = self.config.get("heartbeat", {})
        interval = int(heartbeat_cfg.get("interval_seconds", 10))
        url = heartbeat_cfg.get("url", "")
        if not url:
            print("[heartbeat] no 'url' in config -- logging local heartbeat only")
        while not stop_event.wait(interval):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if url:
                try:
                    request = urllib.request.Request(url, method="POST")
                    with urllib.request.urlopen(request, timeout=5) as response:
                        print(f"[heartbeat] {stamp} POST {url} -> {response.status}")
                except Exception as error:
                    print(f"[heartbeat] {stamp} POST {url} failed: {error}")
            else:
                print(f"[heartbeat] {stamp} agent alive")
