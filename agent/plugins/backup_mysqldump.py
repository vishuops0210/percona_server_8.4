"""
Full logical backup plugin (mysqldump).

Takes a single consistent full dump from a SECONDARY (falling back to the
PRIMARY only if no secondary answers), compresses it, and writes it to a local
folder with a small manifest. This mirrors the Ansible role's backup.py, kept
simple: every backup is a standalone full dump, so restore is a plain SQL load.

A later plugin will add XtraBackup for physical/incremental backups. Note that
XtraBackup must run ON a database node (it needs the datadir), unlike mysqldump
which runs fine here on the ProxySQL host over TCP.

It runs one backup immediately on start, then every `backup.interval_seconds`.
"""

import gzip
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import percona
from plugin import Plugin


class MysqldumpBackupPlugin(Plugin):
    name = "backup-mysqldump"

    def run(self, stop_event):
        mysql_cfg = self.config.get("mysql", {})
        backup_cfg = self.config.get("backup", {})
        interval = backup_cfg.get("interval_seconds", 86400)
        path = backup_cfg.get("path", "/var/backup/mysql")

        if not mysql_cfg.get("db_hosts"):
            print("[backup] no mysql.db_hosts configured -- plugin idle")
            return

        while not stop_event.is_set():
            try:
                self.backup_once(mysql_cfg, path)
            except Exception as error:
                print(f"[backup] failed: {error}")
            stop_event.wait(interval)

    def backup_once(self, mysql_cfg, path):
        members, sources, primary = percona.discover(mysql_cfg)
        if not sources:
            raise RuntimeError("no backup source found (no group member answered)")

        root = Path(path)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup_id = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                     + "_" + uuid.uuid4().hex[:8])
        folder = root / backup_id
        folder.mkdir(mode=0o700)
        dump_path = folder / "dump.sql.gz"

        last_error = None
        for source in sources:
            try:
                print(f"[backup] dumping from {source} -> {dump_path}")
                argv = [
                    mysql_cfg.get("mysqldump_bin", "mysqldump"),
                    "--defaults-file=" + mysql_cfg.get("client_file", percona.DEFAULT_CLIENT_FILE),
                    "-h", source,
                    "-P", str(mysql_cfg.get("port", 3306)),
                    "--single-transaction", "--routines", "--events", "--triggers",
                    "--source-data=2", "--set-gtid-purged=OFF", "--all-databases",
                ]
                # Stream mysqldump's output through gzip in this process.
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with gzip.open(dump_path, "wb") as gz:
                    shutil.copyfileobj(proc.stdout, gz, length=1024 * 1024)
                _, stderr = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(stderr.decode("utf-8", "replace").strip())

                binlog_file, binlog_pos = self._binlog_position(dump_path)
                manifest = {
                    "id": backup_id,
                    "kind": "full",
                    "source_host": source,
                    "binlog_file": binlog_file,
                    "binlog_pos": binlog_pos,
                    "size_bytes": dump_path.stat().st_size,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                }
                (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
                print(f"[backup] done: {backup_id} from {source} "
                      f"({manifest['size_bytes']} bytes)")
                return
            except Exception as error:
                last_error = error
                print(f"[backup] source {source} failed: {error}")
                continue

        # Every source failed: remove the empty folder and report.
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError(f"all sources failed, last error: {last_error}")

    def _binlog_position(self, dump_path):
        """Read the binlog file and position recorded by --source-data=2."""
        with gzip.open(dump_path, "rt", errors="replace") as stream:
            head = stream.read(65536)
        match = re.search(r"SOURCE_LOG_FILE='([^']+)', SOURCE_LOG_POS=(\d+)", head)
        if match:
            return match.group(1), int(match.group(2))
        return "", 0
