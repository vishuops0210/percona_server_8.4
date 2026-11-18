"""
MinIO upload plugin.

Uploads completed local backups to a MinIO bucket. MinIO speaks the S3 API, so
this uses boto3 (pip install boto3). Each backup folder is uploaded once; a
local ".uploaded" marker is written so it is never uploaded again.

The backup plugin writes backups to disk; this plugin ships them off-box. They
are decoupled on purpose, so a MinIO outage never blocks a backup from running.
"""

from pathlib import Path

from plugin import Plugin


class MinioUploadPlugin(Plugin):
    name = "minio-upload"

    def run(self, stop_event):
        minio_cfg = self.config.get("minio", {})
        backup_cfg = self.config.get("backup", {})
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
        backup_path = Path(backup_cfg.get("path", "/var/backup/mysql"))

        print(f"[minio] uploading new backups to s3://{bucket}/{prefix} every {interval}s")
        while not stop_event.is_set():
            try:
                self.upload_new(client, bucket, prefix, backup_path)
            except Exception as error:
                print(f"[minio] failed: {error}")
            stop_event.wait(interval)

    def upload_new(self, client, bucket, prefix, backup_path):
        if not backup_path.is_dir():
            return
        for folder in sorted(backup_path.iterdir()):
            if not folder.is_dir():
                continue
            marker = folder / ".uploaded"
            manifest = folder / "manifest.json"
            # Skip folders already uploaded, or still being written (no manifest yet).
            if marker.exists() or not manifest.exists():
                continue
            for item in folder.iterdir():
                if item.is_file() and item.name != ".uploaded":
                    key = f"{prefix}/{folder.name}/{item.name}"
                    print(f"[minio] uploading {item} -> s3://{bucket}/{key}")
                    client.upload_file(str(item), bucket, key)
            marker.write_text("ok")
            print(f"[minio] uploaded backup {folder.name}")
