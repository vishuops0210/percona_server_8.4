"""Cleanup plugin. Deletes old backups and binlogs from local disk and MinIO.

Config under "cleanup":
  enabled: true or false
  interval: how often to run, e.g. "1d"
  rules: a list, each one with:
    source: a local path
    dest: a folder name under s3://<bucket>/<prefix>/
    keep: how long to keep, e.g. "30d", "2h"
    monthly_keep: for full backups, how long to keep the first full of each
      month, e.g. "12mo". Optional.
    yearly_keep: for full backups, how long to keep the first full of January,
      e.g. "forever". Optional.

Example:
  {"source": "/var/backup/mysql-full", "dest": "full-backups", "keep": "30d", "monthly_keep": "12mo", "yearly_keep": "forever"}
  {"source": "/var/backup/mysql-incremental", "dest": "backups", "keep": "30d"}
  {"source": "/var/backup/binlog", "dest": "binlogs", "keep": "2h"}

Bulk chain deletion: a full and all its incrementals are deleted together,
when the last incremental in that chain is older than keep. An incremental
is useless without its base full, so we never delete part of a chain.
We wait until the whole chain is old enough, then delete it in bulk.

Keeps the latest chain always. Logs every deletion.
"""

import os
import shutil

from datetime import datetime, timedelta

from plugin import Plugin


