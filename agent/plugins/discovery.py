"""
Discovery plugin.

Periodically asks the Group Replication cluster which node is the PRIMARY and
which are SECONDARY, and prints the role of each. The backup and binlog plugins
use the same discovery to pick where to read from, so a failover is picked up
automatically on the next run.
"""

import percona
from plugin import Plugin


class DiscoveryPlugin(Plugin):
    name = "discovery"

    def run(self, stop_event):
        mysql_cfg = self.config.get("mysql", {})
        interval = self.config.get("discovery", {}).get("interval_seconds", 30)

        if not mysql_cfg.get("db_hosts"):
            print("[discovery] no mysql.db_hosts configured -- plugin idle")
            return

        while not stop_event.is_set():
            try:
                members, sources, primary = percona.discover(mysql_cfg)
                for host, role in members:
                    print(f"[discovery] {host} -> {role}")
                best_source = sources[0] if sources else None
                print(f"[discovery] primary={primary} backup-source={best_source}")
            except Exception as error:
                print(f"[discovery] failed: {error}")
            stop_event.wait(interval)
