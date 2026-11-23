"""MinIO upload plugin. Uploads local folders and files to MinIO.

Config under "minio":
  enabled: true or false
  endpoint, bucket, prefix, access_key, secret_key
  interval: how often to check, e.g. "2m"
  uploads: a list, each one with:
    source: a local path
    dest: a folder name under s3://<bucket>/<prefix>/
    mode: "folders" or "files"
    delete_after_upload: if true, delete the local unit after upload

All full backups go to one folder, full-backups. No separate monthly or
yearly folders. The cleanup plugin keeps the first of each month and year
longer, from the same folder.
"""

import os

from pathlib import Path

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
    # Check for multi-char units first: mo, min, mon.
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
        interval = _seconds(minio_cfg.get("interval", "2m"), 120)
        endpoint = minio_cfg.get("endpoint", "")
        bucket = minio_cfg.get("bucket", "")
        prefix = minio_cfg.get("prefix", "")
        access_key = minio_cfg.get("access_key", "")
        secret_key = minio_cfg.get("secret_key", "")

        if not endpoint or not bucket or not access_key or not secret_key:
            print("[minio] missing endpoint, bucket or credentials -- plugin idle")
            return

        s3 = boto3.client("s3", endpoint_url=endpoint,
                          aws_access_key_id=access_key,
                          aws_secret_access_key=secret_key)
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

        if not source.is_dir() or not dest:
            return

        base_key = f"{prefix}/{dest}" if prefix else dest

        if mode == "folders":
            for folder in sorted(source.iterdir()):
                if not folder.is_dir():
                    continue
                if not (folder / "manifest.json").exists():
                    continue
                marker = folder / ".uploaded"
                if marker.exists():
                    continue
                self._upload_folder(s3, bucket, base_key, folder)
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

