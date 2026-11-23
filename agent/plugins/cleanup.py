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
  monthly_retention_months: keep monthly full backups for this many months (default 12)
  yearly_retention_years: keep yearly full backups for this many years (default 0, forever)

GFS retention for full backups:
  Daily: keep fulls from the last full_retention_days (30 days).
  Monthly: keep the first full of each month for monthly_retention_months (12 months).
  Yearly: keep the first full of January for yearly_retention_years (0 means forever).

The monthly and yearly fulls live in separate folders in MinIO:
  monthly-backups/ and yearly-backups/, under the agent prefix. They are
  uploaded there by the minio plugin when a full is the first of its month
  or year. This plugin cleans those folders with their own retention.

Paths are read from the backup and binlog config sections. MinIO settings
are read from the minio config section. The plugin logs every deletion, so
you can see it in the dashboard.
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
        monthly_months = int(cleanup_cfg.get("monthly_retention_months", 12))
        yearly_years = int(cleanup_cfg.get("yearly_retention_years", 0))

        backup_cfg = self.config.get("backup", {})
        full_path = backup_cfg.get("full_path", "/var/backup/mysql-full")
        incr_path = backup_cfg.get("incremental_path", "/var/backup/mysql-incremental")
        binlog_path = self.config.get("binlog", {}).get("path", "/var/backup/binlog")

        while not stop_event.wait(interval):
            self._cleanup_once(full_path, incr_path, binlog_path, full_days, incr_days, binlog_hours, monthly_months, yearly_years)

    def _cleanup_once(self, full_path, incr_path, binlog_path, full_days, incr_days, binlog_hours, monthly_months, yearly_years):
        now = datetime.now()
        full_cutoff = now - timedelta(days=full_days)
        incr_cutoff = now - timedelta(days=incr_days)
        binlog_cutoff = now - timedelta(hours=binlog_hours)
        monthly_cutoff = now - timedelta(days=monthly_months * 30)
        yearly_cutoff = None
        if yearly_years > 0:
            yearly_cutoff = now - timedelta(days=yearly_years * 365)

        # Local full backups, keep the latest one always, plus GFS keeps.
        deleted_fulls = self._clean_folders_gfs(full_path, full_cutoff, monthly_cutoff, yearly_cutoff)
        # Local incrementals, keep the latest one always.
        deleted_incrs = self._clean_folders(incr_path, incr_cutoff, keep_latest=True)
        # Local binlogs, delete by file mtime, no keep-latest rule.
        deleted_binlogs = self._clean_files(binlog_path, binlog_cutoff)

        # MinIO, same retention, plus monthly and yearly folders.
        minio_deleted = self._clean_minio(full_cutoff, incr_cutoff, binlog_cutoff, monthly_cutoff, yearly_cutoff)

        print(f"[cleanup] done: local fulls deleted {deleted_fulls}, incrementals deleted {deleted_incrs}, binlogs deleted {deleted_binlogs}, minio objects deleted {minio_deleted}")

    def _is_monthly_keep(self, date, all_dates, monthly_cutoff):
        """True if this date is the first full of its month and within monthly retention."""
        if date < monthly_cutoff:
            return False
        month_key = (date.year, date.month)
        month_dates = [d for d in all_dates if (d.year, d.month) == month_key]
        if not month_dates:
            return False
        return date == min(month_dates)

    def _is_yearly_keep(self, date, all_dates, yearly_cutoff):
        """True if this date is the first full of January and within yearly retention."""
        if date.month != 1:
            return False
        if yearly_cutoff is not None and date < yearly_cutoff:
            return False
        year_dates = [d for d in all_dates if d.year == date.year and d.month == 1]
        if not year_dates:
            return False
        return date == min(year_dates)

    def _clean_folders_gfs(self, path, cutoff, monthly_cutoff, yearly_cutoff):
        """Delete subfolders older than cutoff, keeping GFS monthly/yearly and latest."""
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
        all_dates = [d for d, _, _ in folders]
        deleted = 0
        # Keep the latest folder always.
        keep = {folders[-1][1]}
        for date, full, name in folders:
            if full in keep:
                continue
            # Keep if it is a GFS monthly or yearly keeper.
            if self._is_monthly_keep(date, all_dates, monthly_cutoff):
                continue
            if self._is_yearly_keep(date, all_dates, yearly_cutoff):
                continue
            if date < cutoff:
                try:
                    shutil.rmtree(full)
                    print(f"[cleanup] deleted local folder {full}")
                    deleted += 1
                except Exception as error:
                    print(f"[cleanup] failed to delete {full}: {error}")
        return deleted

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

    def _clean_minio(self, full_cutoff, incr_cutoff, binlog_cutoff, monthly_cutoff, yearly_cutoff):
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

        dest_cutoffs = {
            "full-backups": full_cutoff,
            "backups": incr_cutoff,
            "binlogs": binlog_cutoff,
            "monthly-backups": monthly_cutoff,
            "yearly-backups": yearly_cutoff,
        }
        deleted = 0
        for dest, cutoff in dest_cutoffs.items():
            if cutoff is None:
                # Yearly with 0 years means keep forever, skip deletion.
                continue
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
            if dest in ("full-backups", "backups", "monthly-backups", "yearly-backups"):
                folders = set()
                for key in keys:
                    parts = key[len(dest_prefix):].split("/")
                    if parts and parts[0]:
                        folders.add(parts[0])
                if folders:
                    dated = [(_parse_backup_date(f), f) for f in folders]
                    dated = [(d, f) for d, f in dated if d is not None]
                    if dated:
                        dated.sort()
                        keep_key = dest_prefix + dated[-1][1]
            for key in keys:
                if keep_key and key.startswith(keep_key):
                    continue
                rel = key[len(dest_prefix):]
                first = rel.split("/")[0] if rel else ""
                date = _parse_backup_date(first)
                if date is None:
                    continue
                # For full-backups, apply GFS keep before deleting.
                if dest == "full-backups":
                    all_dates = []
                    for k in keys:
                        r = k[len(dest_prefix):]
                        f = r.split("/")[0] if r else ""
                        d = _parse_backup_date(f)
                        if d is not None:
                            all_dates.append(d)
                    if self._is_monthly_keep(date, all_dates, monthly_cutoff):
                        continue
                    if self._is_yearly_keep(date, all_dates, yearly_cutoff):
                        continue
                if date < cutoff:
                    try:
                        s3.delete_object(Bucket=bucket, Key=key)
                        print(f"[cleanup] deleted MinIO object s3://{bucket}/{key}")
                        deleted += 1
                    except Exception as error:
                        print(f"[cleanup] failed to delete s3://{bucket}/{key}: {error}")
        return deleted
