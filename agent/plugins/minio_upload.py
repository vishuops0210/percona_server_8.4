"""
MinIO upload plugin, generic.

Uploads local folders to a MinIO bucket. MinIO speaks the S3 API, so this
uses boto3 (pip install boto3). What to upload is driven by the config, not
hardcoded in the plugin, so the same plugin can ship backups, binlogs, or
anything else.

Config, under "minio":
  enabled, endpoint, access_key, secret_key, bucket, prefix,
  interval_seconds, and uploads, a list of jobs. Each job has:
    source: local path to upload from
    dest:   destination prefix inside s3://<bucket>/<prefix>/
    mode:   "folders" or "files"
      folders: each subdirectory of source is one upload unit. A unit is
        uploaded only when it has a manifest.json, and a local ".uploaded"
        marker is written so it is never uploaded again. All files inside
        the unit go to <prefix>/<dest>/<unit>/<file>.
      files: each file in source is one upload unit. A "<file>.uploaded"
        marker is written after upload so it is never uploaded again.
        Files go to <prefix>/<dest>/<file>.
    kinds: optional list, folders mode only. When set, only upload folders
      whose manifest.json "kind" is in this list. For example
      ["incremental"] uploads only incremental backups and skips full ones,
      so full backups can be handled by a separate plugin later. When
      omitted, all folders with a manifest are uploaded.
    delete_after_upload: true or false (default false). When true, the local
      folder or file is deleted after a successful upload, to free disk
      space. When false, the local copy is kept. This is a per-job choice,
      so backups can be kept locally while binlogs are deleted, or the
      other way around.

Example config:
  "minio": {
    "enabled": true,
    "endpoint": "https://minio.ldc.opstree.dev:9000",
    "bucket": "automation-percona-backup",
    "prefix": "agent-proxysql-01",
    "interval_seconds": 120,
    "uploads": [
      {"source": "/var/backup/mysql", "dest": "backups", "mode": "folders", "delete_after_upload": false, "kinds": ["incremental"]},
      {"source": "/var/backup/binlog", "dest": "binlogs", "mode": "files", "delete_after_upload": true}
    ]
  }

The backup plugin writes backups to disk; this plugin ships them off-box. They
are decoupled on purpose, so a MinIO outage never blocks a backup from running.
"""

import json
import shutil
from pathlib import Path

from plugin import Plugin


class MinioUploadPlugin(Plugin):
    name = "minio-upload"

    def run(self, stop_event):
        minio_cfg = self.config.get("minio", {})
        interval = minio_cfg.get("interval_seconds", 120)

        if not minio_cfg.get("enabled") or not minio_cfg.get("endpoint"):
            print("[minio] disabled or no endpoint -- plugin idle")
            return

        # Import boto3 here so the agent still runs if boto3 is not installed.
        try:
            import boto3
        except ImportError:
            print("[minio] boto3 not installed (pip install boto3) -- plugin idle")
            return

        client = boto3.client(
            "s3",
            endpoint_url=minio_cfg["endpoint"],
            aws_access_key_id=minio_cfg.get("access_key"),
            aws_secret_access_key=minio_cfg.get("secret_key"),
        )
        bucket = minio_cfg.get("bucket", "percona-backup")
        prefix = minio_cfg.get("prefix", self.config.get("agent_id", "agent"))
        uploads = minio_cfg.get("uploads", [])

        print(f"[minio] uploading to s3://{bucket}/{prefix} every {interval}s")
        while not stop_event.is_set():
            try:
                for job in uploads:
                    self.upload_job(client, bucket, prefix, job)
            except Exception as error:
                print(f"[minio] failed: {error}")
            stop_event.wait(interval)

    def upload_job(self, client, bucket, prefix, job):
        source = Path(job.get("source", ""))
        dest = job.get("dest", "").strip("/")
        mode = job.get("mode", "files")
        delete_after = job.get("delete_after_upload", False)
        kinds = job.get("kinds")
        if not source.is_dir():
            return
        if mode == "folders":
            self._upload_folders(client, bucket, prefix, source, dest, delete_after, kinds)
        else:
            self._upload_files(client, bucket, prefix, source, dest, delete_after)

    def _upload_folders(self, client, bucket, prefix, source, dest, delete_after, kinds):
        for folder in sorted(source.iterdir()):
            if not folder.is_dir():
                continue
            marker = folder / ".uploaded"
            manifest = folder / "manifest.json"
            # Skip folders already uploaded, or still being written (no manifest yet).
            if marker.exists() or not manifest.exists():
                continue
            # When kinds is set, only upload folders whose manifest kind matches.
            if kinds is not None:
                try:
                    kind = json.loads(manifest.read_text()).get("kind")
                except Exception:
                    kind = None
                if kind not in kinds:
                    continue
            for item in folder.iterdir():
                if item.is_file() and item.name != ".uploaded":
                    key = f"{prefix}/{dest}/{folder.name}/{item.name}"
                    print(f"[minio] uploading {item} -> s3://{bucket}/{key}")
                    client.upload_file(str(item), bucket, key)
            marker.write_text("ok")
            print(f"[minio] uploaded {folder.name} to {dest}")
            if delete_after:
                shutil.rmtree(folder)
                print(f"[minio] deleted local folder {folder} after upload")

    def _upload_files(self, client, bucket, prefix, source, dest, delete_after):
        for item in sorted(source.iterdir()):
            if not item.is_file():
                continue
            if item.name.endswith(".uploaded"):
                continue
            marker = Path(str(item) + ".uploaded")
            if marker.exists():
                continue
            key = f"{prefix}/{dest}/{item.name}"
            print(f"[minio] uploading {item} -> s3://{bucket}/{key}")
            client.upload_file(str(item), bucket, key)
            marker.write_text("ok")
            print(f"[minio] uploaded {item.name} to {dest}")
            if delete_after:
                item.unlink()
                marker.unlink()
                print(f"[minio] deleted local file {item} after upload")
