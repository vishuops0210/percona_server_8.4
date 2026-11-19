"""
Full and incremental physical backup plugin (Percona XtraBackup).

Runs ON a database node. XtraBackup reads the datadir directly, so it cannot
run from the ProxySQL host. Install the agent on a SECONDARY so the backup
I/O stays off the writer.

Takes a full backup once a day and an incremental every hour in between.
Each backup lands in its own folder with a manifest (LSN range and binlog
position), so a restore knows exactly what to apply and in what order.
The minio-upload plugin ships finished backups off-box.

It runs once on start, then every `backup.interval_seconds`.
"""

import json
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from plugin import Plugin


class XtraBackupPlugin(Plugin):
    name = "backup-xtrabackup"

    def run(self, stop_event):
        mysql_cfg = self.config.get("mysql", {})
        backup_cfg = self.config.get("backup", {})
        interval = backup_cfg.get("interval_seconds", 3600)
        full_interval = backup_cfg.get("full_interval_seconds", 86400)
        path = backup_cfg.get("path", "/var/backup/mysql")
        datadir = backup_cfg.get("datadir", "/var/lib/mysql")

        if not mysql_cfg.get("db_hosts"):
            print("[backup] no mysql.db_hosts configured -- plugin idle")
            return

        while not stop_event.is_set():
            try:
                self.backup_once(mysql_cfg, backup_cfg, path, datadir, full_interval)
            except Exception as error:
                print(f"[backup] failed: {error}")
            stop_event.wait(interval)

    def backup_once(self, mysql_cfg, backup_cfg, path, datadir, full_interval):
        xtrabackup_bin = mysql_cfg.get("xtrabackup_bin", "xtrabackup")
        if shutil.which(xtrabackup_bin) is None:
            # The agent never installs anything itself. The fix is the
            # recevo_agent Ansible role, run from your laptop (the control
            # node) against this DB node. See agent/DEPLOY.md step 2.
            raise RuntimeError(
                f"xtrabackup binary '{xtrabackup_bin}' not found on this host. "
                "This agent runs on a database node, and the role installs it. "
                "From the repo root, on the verified-restore branch, run: "
                "ansible-playbook -i <inventory> playbooks/operations/recevo-agent.yml "
                "--extra-vars \"recevo_backup_password=<the backup user password>\""
            )

        root = Path(path)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Full if there is no finished backup yet, or the last full is older
        # than full_interval_seconds. Otherwise incremental from the latest
        # finished backup (full or incremental), so the chain stays linked.
        latest_full, latest_any = self._latest(root)
        now = datetime.now(timezone.utc)
        if latest_full is None or (now - latest_full[0]).total_seconds() >= full_interval:
            kind = "full"
            base_dir = None
        else:
            kind = "incremental"
            base_dir = latest_any[1]

        backup_id = now.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        folder = root / backup_id
        folder.mkdir(mode=0o700)

        argv = [
            xtrabackup_bin,
            "--defaults-file=" + mysql_cfg.get("client_file", "/etc/percona-backup/client.cnf"),
            # Talk to the LOCAL mysqld: this plugin backs up this node's datadir.
            "--host=127.0.0.1",
            "--port=" + str(mysql_cfg.get("port", 3306)),
            "--backup",
            "--datadir=" + datadir,
            "--target-dir=" + str(folder),
        ]
        if kind == "incremental":
            argv.append("--incremental-basedir=" + str(base_dir))

        print(f"[backup] {kind} backup -> {folder}"
              + (f" (base {base_dir.name})" if base_dir else ""))
        result = subprocess.run(argv, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            # A failed backup leaves a partial folder; remove it so it is
            # never used as an incremental base.
            shutil.rmtree(folder, ignore_errors=True)
            tail = "\n".join(result.stderr.strip().splitlines()[-15:])
            raise RuntimeError(f"xtrabackup {kind} failed:\n{tail}")

        checkpoints = self._read_kv(folder / "xtrabackup_checkpoints")
        binlog_file, binlog_pos = self._binlog_position(folder / "xtrabackup_binlog_info")
        manifest = {
            "id": backup_id,
            "kind": kind,
            "base": base_dir.name if base_dir else None,
            "source_host": socket.gethostname(),
            "from_lsn": checkpoints.get("from_lsn", ""),
            "to_lsn": checkpoints.get("to_lsn", ""),
            "binlog_file": binlog_file,
            "binlog_pos": binlog_pos,
            "size_bytes": sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()),
            "created_utc": now.isoformat(),
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"[backup] done: {backup_id} {kind} ({manifest['size_bytes']} bytes)")

    def _latest(self, root):
        """Find the latest finished full backup and the latest finished backup.

        Returns (latest_full, latest_any), each a (created_datetime, dir)
        tuple or None. Only folders with a manifest.json count; a folder
        without one is an incomplete backup and is ignored.
        """
        latest_full = None
        latest_any = None
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            manifest_path = folder / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
                created = datetime.fromisoformat(manifest["created_utc"])
            except Exception:
                continue
            entry = (created, folder)
            if latest_any is None or created > latest_any[0]:
                latest_any = entry
            if manifest.get("kind") == "full" and (latest_full is None or created > latest_full[0]):
                latest_full = entry
        return latest_full, latest_any

    def _read_kv(self, path):
        """Read a 'key = value' file such as xtrabackup_checkpoints."""
        values = {}
        if not path.exists():
            return values
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        return values

    def _binlog_position(self, path):
        """Read binlog file and position from xtrabackup_binlog_info.

        The file holds one line: <binlog file><TAB><position><TAB><gtid set>.
        """
        if not path.exists():
            return "", 0
        lines = path.read_text(errors="replace").splitlines()
        if not lines:
            return "", 0
        parts = lines[0].split("\t")
        if len(parts) >= 2:
            try:
                return parts[0].strip(), int(parts[1])
            except ValueError:
                return parts[0].strip(), 0
        return "", 0
