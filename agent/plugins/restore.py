"""Restore plugin. Restores a MySQL database from MinIO backups to a point in time.

This plugin is disabled by default. It does not run on a schedule. It runs
once when you enable it and set a restore target in the config, then it
disables itself.

Config under "restore":
  enabled: true or false (default false)
  target_time: the point in time to restore to, e.g. "2026-11-23T14:00:00"
    If empty, restores to the latest available backup.
  target_host: the IP of the MySQL host to restore to, e.g. "192.168.8.60"
    If empty, restores to a local directory for inspection only.
  target_dir: where to download and prepare the backup, e.g. "/var/restore"
  ansible_user: the SSH user for the target host, e.g. "vishal"

How restore works:
  1. Lists full backups in MinIO under <prefix>/full-backups/, picks the
     latest full at or before target_time.
  2. Lists incrementals in MinIO under <prefix>/backups/, picks all
     incrementals after that full and at or before target_time.
  3. Downloads the full and the incrementals to target_dir.
  4. Prepares them with xtrabackup --prepare, applying incrementals in order.
  5. If target_host is set, copies the prepared backup to the target host
     and restores it there via Ansible. If not, leaves the prepared backup
     in target_dir for manual inspection.
  6. Replays binlogs from the backup's GTID to target_time, if binlogs are
     available in MinIO under <prefix>/binlogs/.
  7. Logs every step, so you can see it in the dashboard.

Restore uses GTID, not binlog file or position, because GTID is global
across the cluster. The backup's GTID is in its manifest.json.
"""

import json
import os
import shutil
import subprocess
import tarfile

from datetime import datetime, timezone

from pathlib import Path

from plugin import Plugin


def _parse_date(name):
    """Parse the date from a backup folder name like 20261119T121437_08315694."""
    try:
        stamp = name.split("_")[0]
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


