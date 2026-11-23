"""Cleanup plugin. Deletes old backups and binlogs from local disk and MinIO.

Config under "cleanup":
  enabled: true or false
  interval: how often to run, e.g. "1d"
  rules: a list, each one with:
    source: a local path
    dest: a folder name under s3://<bucket>/<prefix>/
    keep: how long to keep, e.g. "30d", "2h"
    monthly_keep: for full backups, how long to keep the first full of each
      month, e.g. "12M". Optional.
    yearly_keep: for full backups, how long to keep the first full of January,
      e.g. "forever". Optional.

Example:
  {"source": "/var/backup/mysql-full", "dest": "full-backups", "keep": "30d", "monthly_keep": "12M", "yearly_keep": "forever"}
  {"source": "/var/backup/mysql-incremental", "dest": "backups", "keep": "30d"}
  {"source": "/var/backup/binlog", "dest": "binlogs", "keep": "2h"}

All full backups live in one folder, full-backups. The first full of each
month is kept for monthly_keep, the first full of January is kept for
yearly_keep. The rest are kept for keep. No separate monthly or yearly
folders needed.

Keeps the latest folder always, so the backup chain continues.
Logs every deletion.
"""

import os
import shutil

from datetime import datetime, timedelta

from plugin import Plugin


def _seconds(value):
    """Parse a duration like 30s, 5m, 1h, 7d, 12M into seconds. None for forever."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if value.lower() in ("forever", "never"):
        return None
    if not value:
        return None
    unit = value[-1]
    try:
        number = int(value[:-1])
    except ValueError:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
    return number * multipliers.get(unit, 1)


def _parse_date(name):
    """Parse the date from a backup folder name like 20261119T121437_08315694."""
    try:
        stamp = name.split("_")[0]
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except Exception:
        return None


class CleanupPlugin(Plugin):
    name = "cleanup"

    def run(self, stop_event):
        cfg = self.config.get("cleanup", {})
        if not cfg.get("enabled"):
            print("[cleanup] disabled -- plugin idle")
            return
        interval = _seconds(cfg.get("interval", "1d")) or 86400
        rules = cfg.get("rules", [])

        while not stop_event.wait(interval):
            try:
                self._cleanup_once(rules)
            except Exception as error:
                print(f"[cleanup] failed: {error}")

    def _cleanup_once(self, rules):
        now = datetime.now()
        deleted = {"local": 0, "minio": 0}
        for rule in rules:
            source = rule.get("source", "")
            dest = rule.get("dest", "")
            keep = _seconds(rule.get("keep", "30d"))
            monthly_keep = _seconds(rule.get("monthly_keep"))
            yearly_keep = _seconds(rule.get("yearly_keep"))
            if keep is None:
                continue
            cutoff = now - timedelta(seconds=keep)
            monthly_cutoff = None
            if monthly_keep is not None:
                monthly_cutoff = now - timedelta(seconds=monthly_keep)
            yearly_cutoff = None
            if yearly_keep is not None:
                yearly_cutoff = now - timedelta(seconds=yearly_keep)
            deleted["local"] += self._clean_local(source, cutoff, monthly_cutoff, yearly_cutoff)
            deleted["minio"] += self._clean_minio(dest, cutoff, monthly_cutoff, yearly_cutoff)
        print(f"[cleanup] done: local deleted {deleted['local']}, minio deleted {deleted['minio']}")

    def _is_first_of_month(self, date, all_dates):
        month_dates = [d for d in all_dates if (d.year, d.month) == (date.year, date.month)]
        return month_dates and date == min(month_dates)

    def _is_first_of_year(self, date, all_dates):
        if date.month != 1:
            return False
        year_dates = [d for d in all_dates if d.year == date.year and d.month == 1]
        return year_dates and date == min(year_dates)

    def _clean_local(self, path, cutoff, monthly_cutoff, yearly_cutoff):
        """Delete subfolders older than cutoff, keeping first-of-month/year and latest."""
        if not os.path.isdir(path):
            return 0
        folders = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if not os.path.isdir(full):
                continue
            date = _parse_date(name)
            if date is None:
                continue
            folders.append((date, full))
        if not folders:
            return 0
        folders.sort()
        all_dates = [d for d, _ in folders]
        deleted = 0
        keep = {folders[-1][1]}
        for date, full in folders:
            if full in keep:
                continue
            # Keep if it is the first of its month and within monthly retention.
            if monthly_cutoff is not None and date >= monthly_cutoff and self._is_first_of_month(date, all_dates):
                continue
            # Keep if it is the first of January and within yearly retention, or forever.
            if self._is_first_of_year(date, all_dates):
                if yearly_cutoff is None or date >= yearly_cutoff:
                    continue
            if date < cutoff:
                try:
                    shutil.rmtree(full)
                    print(f"[cleanup] deleted local folder {full}")
                    deleted += 1
                except Exception as error:
                    print(f"[cleanup] failed to delete {full}: {error}")
        return deleted

    def _clean_minio(self, dest, cutoff, monthly_cutoff, yearly_cutoff):
        minio_cfg = self.config.get("minio", {})
        if not minio_cfg.get("enabled"):
            return 0
        try:
            import boto3
        except ImportError:
            return 0
        endpoint = minio_cfg.get("endpoint", "")
        bucket = minio_cfg.get("bucket", "")
        prefix = minio_cfg.get("prefix", "")
        access_key = minio_cfg.get("access_key", "")
        secret_key = minio_cfg.get("secret_key", "")
        if not endpoint or not bucket or not access_key:
            return 0
        try:
            s3 = boto3.client("s3", endpoint_url=endpoint,
                              aws_access_key_id=access_key,
                              aws_secret_access_key=secret_key)
        except Exception as error:
            print(f"[cleanup] MinIO client failed: {error}")
            return 0

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
            return 0

        # Keep the latest folder.
        keep_key = None
        folders = set()
        for key in keys:
            parts = key[len(dest_prefix):].split("/")
            if parts and parts[0]:
                folders.add(parts[0])
        if folders:
            dated = [(_parse_date(f), f) for f in folders]
            dated = [(d, f) for d, f in dated if d is not None]
            if dated:
                dated.sort()
                keep_key = dest_prefix + dated[-1][1]

        deleted = 0
        all_dates = []
        for key in keys:
            rel = key[len(dest_prefix):]
            first = rel.split("/")[0] if rel else ""
            d = _parse_date(first)
            if d is not None:
                all_dates.append(d)

        for key in keys:
            if keep_key and key.startswith(keep_key):
                continue
            rel = key[len(dest_prefix):]
            first = rel.split("/")[0] if rel else ""
            date = _parse_date(first)
            if date is None:
                continue
            if monthly_cutoff is not None and date >= monthly_cutoff and self._is_first_of_month(date, all_dates):
                continue
            if self._is_first_of_year(date, all_dates):
                if yearly_cutoff is None or date >= yearly_cutoff:
                    continue
            if date < cutoff:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    print(f"[cleanup] deleted MinIO object s3://{bucket}/{key}")
                    deleted += 1
                except Exception as error:
                    print(f"[cleanup] failed to delete s3://{bucket}/{key}: {error}")
        return deleted
