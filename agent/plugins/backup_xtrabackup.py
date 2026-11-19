"""
Remote physical backup plugin (Percona XtraBackup via Ansible).

Runs on the ProxySQL host (proxy1). The agent identifies a SECONDARY database
node, then runs an Ansible playbook against it. The playbook installs
XtraBackup on the secondary, takes the backup there, and fetches the archive
back to this host. The agent then extracts it and uploads it to MinIO.

XtraBackup reads the datadir directly, so it cannot run on proxy1. This
plugin is how a backup is taken from a separate server: the work happens on
the secondary, the result lands here.

Takes a full backup once a day and an incremental every hour in between.
Each backup lands in `backup.path/<id>/` with a `manifest.json` (kind, base,
LSN range, binlog position).

It runs once on start, then every `backup.interval_seconds`.
"""

import json
import shutil
import socket
import subprocess
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import percona
from plugin import Plugin


class XtraBackupPlugin(Plugin):
    name = "backup-xtrabackup"

    def run(self, stop_event):
        mysql_cfg = self.config.get("mysql", {})
        backup_cfg = self.config.get("backup", {})
        interval = backup_cfg.get("interval_seconds", 3600)
        full_interval = backup_cfg.get("full_interval_seconds", 86400)
        path = backup_cfg.get("path", "/var/backup/mysql")

        if not mysql_cfg.get("db_hosts"):
            print("[backup] no mysql.db_hosts configured -- plugin idle")
            return

        while not stop_event.is_set():
            try:
                self.backup_once(mysql_cfg, backup_cfg, path, full_interval)
            except Exception as error:
                print(f"[backup] failed: {error}")
            stop_event.wait(interval)

    def backup_once(self, mysql_cfg, backup_cfg, path, full_interval):
        ansible_dir = backup_cfg.get("ansible_dir", "/home/vishal/ansible")
        ansible_playbook = backup_cfg.get(
            "ansible_playbook", "playbooks/operations/recevo-xtrabackup.yml")
        ansible_user = backup_cfg.get("ansible_user", "vishal")
        if shutil.which("ansible-playbook") is None:
            raise RuntimeError(
                "ansible-playbook not found on this host. Install Ansible "
                "with the recevo_agent role (it runs on proxy1).")

        # Pick a secondary to back up from. Never back up from the writer.
        secondary = self._pick_secondary(mysql_cfg)
        if not secondary:
            raise RuntimeError(
                "no SECONDARY node found. Check that the cluster is up and "
                "discovery can reach it.")

        root = Path(path)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Full if there is no finished backup yet, or the last full is older
        # than full_interval_seconds. Otherwise incremental from the latest
        # finished backup (full or incremental), so the chain stays linked.
        latest_full, latest_any = self._latest(root)
        now = datetime.now(timezone.utc)
        if latest_full is None or (now - latest_full[0]).total_seconds() >= full_interval:
            kind = "full"
            base = ""
        else:
            kind = "incremental"
            base = latest_any[1].name

        backup_id = now.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        print(f"[backup] {kind} backup of {secondary} -> {root / backup_id}"
              + (f" (base {base})" if base else ""))

        # Run the playbook against the secondary. It installs XtraBackup,
        # takes the backup there, and fetches the archive back to this host.
        argv = [
            "ansible-playbook",
            "-i", secondary + ",",
            "-u", ansible_user,
            ansible_playbook,
            "--extra-vars",
            f"backup_id={backup_id} backup_kind={kind} backup_base={base}",
        ]
        result = subprocess.run(argv, cwd=ansible_dir, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
            raise RuntimeError(f"ansible-playbook for {kind} backup failed:\n{tail}")

        # The playbook fetched the archive to <path>/<id>.tar.gz. Extract it
        # and write the manifest from the XtraBackup metadata inside.
        archive = root / (backup_id + ".tar.gz")
        if not archive.exists():
            raise RuntimeError(
                f"playbook finished but {archive} is missing on this host")
        folder = root / backup_id
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(path=root)
        archive.unlink()

        checkpoints = self._read_kv(folder / "xtrabackup_checkpoints")
        binlog_file, binlog_pos = self._binlog_position(folder / "xtrabackup_binlog_info")
        manifest = {
            "id": backup_id,
            "kind": kind,
            "base": base or None,
            "source_host": secondary,
            "from_lsn": checkpoints.get("from_lsn", ""),
            "to_lsn": checkpoints.get("to_lsn", ""),
            "binlog_file": binlog_file,
            "binlog_pos": binlog_pos,
            "size_bytes": sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()),
            "created_utc": now.isoformat(),
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(f"[backup] done: {backup_id} {kind} ({manifest['size_bytes']} bytes)")

    def _pick_secondary(self, mysql_cfg):
        """Return the IP of a SECONDARY node, or None if none is found."""
        try:
            result = percona.discover(mysql_cfg)
        except Exception:
            return None
        members = result.get("members", []) if isinstance(result, dict) else []
        for member in members:
            host = member.get("host") or member.get("MEMBER_HOST") or ""
            role = member.get("role") or member.get("MEMBER_ROLE") or ""
            if host and str(role).upper() == "SECONDARY":
                return str(host)
        return None

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
