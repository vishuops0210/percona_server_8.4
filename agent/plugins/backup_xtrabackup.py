"""Remote physical backup plugin (Percona XtraBackup via Ansible).

Runs on proxy1. Picks a SECONDARY, runs the playbook there, fetches the
archive back, extracts it, writes the manifest.

Full and incremental go to separate folders from config.
Takes a full once every full_every, an incremental every interval in between.
Each backup lands as <id>/ with manifest.json.

Source of truth for the chain is MinIO. Lists backup folders in MinIO to find
the latest full and latest incremental. Falls back to local folders if MinIO
is empty or unreachable. If both are empty, takes a full.
"""

import json
import shutil
import subprocess
import tarfile
import uuid

from datetime import datetime, timezone

from pathlib import Path

from plugins import percona

from plugin import Plugin


def _seconds(value, default=0):
    """Parse a duration like 30s, 5m, 1h, 7d, 12mo, 1y into seconds.
    Units: s seconds, m or min minutes, h hours, d days, w weeks,
    mo or M months (30 days), y years (365 days)."""
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if not value:
        return default
    lower = value.lower()
    if lower.endswith("mo") or lower.endswith("mon"):
        try:
            number = int(value[:-2] if lower.endswith("mo") else value[:-3])
            return number * 2592000
        except ValueError:
            return default
    if lower.endswith("min"):
        try:
            number = int(value[:-3])
            return number * 60
        except ValueError:
            return default
    unit = value[-1]
    try:
        number = int(value[:-1])
    except ValueError:
        return default
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
    return number * multipliers.get(unit, 1)


class XtraBackupPlugin(Plugin):
    name = "backup-xtrabackup"

    def run(self, stop_event):
        mysql_cfg = self.config.get("mysql", {})
        backup_cfg = self.config.get("backup", {})
        interval = _seconds(backup_cfg.get("interval", "1h"), 3600)
        full_every = _seconds(backup_cfg.get("full_every", "7d"), 604800)

        if not mysql_cfg.get("db_hosts"):
            print("[backup] no mysql.db_hosts configured -- plugin idle")
            return

        while not stop_event.is_set():
            try:
                self.backup_once(mysql_cfg, backup_cfg, full_every)
            except Exception as error:
                print(f"[backup] failed: {error}")
            stop_event.wait(interval)

    def backup_once(self, mysql_cfg, backup_cfg, full_every):
        ansible_dir = backup_cfg.get("ansible_dir", "/home/vishal/ansible")
        ansible_playbook = backup_cfg.get(
            "ansible_playbook", "playbooks/operations/recevo-xtrabackup.yml")
        ansible_user = backup_cfg.get("ansible_user", "vishal")

        if shutil.which("ansible-playbook") is None:
            raise RuntimeError("ansible-playbook not found on this host")

        secondary = self._pick_secondary(mysql_cfg)
        if not secondary:
            raise RuntimeError("no SECONDARY node found")

        full_path = Path(backup_cfg.get("full_path", "/var/backup/mysql-full"))
        incremental_path = Path(backup_cfg.get("incremental_path", "/var/backup/mysql-incremental"))
        staging_path = Path(backup_cfg.get("path", "/var/backup/mysql"))

        full_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        incremental_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging_path.mkdir(mode=0o700, parents=True, exist_ok=True)

        latest_full, latest_any = self._latest(full_path, incremental_path)
        now = datetime.now(timezone.utc)

        if latest_full is None or (now - latest_full[0]).total_seconds() >= full_every:
            kind = "full"
            base = ""
            dest_path = full_path
        else:
            kind = "incremental"
            base = latest_any[1].name
            dest_path = incremental_path

        backup_id = now.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        print(f"[backup] {kind} backup of {secondary} -> {dest_path / backup_id}"
              + (f" (base {base})" if base else ""))

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

        archive = staging_path / (backup_id + ".tar.gz")
        if not archive.exists():
            raise RuntimeError(f"playbook finished but {archive} is missing")

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(path=staging_path)
        archive.unlink()

        folder = dest_path / backup_id
        shutil.move(str(staging_path / backup_id), str(folder))

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
        try:
            members, sources, primary = percona.discover(mysql_cfg)
        except Exception:
            return None
        for host, role in members:
            if host and str(role).upper() == "SECONDARY":
                return str(host)
        return None

    def _latest(self, full_path, incremental_path):
        """Find latest full and latest any backup. MinIO first, local fallback."""
        minio_full, minio_any = self._latest_from_minio()
        if minio_full is not None or minio_any is not None:
            return minio_full, minio_any
        return self._latest_from_local(full_path, incremental_path)

    def _latest_from_minio(self):
        minio_cfg = self.config.get("minio", {})
        if not minio_cfg.get("enabled"):
            return None, None
        try:
            import boto3
        except ImportError:
            return None, None
        endpoint = minio_cfg.get("endpoint", "")
        bucket = minio_cfg.get("bucket", "")
        prefix = minio_cfg.get("prefix", "")
        access_key = minio_cfg.get("access_key", "")
        secret_key = minio_cfg.get("secret_key", "")
        if not endpoint or not bucket or not access_key:
            return None, None
        try:
            s3 = boto3.client("s3", endpoint_url=endpoint,
                              aws_access_key_id=access_key,
                              aws_secret_access_key=secret_key)
        except Exception as error:
            print(f"[backup] MinIO client failed, falling back to local: {error}")
            return None, None

        latest_full = None
        latest_any = None
        for dest, is_full in (("full-backups", True), ("backups", False)):
            dest_prefix = f"{prefix}/{dest}/" if prefix else f"{dest}/"
            try:
                paginator = s3.get_paginator("list_objects_v2")
                pages = paginator.paginate(Bucket=bucket, Prefix=dest_prefix)
                folders = set()
                for page in pages:
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        rel = key[len(dest_prefix):]
                        first = rel.split("/")[0] if rel else ""
                        if first:
                            folders.add(first)
            except Exception as error:
                print(f"[backup] MinIO list failed for {dest_prefix}: {error}")
                return None, None
            for folder_name in folders:
                try:
                    stamp = folder_name.split("_")[0]
                    created = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                entry = (created, Path(folder_name))
                if latest_any is None or created > latest_any[0]:
                    latest_any = entry
                if is_full and (latest_full is None or created > latest_full[0]):
                    latest_full = entry
        return latest_full, latest_any

    def _latest_from_local(self, full_path, incremental_path):
        latest_full = None
        latest_any = None
        for root in (full_path, incremental_path):
            if not root.is_dir():
                continue
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
        values = {}
        if not path.exists():
            return values
        for line in path.read_text(errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        return values

    def _binlog_position(self, path):
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

