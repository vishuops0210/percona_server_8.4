"""
MinIO upload plugin.

Uploads local backup folders and binlog files to a MinIO bucket, so a local
disk failure does not lose the backups. Each upload is idempotent: a unit
already uploaded is skipped, and a unit with a marker is not uploaded again.

Config under "minio":
  enabled: true or false (default false)
  endpoint: the MinIO API URL, e.g. https://minio.ldc.opstree.dev:9000
  bucket: the MinIO bucket name
  prefix: the folder name inside the bucket, e.g. agent-proxysql-01
  access_key / secret_key: the credentials, scoped to the bucket
  interval_seconds: how often to check for new units (default 120)
  uploads: a list of jobs, each one with:
    source: a local path
    dest: a folder name under s3://<bucket>/<prefix>/
    mode: "folders" (each subfolder is a unit, needs manifest.json)
      or "files" (each file is a unit)
    delete_after_upload: if true, delete the local unit after a successful
      upload (default false)

For monthly and yearly full backups: when a full backup is the first full
of its month, it is also uploaded to the monthly-backups folder. When it is
the first full of January, it is also uploaded to the yearly-backups folder.
This is detected by the backup plugin writing a flag in the manifest, and
the minio plugin reading that flag.

Runs once on start, then every minio.interval_seconds.
"""

import os
import time

from pathlib import Path

from plugin import Plugin


class MinIOUploadPlugin(Plugin):
    name = "minio"

    def run(self, stop_event):
        minio_cfg = self.config.get("minio", {})
        if not minio_cfg.get("enabled"):
            print("[minio] disabled or no endpoint -- plugin idle")
            return
        try:
            import boto3
        except ImportError:
            print("[minio] boto3 not installed -- plugin idle")
            return
        interval = minio_cfg.get("interval_seconds", 120)
        endpoint = minio_cfg.get("endpoint", "")
        bucket = minio_cfg.get("bucket", "")
        prefix = minio_cfg.get("prefix", "")
        access_key = minio_cfg.get("access_key", "")
        secret_key = minio_cfg.get("secret_key", "")

        if not endpoint or not bucket or not access_key or not secret_key:
            print("[minio] missing endpoint, bucket or credentials -- plugin idle")
            return

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        uploads = minio_cfg.get("uploads", [])
        if not uploads:
            print("[minio] no uploads configured -- plugin idle")
            return

        while not stop_event.is_set():
            for job in uploads:
                try:
                    self._upload_job(s3, bucket, prefix, job)
                except Exception as error:
                    print(f"[minio] job failed ({job.get('source')} -> {job.get('dest')}): {error}")
            stop_event.wait(interval)

    def _upload_job(self, s3, bucket, prefix, job):
        source = Path(job.get("source", ""))
        dest = job.get("dest", "")
        mode = job.get("mode", "folders")
        delete_after = bool(job.get("delete_after_upload", False))

        if not source.is_dir():
            return
        if not dest:
            return

        base_key = f"{prefix}/{dest}" if prefix else dest

        if mode == "folders":
            for folder in sorted(source.iterdir()):
                if not folder.is_dir():
                    continue
                # A folder is ready to upload when the backup plugin has
                # finished it, which it signals by writing manifest.json.
                if not (folder / "manifest.json").exists():
                    continue
                marker = folder / ".uploaded"
                if marker.exists():
                    continue
                self._upload_folder(s3, bucket, base_key, folder)
                # For monthly and yearly fulls, also upload to those folders.
                if dest == "full-backups":
                    self._upload_monthly_yearly(s3, bucket, prefix, folder)
                marker.touch()
                print(f"[minio] uploaded {folder.name} -> s3://{bucket}/{base_key}/{folder.name}")
                if delete_after:
                    import shutil
                    shutil.rmtree(folder)
                    print(f"[minio] deleted local folder {folder} after upload")
        elif mode == "files":
            for path in sorted(source.iterdir()):
                if not path.is_file():
                    continue
                if path.suffix == ".uploaded":
                    continue
                marker = Path(str(path) + ".uploaded")
                if marker.exists():
                    continue
                key = f"{base_key}/{path.name}"
                s3.upload_file(str(path), bucket, key)
                marker.touch()
                print(f"[minio] uploaded {path.name} -> s3://{bucket}/{key}")
                if delete_after:
                    path.unlink()
                    marker.unlink()
                    print(f"[minio] deleted local file {path} after upload")

    def _upload_folder(self, s3, bucket, base_key, folder):
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(folder)
            key = f"{base_key}/{folder.name}/{rel}"
            s3.upload_file(str(path), bucket, key)

    def _upload_monthly_yearly(self, s3, bucket, prefix, folder):
        """Upload a full backup to monthly-backups and yearly-backups if it is the first of its month or year."""
        try:
            import json
            manifest = json.loads((folder / "manifest.json").read_text())
            created = manifest.get("created_utc", "")
            if not created:
                return
            from datetime import datetime
            date = datetime.fromisoformat(created)
        except Exception:
            return

        # Check if this is the first full of its month.
        monthly_prefix = f"{prefix}/monthly-backups/" if prefix else "monthly-backups/"
        monthly_key = f"{monthly_prefix}{folder.name}/"
        try:
            paginator = s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket, Prefix=monthly_prefix)
            existing_months = set()
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    rel = key[len(monthly_prefix):]
                    first = rel.split("/")[0] if rel else ""
                    if first:
                        try:
                            d = datetime.strptime(first.split("_")[0], "%Y%m%dT%H%M%S")
                            existing_months.add((d.year, d.month))
                        except Exception:
                            continue
            if (date.year, date.month) not in existing_months:
                self._upload_folder(s3, bucket, f"{prefix}/monthly-backups" if prefix else "monthly-backups", folder)
                print(f"[minio] uploaded {folder.name} -> s3://{bucket}/{prefix}/monthly-backups/{folder.name} (first full of the month)")
        except Exception as error:
            print(f"[minio] monthly check failed for {folder.name}: {error}")

        # Check if this is the first full of January.
        if date.month == 1:
            yearly_prefix = f"{prefix}/yearly-backups/" if prefix else "yearly-backups/"
            try:
                paginator = s3.get_paginator("list_objects_v2")
                pages = paginator.paginate(Bucket=bucket, Prefix=yearly_prefix)
                existing_years = set()
                for page in pages:
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        rel = key[len(yearly_prefix):]
                        first = rel.split("/")[0] if rel else ""
                        if first:
                            try:
                                d = datetime.strptime(first.split("_")[0], "%Y%m%dT%H%M%S")
                                existing_years.add(d.year)
                            except Exception:
                                continue
                if date.year not in existing_years:
                    self._upload_folder(s3, bucket, f"{prefix}/yearly-backups" if prefix else "yearly-backups", folder)
                    print(f"[minio] uploaded {folder.name} -> s3://{bucket}/{prefix}/yearly-backups/{folder.name} (first full of the year)")
            except Exception as error:
                print(f"[minio] yearly check failed for {folder.name}: {error}")