def _seconds(value):
    """Parse a duration like 30s, 5m, 1h, 7d, 12mo into seconds. None for forever."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    value = str(value).strip()
    if value.lower() in ("forever", "never"):
        return None
    if not value:
        return None
    lower = value.lower()
    if lower.endswith("mo") or lower.endswith("mon"):
        try:
            number = int(value[:-2] if lower.endswith("mo") else value[:-3])
            return number * 2592000
        except ValueError:
            return None
    if lower.endswith("min"):
        try:
            number = int(value[:-3])
            return number * 60
        except ValueError:
            return None
    unit = value[-1]
    try:
        number = int(value[:-1])
    except ValueError:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
    return number * multipliers.get(unit, 1)


def _parse_date(name):
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
        full_rule = next((r for r in rules if r.get("dest") == "full-backups"), None)
        incr_rule = next((r for r in rules if r.get("dest") == "backups"), None)
        if full_rule and incr_rule:
            keep = _seconds(full_rule.get("keep", "30d"))
            incr_keep = _seconds(incr_rule.get("keep", "30d"))
            chain_keep = max(keep or 0, incr_keep or 0)
            monthly_keep = _seconds(full_rule.get("monthly_keep"))
            yearly_keep = _seconds(full_rule.get("yearly_keep"))
            cutoff = now - timedelta(seconds=chain_keep) if chain_keep else None
            monthly_cutoff = now - timedelta(seconds=monthly_keep) if monthly_keep is not None else None
            yearly_cutoff = now - timedelta(seconds=yearly_keep) if yearly_keep is not None else None
            if cutoff is not None:
                deleted["local"] += self._clean_chains_local(full_rule.get("source", ""), incr_rule.get("source", ""), cutoff, monthly_cutoff, yearly_cutoff)
                deleted["minio"] += self._clean_chains_minio("full-backups", "backups", cutoff, monthly_cutoff, yearly_cutoff)
        for rule in rules:
            if rule.get("dest") in ("full-backups", "backups"):
                continue
            keep = _seconds(rule.get("keep", "30d"))
            if keep is None:
                continue
            cutoff = now - timedelta(seconds=keep)
            deleted["local"] += self._clean_files(rule.get("source", ""), cutoff)
            deleted["minio"] += self._clean_minio_files(rule.get("dest", ""), cutoff)
        print(f"[cleanup] done: local deleted {deleted['local']}, minio deleted {deleted['minio']}")

    def _is_first_of_month(self, date, all_dates):
        month_dates = [d for d in all_dates if (d.year, d.month) == (date.year, date.month)]
        return month_dates and date == min(month_dates)

    def _is_first_of_year(self, date, all_dates):
        if date.month != 1:
            return False
        year_dates = [d for d in all_dates if d.year == date.year and d.month == 1]
        return year_dates and date == min(year_dates)

    def _clean_chains_local(self, full_path, incr_path, cutoff, monthly_cutoff, yearly_cutoff):
        """Delete full chains in bulk. Group incrementals by the most recent full before them."""
        # Load fulls.
        fulls = []
        if os.path.isdir(full_path):
            for name in os.listdir(full_path):
                full = os.path.join(full_path, name)
                if not os.path.isdir(full):
                    continue
                date = _parse_date(name)
                if date is None:
                    continue
                fulls.append((date, name, full))
        fulls.sort()
        if not fulls:
            return 0
        # Load incrementals.
        incrs = []
        if os.path.isdir(incr_path):
            for name in os.listdir(incr_path):
                full = os.path.join(incr_path, name)
                if not os.path.isdir(full):
                    continue
                date = _parse_date(name)
                if date is None:
                    continue
                incrs.append((date, name, full))
        incrs.sort()
        # Group incrementals by their base full: the most recent full before them.
        chains = {name: {"full": (date, path), "incrs": []} for date, name, path in fulls}
        for incr_date, incr_name, incr_path in incrs:
            base = None
            for full_date, full_name, _ in fulls:
                if full_date <= incr_date:
                    base = full_name
                else:
                    break
            if base and base in chains:
                chains[base]["incrs"].append((incr_date, incr_path))
            else:
                chains[incr_name] = {"full": None, "incrs": [(incr_date, incr_path)]}
        # Find the latest date overall, keep that chain.
        all_dates = [d for d, _, _ in fulls] + [d for d, _, _ in incrs]
        if not all_dates:
            return 0
        latest_date = max(all_dates)
        deleted = 0
        for full_name, chain in chains.items():
            chain_dates = []
            if chain["full"]:
                chain_dates.append(chain["full"][0])
            for date, _ in chain["incrs"]:
                chain_dates.append(date)
            if not chain_dates:
                continue
            latest_in_chain = max(chain_dates)
            if latest_in_chain == latest_date:
                continue
            if chain["full"]:
                full_date = chain["full"][0]
                if monthly_cutoff is not None and full_date >= monthly_cutoff and self._is_first_of_month(full_date, all_dates):
                    continue
                if self._is_first_of_year(full_date, all_dates):
                    if yearly_cutoff is None or full_date >= yearly_cutoff:
                        continue
            if latest_in_chain < cutoff:
                for _, incr_path in chain["incrs"]:
                    try:
                        shutil.rmtree(incr_path)
                        print(f"[cleanup] deleted local incremental {incr_path} (chain {full_name})")
                        deleted += 1
                    except Exception as error:
                        print(f"[cleanup] failed to delete {incr_path}: {error}")
                if chain["full"]:
                    try:
                        shutil.rmtree(chain["full"][1])
                        print(f"[cleanup] deleted local full {chain['full'][1]} (chain {full_name})")
                        deleted += 1
                    except Exception as error:
                        print(f"[cleanup] failed to delete {chain['full'][1]}: {error}")
        return deleted

    def _clean_files(self, path, cutoff):
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

    def _clean_chains_minio(self, full_dest, incr_dest, cutoff, monthly_cutoff, yearly_cutoff):
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
            s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        except Exception as error:
            print(f"[cleanup] MinIO client failed: {error}")
            return 0

        def _list(dest):
            dest_prefix = f"{prefix}/{dest}/" if prefix else f"{dest}/"
            try:
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
                                folders.setdefault(first, {"date": date, "keys": []})["keys"].append(key)
                return folders
            except Exception as error:
                print(f"[cleanup] MinIO list failed for {dest_prefix}: {error}")
                return {}

        full_folders = _list(full_dest)
        incr_folders = _list(incr_dest)
        if not full_folders and not incr_folders:
            return 0
        full_dates = sorted([(v["date"], k) for k, v in full_folders.items()])
        chains = {k: {"date": v["date"], "keys": list(v["keys"])} for k, v in full_folders.items()}
        for incr_name, info in incr_folders.items():
            incr_date = info["date"]
            base = None
            for full_date, full_name in full_dates:
                if full_date <= incr_date:
                    base = full_name
                else:
                    break
            if base and base in chains:
                chains[base]["keys"].extend(info["keys"])
            else:
                chains[incr_name] = {"date": incr_date, "keys": list(info["keys"])}
        all_dates = [v["date"] for v in full_folders.values()] + [v["date"] for v in incr_folders.values()]
        if not all_dates:
            return 0
        latest_date = max(all_dates)
        deleted = 0
        for full_name, chain in chains.items():
            # Latest date in this chain.
            chain_dates = [chain["date"]]
            for key in chain["keys"]:
                parts = key.split("/")
                folder = parts[-2] if len(parts) >= 2 else ""
                d = _parse_date(folder)
                if d is not None:
                    chain_dates.append(d)
            latest_in_chain = max(chain_dates) if chain_dates else chain["date"]
            if latest_in_chain == latest_date:
                continue
            full_date = chain["date"]
            if monthly_cutoff is not None and full_date >= monthly_cutoff and self._is_first_of_month(full_date, all_dates):
                continue
            if self._is_first_of_year(full_date, all_dates):
                if yearly_cutoff is None or full_date >= yearly_cutoff:
                    continue
            if latest_in_chain < cutoff:
                for key in chain["keys"]:
                    try:
                        s3.delete_object(Bucket=bucket, Key=key)
                        print(f"[cleanup] deleted MinIO object s3://{bucket}/{key} (chain {full_name})")
                        deleted += 1
                    except Exception as error:
                        print(f"[cleanup] failed to delete s3://{bucket}/{key}: {error}")
        return deleted

    def _clean_minio_files(self, dest, cutoff):
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
            s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key)
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
        deleted = 0
        for key in keys:
            rel = key[len(dest_prefix):]
            first = rel.split("/")[0] if rel else ""
            date = _parse_date(first)
            if date is None:
                continue
            if date < cutoff:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    print(f"[cleanup] deleted MinIO object s3://{bucket}/{key}")
                    deleted += 1
                except Exception as error:
                    print(f"[cleanup] failed to delete s3://{bucket}/{key}: {error}")
        return deleted
