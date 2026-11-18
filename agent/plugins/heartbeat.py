"""
Heartbeat plugin.

Sends a small "I am alive" message to a URL every few seconds, so a server can
tell that the agent is still running. If the agent goes quiet, the server knows
something is wrong.

This is the first plugin. It shows the pattern every later plugin will follow.
"""

import json
import urllib.request
from datetime import datetime, timezone

from plugin import Plugin


class HeartbeatPlugin(Plugin):
    name = "heartbeat"

    def run(self, stop_event):
        # Read the "heartbeat" section from the config.
        settings = self.config.get("heartbeat", {})
        url = settings.get("url")
        interval = settings.get("interval_seconds", 30)
        agent_id = self.config.get("agent_id", "agent")

        # Without a URL there is nowhere to send to, so the plugin stays idle.
        if not url:
            print("[heartbeat] no 'url' in config -- plugin idle")
            return

        print(f"[heartbeat] sending to {url} every {interval}s")

        # Loop until the agent is asked to stop.
        while not stop_event.is_set():
            self.send(url, agent_id)
            # Wait for the interval, but wake up immediately if asked to stop.
            stop_event.wait(interval)

    def send(self, url, agent_id):
        """POST one heartbeat message to the URL."""
        payload = json.dumps({
            "agent_id": agent_id,
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                print(f"[heartbeat] sent ({response.status})")
        except Exception as error:
            # A failed heartbeat must not crash the agent; just report it.
            print(f"[heartbeat] failed to send: {error}")
