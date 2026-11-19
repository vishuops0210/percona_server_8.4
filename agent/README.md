# Verified Restore — Agent

The agent runs on the client side and takes/verifies database backups. It is a
**small core plus plugins**: the core just loads config and runs whatever
plugins it finds; every real feature is a plugin.

Written in **Python 3**. The MySQL work uses the `mysql` / `mysqlbinlog`
command-line clients; only the MinIO plugin needs an external package
(`boto3`). XtraBackup runs on the database node, via Ansible, not on this
host.

## Layout

```
agent.py                 core: load config, load plugins, start them, wait, stop
plugin.py                the Plugin base class (the contract every plugin follows)
plugins_loader.py        finds and loads every plugin in plugins/
percona.py               shared MySQL helpers (discovery, queries)
plugins/
  heartbeat.py           sends a heartbeat to a URL
  discovery.py           reports which node is PRIMARY / SECONDARY
  backup_xtrabackup.py   remote physical backup (XtraBackup via Ansible)
  minio_upload.py        uploads completed backups to MinIO (S3)
  binlog_ship.py         ships rotated binlogs from the primary (for RPO)
config.json              settings for the agent and each plugin
requirements.txt         boto3 (only needed for the MinIO plugin)
recevo-agent.service     systemd unit for running the agent as a service
DEPLOY.md                deploy runbook for proxy1 (read this first)
```

## Where it runs

Deploy on the **ProxySQL host (proxy1)**, the separate server. The agent
identifies a SECONDARY database node and runs an Ansible playbook against
it. The playbook installs XtraBackup on the secondary, takes the backup
there, and fetches the archive back to proxy1. The agent then uploads it
to MinIO. XtraBackup reads the datadir directly, so it cannot run on
proxy1; that is why the backup work happens on the secondary.

## Setup (Ansible roles, not by hand)

Installation and setup are done by Ansible roles
(`roles/recevo_agent/` for proxy1, `roles/recevo_xtrabackup/` for the
secondary), run from `playbooks/operations/recevo-agent.yml` and
`playbooks/operations/recevo-xtrabackup.yml`. The agent itself never
installs anything. See `DEPLOY.md` for the deploy steps.

## Install & run

```bash
cd agent
pip install -r requirements.txt     # only needed for the MinIO plugin
python3 agent.py
```

Press `Ctrl+C` to stop. Point it at another config with `--config other.json`.
See `DEPLOY.md` for the full deploy (backup user, SSH, the roles, systemd).

## The plugins

Each plugin reads its own section of `config.json` and idles with a clear
message if that section is missing or disabled, so you can enable only what you
need.

- **discovery** — every `discovery.interval_seconds`, asks the cluster which
  node is PRIMARY and which are SECONDARY (via `replication_group_members`).
- **backup-xtrabackup** — full XtraBackup once a day, incremental every hour
  in between, taken on a SECONDARY via the Ansible playbook and fetched back
  to this host. Each backup lands in `backup.path/<id>/` with a
  `manifest.json` (kind, base, LSN range, binlog position). Runs once at
  start, then every `backup.interval_seconds`.
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
- `backup.path` — where backups land on proxy1.
  `backup.full_interval_seconds` is how often a full is taken; in between,
  backups are incremental. `backup.ansible_dir` and
  `backup.ansible_playbook` tell the plugin where the Ansible files live on
  proxy1 and which playbook to run.
- `backup.path` / `binlog.path` — local directories for backups and binlogs.
- `minio.*` — set `enabled: true` and fill `endpoint` / `access_key` /
  `secret_key` / `bucket` to ship backups off-box.

## How to add another plugin

Drop a new file in `plugins/` with a class that inherits from `Plugin` and
implements `run(self, stop_event)`. The loader finds it automatically — no
other file changes.
