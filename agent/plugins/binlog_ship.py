"""
Binlog shipping plugin (for RPO / point-in-time recovery).

Mirrors the Ansible role's binlog-ship.sh: find the PRIMARY, FLUSH BINARY LOGS
to seal the active log, then download every rotated (non-active) binlog that is
not already archived locally, using `mysqlbinlog --read-from-remote-server
--raw`. Nothing runs on the database nodes themselves.

The gap between runs is roughly your RPO, so with the default 5 minutes the
worst case is about 5 minutes of lost transactions. Runs once on start, then
every `binlog.interval_seconds`.
"""

import subprocess
from pathlib import Path

from plugins import percona
from plugin import Plugin


class BinlogShipPlugin(Plugin):
    name = "binlog-ship"

    def run(self, stop_event):
        mysql_cfg = self.config.get("mysql", {})
        binlog_cfg = self.config.get("binlog", {})
        interval = binlog_cfg.get("interval_seconds", 300)
        archive = Path(binlog_cfg.get("path", "/var/backup/binlog"))

        if not mysql_cfg.get("db_hosts"):
            print("[binlog] no mysql.db_hosts configured -- plugin idle")
            return

        print(f"[binlog] shipping rotated binlogs to {archive} every {interval}s")
        while not stop_event.is_set():
            try:
                self.ship_once(mysql_cfg, archive)
            except Exception as error:
                print(f"[binlog] failed: {error}")
            stop_event.wait(interval)

    def ship_once(self, mysql_cfg, archive):
        # Create the archive dir inside the guarded loop, so a permissions or
        # path problem is reported and retried instead of crashing the thread.
        archive.mkdir(mode=0o700, parents=True, exist_ok=True)

        _members, _sources, primary = percona.discover(mysql_cfg)
        if not primary:
            raise RuntimeError("no primary found")

        # Seal the active log so the last few minutes become a shippable file.
        # Best effort: if it fails we still ship the already-rotated logs.
        try:
            percona.mysql_query(mysql_cfg, primary, "FLUSH BINARY LOGS")
        except subprocess.CalledProcessError:
            print("[binlog] FLUSH BINARY LOGS failed; shipping rotated logs only")

        logs = percona.mysql_query(mysql_cfg, primary, "SHOW BINARY LOGS")
        names = [line.split("\t")[0] for line in logs.splitlines() if line.strip()]
        if not names:
            return
        active = names[-1]  # the server is still writing to the last one

        client_file = mysql_cfg.get("client_file", percona.DEFAULT_CLIENT_FILE)
        for log in names:
            if log == active:
                continue
            if (archive / log).exists():
                continue  # already shipped; rotated logs are immutable
            print(f"[binlog] fetching {log} from {primary}")
            argv = [
                mysql_cfg.get("mysqlbinlog_bin", "mysqlbinlog"),
                "--defaults-file=" + client_file,
                "--read-from-remote-server",
                "--host=" + primary,
                "--port=" + str(mysql_cfg.get("port", 3306)),
                "--raw",
                "--result-file=" + str(archive) + "/",
                log,
            ]
            subprocess.run(argv, check=True, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (archive / log).chmod(0o600)
