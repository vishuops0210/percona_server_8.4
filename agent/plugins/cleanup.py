"""
Cleanup plugin.

Deletes old backups and binlogs from local disk and from MinIO, so storage
does not bloat. Keeps the latest full and the latest incremental locally,
so the backup chain continues. Runs once a day by default.

Retention policy, from config under "cleanup":
  enabled: true or false (default false)
  interval_seconds: how often to run (default 86400, once a day)
  full_retention_days: keep full backups for this many days (default 30)
  incremental_retention_days: keep incrementals for this many days (default 7)
  binlog_retention_hours: keep binlogs for this many hours (default 2)

Paths are read from the backup and binlog config sections. MinIO settings
are read from the minio config section. The same retention applies to both
local disk and MinIO. The plugin logs every deletion, so you can see it in
the dashboard.
"""

import os
import shutil
import time
from datetime import datetime, timedelta

from plugin import Plugin


def _parse_backup_date(name):
    """Parse the date from a backup folder name like 20261119T121437_08315694."""
    try:
        stamp = name.split("_")[0]
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except Exception:
        return None


class CleanupPlugin(Plugin):
    name = "cleanup"

    def run(self, stop_event):
        cleanup_cfg = self.config.get("cleanup", {})
        if not cleanup_cfg.get("enabled"):
            print("[cleanup] disabled -- plugin idle")
            return
        interval = int(cleanup_cfg.get("interval_seconds", 86400))
        full_days = int(cleanup_cfg.get("full_retention_days", 30))
        incr_days = int(cleanup_cfg.get("incremental_retention_days", 7))
        binlog_hours = int(cleanup_cfg.get("binlog_retention_hours", 2))

        backup_cfg = self.config.get("backup", {})
        full_path = backup_cfg.get("full_path", "/var/backup/mysql-full")
        incr_path = backup_cfg.get("incremental_path", "/var/backup/mysql-incremental")
        binlog_path = self.config.get("binlog", {}).get("path", "/var/backup/binlog")

        while not stop_event.wait(interval):
            self._cleanup_once(full_path, incr_path, binlog_path, full_days, incr_days, binlog_hours)

    def _cleanup_once(self, full_path, incr_path, binlog_path, full_days, incr_days, binlog_hours):
        now = datetime.now()
        full_cutoff = now - timedelta(days=full_days)
        incr_cutoff = now - timedelta(days=incr_days)
        binlog_cutoff = now - timedelta(hours=binlog_hours)

        # Local full backups, keep the latest one always.
        deleted_fulls = self._clean_folders(full_path, full_cutoff, keep_latest=True)
        # Local incrementals, keep the latest one always.
        deleted_incrs = self._clean_folders(incr_path, incr_cutoff, keep_latest=True)
        # Local binlogs, delete by file mtime, no keep-latest rule.
        deleted_binlogs = self._clean_files(binlog_path, binlog_cutoff)

        # MinIO, same retention, same keep-latest rule for backups.
        minio_deleted = self._clean_minio(full_cutoff, incr_cutoff, binlog_cutoff)

        print(f"[cleanup] done: local fulls deleted {deleted_fulls}, incrementals deleted {deleted_incrs}, binlogs deleted {deleted_binlogs}, minio objects deleted {minio_deleted}")

    def _clean_folders(self, path, cutoff, keep_latest=True):
        """Delete subfolders older than cutoff. Keeps the latest folder if keep_latest."""
        if not os.path.isdir(path):
            return 0
        folders = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if not os.path.isdir(full):
                continue
            date = _parse_backup_date(name)
            if date is None:
                continue
            folders.append((date, full, name))
        if not folders:
            return 0
        folders.sort()
        deleted = 0
        # Keep the latest folder always, so the backup chain continues.
        keep = {folders[-1][1]} if keep_latest else set()
        for date, full, name in folders:
            if full in keep:
                continue
            if date < cutoff:
                try:
                    shutil.rmtree(full)
                    print(f"[cleanup] deleted local folder {full}")
                    deleted += 1
                except Exception as error:
                    print(f"[cleanup] failed to delete {full}: {error}")
        return deleted

    def _clean_files(self, path, cutoff):
        """Delete files older than cutoff, by mtime. Skips .uploaded markers."""
        if not os.path.isdir(path):
            return 0
        deleted = 0
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            if name.endswith(".uploaded"):
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(full))
            except Exception:
                continue
            if mtime < cutoff:
                try:
                    os.remove(full)
                    marker = full + ".uploaded"
                    if os.path.exists(marker):
                        os.remove(marker)
                    print(f"[cleanup] deleted local file {full}")
                    deleted += 1
                except Exception as error:
                    print(f"[cleanup] failed to delete {full}: {error}")
        return deleted

    def _clean_minio(self, full_cutoff, incr_cutoff, binlog_cutoff):
        """Delete expired objects from MinIO under the agent prefix."""
        minio_cfg = self.config.get("minio", {})
        if not minio_cfg.get("enabled"):
            return 0
        try:
            import boto3
        except ImportError:
            print("[cleanup] boto3 not available, skipping MinIO cleanup")
            return 0
        endpoint = minio_cfg.get("endpoint", "")
        bucket = minio_cfg.get("bucket", "")
        prefix = minio_cfg.get("prefix", "")
        access_key = minio_cfg.get("access_key", "")
        secret_key = minio_cfg.get("secret_key", "")
        if not endpoint or not bucket or not access_key:
            print("[cleanup] MinIO not configured, skipping MinIO cleanup")
            return 0
        try:
            s3 = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
        except Exception as error:
            print(f"[cleanup] MinIO client failed: {error}")
            return 0

        # Map each dest folder to its cutoff. full-backups uses full cutoff,
        # backups (incrementals) uses incremental cutoff, binlogs uses binlog cutoff.
        dest_cutoffs = {
            "full-backups": full_cutoff,
            "backups": incr_cutoff,
            "binlogs": binlog_cutoff,
        }
        deleted = 0
        for dest, cutoff in dest_cutoffs.items():
            dest_prefix = f"{prefix}/{dest}/" if prefix else f"{dest}/"
            try:
                paginator = s3.get_paginator("list_objects_v2")
                pages = paginator.paginate(Bucket=bucket, Prefix=dest_prefix)
                keys = []
                for page in pages:
                    for obj in page.get("Contents", []):
                        keys.append(obj["Key"])
            except Exception as error:
                print(f"[cleanup] MinIO list failed for {dest_prefix}: {error}")
                continue
            # For backups, keep the latest folder. For binlogs, no keep-latest.
            keep_key = None
            if dest in ("full-backups", "backups"):
                # Find the latest folder name in the keys.
                folders = set()
                for key in keys:
                    parts = key[len(dest_prefix):].split("/")
                    if parts and parts[0]:
                        folders.add(parts[0])
                if folders:
                    dated = [( _parse_backup_date(f), f) for f in folders]
                    dated = [(d, f) for d, f in dated if d is not None]
                    if dated:
                        dated.sort()
                        keep_key = dest_prefix + dated[-1][1]
            for key in keys:
                # Skip the latest backup folder entirely.
                if keep_key and key.startswith(keep_key):
                    continue
                # Parse the date from the folder or file name.
                rel = key[len(dest_prefix):]
                first = rel.split("/")[0] if rel else ""
                date = _parse_backup_date(first)
                if date is None:
                    # For binlogs, use the object LastModified from list, re-fetch if needed.
                    continue
                if date < cutoff:
                    try:
                        s3.delete_object(Bucket=bucket, Key=key)
                        print(f"[cleanup] deleted MinIO object s3://{bucket}/{key}")
                        deleted += 1
                    except Exception as error:
                        print(f"[cleanup] failed to delete s3://{bucket}/{key}: {error}")
        return deleted