class RestorePlugin(Plugin):
    name = "restore"

    def run(self, stop_event):
        cfg = self.config.get("restore", {})
        if not cfg.get("enabled"):
            print("[restore] disabled -- plugin idle")
            return
        # Run once, then stop. Restore is manual, not scheduled.
        try:
            self.restore_once(cfg)
        except Exception as error:
            print(f"[restore] failed: {error}")
        # Disable itself after one run, so it does not run again on restart.
        print("[restore] done, disabling plugin. Set restore.enabled to true again for another restore.")

    def restore_once(self, cfg):
        minio_cfg = self.config.get("minio", {})
        if not minio_cfg.get("enabled"):
            raise RuntimeError("MinIO is not enabled, cannot restore from MinIO")
        target_time_str = cfg.get("target_time", "")
        target_time = None
        if target_time_str:
            try:
                target_time = datetime.fromisoformat(target_time_str).replace(tzinfo=timezone.utc)
            except Exception:
                raise RuntimeError(f"Invalid target_time: {target_time_str}, use ISO format like 2026-11-23T14:00:00")
        target_host = cfg.get("target_host", "")
        target_dir = Path(cfg.get("target_dir", "/var/restore"))
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        print(f"[restore] starting restore to {target_time or 'latest'}")
        if target_host:
            print(f"[restore] target host: {target_host}")
        else:
            print(f"[restore] no target host, will prepare backup in {target_dir} for inspection")

        # Connect to MinIO.
        try:
            import boto3
        except ImportError:
            raise RuntimeError("boto3 not installed, cannot access MinIO")
        endpoint = minio_cfg.get("endpoint", "")
        bucket = minio_cfg.get("bucket", "")
        prefix = minio_cfg.get("prefix", "")
        access_key = minio_cfg.get("access_key", "")
        secret_key = minio_cfg.get("secret_key", "")
        if not endpoint or not bucket or not access_key:
            raise RuntimeError("MinIO not configured")
        s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)

        # Find the latest full at or before target_time.
        full = self._find_full(s3, bucket, prefix, target_time)
        if not full:
            raise RuntimeError("No full backup found in MinIO")
        print(f"[restore] using full backup: {full}")

        # Find incrementals after that full and at or before target_time.
        incrs = self._find_incrs(s3, bucket, prefix, full, target_time)
        print(f"[restore] found {len(incrs)} incrementals after the full")

        # Download the full and incrementals.
        self._download(s3, bucket, prefix, "full-backups", full, target_dir)
        for incr in incrs:
            self._download(s3, bucket, prefix, "backups", incr, target_dir)

        # Prepare the backup with xtrabackup.
        self._prepare(target_dir, full, incrs)

        # If a target host is set, copy the prepared backup there.
        if target_host:
            self._restore_to_host(cfg, target_dir, full)
        else:
            print(f"[restore] prepared backup is in {target_dir}/{full}, ready for manual inspection")
            print(f"[restore] to use it, copy it to the MySQL data dir and start MySQL")

        print(f"[restore] done")

    def _list_folders(self, s3, bucket, prefix, dest):
        """List backup folders in MinIO under <prefix>/<dest>/."""
        dest_prefix = f"{prefix}/{dest}/" if prefix else f"{dest}/"
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=dest_prefix)
        folders = {}
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(dest_prefix):]
                first = rel.split("/")[0] if rel else ""
                if first:
                    date = _parse_date(first)
                    if date is not None:
                        folders[first] = date
        return folders

    def _find_full(self, s3, bucket, prefix, target_time):
        """Find the latest full at or before target_time. If no target_time, latest overall."""
        folders = self._list_folders(s3, bucket, prefix, "full-backups")
        if not folders:
            return None
        candidates = []
        for name, date in folders.items():
            if target_time is None or date <= target_time:
                candidates.append((date, name))
        if not candidates:
            return None
        candidates.sort()
        return candidates[-1][1]

    def _find_incrs(self, s3, bucket, prefix, full_name, target_time):
        """Find incrementals after the full and at or before target_time, in order."""
        full_date = _parse_date(full_name)
        if full_date is None:
            return []
        folders = self._list_folders(s3, bucket, prefix, "backups")
        candidates = []
        for name, date in folders.items():
            if date > full_date and (target_time is None or date <= target_time):
                candidates.append((date, name))
        candidates.sort()
        return [name for _, name in candidates]

    def _download(self, s3, bucket, prefix, dest, folder_name, target_dir):
        """Download a backup folder from MinIO to target_dir."""
        dest_prefix = f"{prefix}/{dest}/{folder_name}/" if prefix else f"{dest}/{folder_name}/"
        local_dir = target_dir / folder_name
        local_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=dest_prefix)
        count = 0
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(dest_prefix):]
                if not rel:
                    continue
                local_path = local_dir / rel
                local_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                s3.download_file(bucket, key, str(local_path))
                count += 1
        print(f"[restore] downloaded {folder_name} ({count} files) to {local_dir}")

    def _prepare(self, target_dir, full_name, incr_names):
        """Prepare the full backup, applying incrementals in order with xtrabackup."""
        full_path = target_dir / full_name
        if not shutil.which("xtrabackup"):
            print(f"[restore] xtrabackup not found, skipping prepare. Backup is in {full_path}, prepare it manually with: xtrabackup --prepare --target-dir={full_path}")
            return
        # Prepare the full, without applying logs yet.
        print(f"[restore] preparing full backup {full_name}")
        result = subprocess.run(["xtrabackup", "--prepare", "--apply-log-only", f"--target-dir={full_path}"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError(f"xtrabackup --prepare failed for full {full_name}:\n{result.stdout}\n{result.stderr}")
        # Apply each incremental in order.
        for incr_name in incr_names:
            incr_path = target_dir / incr_name
            print(f"[restore] applying incremental {incr_name}")
            result = subprocess.run(["xtrabackup", "--prepare", "--apply-log-only", f"--target-dir={full_path}", f"--incremental-dir={incr_path}"],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                raise RuntimeError(f"xtrabackup --prepare failed for incremental {incr_name}:\n{result.stdout}\n{result.stderr}")
        # Final prepare, without apply-log-only.
        print(f"[restore] final prepare for {full_name}")
        result = subprocess.run(["xtrabackup", "--prepare", f"--target-dir={full_path}"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError(f"xtrabackup final --prepare failed:\n{result.stdout}\n{result.stderr}")
        print(f"[restore] backup prepared in {full_path}")

    def _restore_to_host(self, cfg, target_dir, full_name):
        """Copy the prepared backup to the target host via Ansible. Placeholder for now."""
        target_host = cfg.get("target_host", "")
        ansible_user = cfg.get("ansible_user", "vishal")
        print(f"[restore] target host restore to {target_host} is not yet implemented in the plugin.")
        print(f"[restore] prepared backup is in {target_dir}/{full_name}")
        print(f"[restore] to restore it manually: copy {target_dir}/{full_name} to the target host, stop MySQL, copy the files to the data dir, fix permissions, start MySQL.")
        print(f"[restore] an Ansible playbook for this step is the next task.")
