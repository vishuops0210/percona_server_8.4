# Verified Restore — Agent

The agent runs on the client side and takes/verifies database backups. It is a
**small core plus plugins**: the core just loads config and runs whatever
plugins it finds; every real feature is a plugin.

Written in **Python 3**. The MySQL work uses the `mysql` / `mysqldump` /
`mysqlbinlog` command-line clients (installed by the Ansible roles); only the
MinIO plugin needs an external package (`boto3`).

## Layout

```
agent.py                 core: load config, load plugins, start them, wait, stop
plugin.py                the Plugin base class (the contract every plugin follows)
plugins_loader.py        finds and loads every plugin in plugins/
percona.py               shared MySQL helpers (discovery, queries)
plugins/
  heartbeat.py           sends a heartbeat to a URL
  discovery.py           reports which node is PRIMARY / SECONDARY
  backup_mysqldump.py    full logical backup (mysqldump from a secondary)
  minio_upload.py        uploads completed backups to MinIO (S3)
  binlog_ship.py         ships rotated binlogs from the primary (for RPO)
config.json              settings for the agent and each plugin
requirements.txt         boto3 (only needed for the MinIO plugin)
```

## Where it runs

Deploy on the **ProxySQL host**, the same place the Ansible `percona_backup`
role runs. Everything the agent does to MySQL is over TCP to the Group
Replication nodes, so nothing runs on the database servers.

(When we add the XtraBackup plugin later, that one is different: XtraBackup
needs local datadir access, so it must run on a database node, not ProxySQL.)

## Install & run

```bash
cd agent
pip install -r requirements.txt     # only needed for the MinIO plugin
python3 agent.py
```

Press `Ctrl+C` to stop. Point it at another config with `--config other.json`.

## The plugins

Each plugin reads its own section of `config.json` and idles with a clear
message if that section is missing or disabled, so you can enable only what you
need.

- **discovery** — every `discovery.interval_seconds`, asks the cluster which
  node is PRIMARY and which are SECONDARY (via `replication_group_members`).
- **backup-mysqldump** — one full `mysqldump` from a secondary (falls back to
  the primary), gzipped to `backup.path/<id>/dump.sql.gz` with a `manifest.json`
  (records the source and the binlog position). Runs once at start, then every
  `backup.interval_seconds`.
- **minio-upload** — every `minio.interval_seconds`, uploads any completed
  backup folder not yet sent to the bucket, then marks it `.uploaded`.
- **binlog-ship** — every `binlog.interval_seconds`, seals the active binlog and
  downloads rotated binlogs from the primary to `binlog.path` for point-in-time
  recovery. The interval is roughly your RPO.
- **heartbeat** — POSTs an "alive" message to a URL (idle until a URL is set).

## Configuration (`config.json`)

- `mysql.client_file` — a MySQL `--defaults-file` with the backup user's
  credentials (the role writes `/etc/percona-backup/client.cnf`). No passwords
  live in `config.json`.
- `mysql.db_hosts` — the Group Replication node IPs.
- `backup.path` / `binlog.path` — local directories for dumps and binlogs.
- `minio.*` — set `enabled: true` and fill `endpoint` / `access_key` /
  `secret_key` / `bucket` to ship backups off-box.

## How to add another plugin

Drop a new file in `plugins/` with a class that inherits from `Plugin` and
implements `run(self, stop_event)`. The loader finds it automatically — no
other file changes. The next plugin planned is **XtraBackup** (physical /
incremental), which will run on a database node rather than here.
