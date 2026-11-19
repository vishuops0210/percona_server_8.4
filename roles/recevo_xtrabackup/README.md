# recevo_xtrabackup

Takes a remote XtraBackup backup on a secondary database node and fetches
it back to the control node (proxy1).

This role runs ON the secondary, invoked by the Recevo agent on proxy1 via
Ansible. The agent identifies the secondary, runs this role against it, and
the backup lands back on proxy1, where the agent uploads it to MinIO.

## What it does

- Installs Percona XtraBackup 8.4 and the MySQL client on the secondary.
- Copies `/etc/percona-backup/client.cnf` from the control node (proxy1) to
  the secondary, so XtraBackup can authenticate to the local mysqld.
- Runs XtraBackup on the secondary (full or incremental, as told by the
  agent). The backup stays on the secondary too, so the next incremental
  has a base to build on.
- Archives the backup and fetches it back to the control node
  (`/var/backup/mysql/<id>.tar.gz`).

## What it does NOT do

- It does not create the backup MySQL user. Create that by hand first (see
  `agent/DEPLOY.md`). The role only copies `client.cnf` from proxy1.
- It does not delete old backups on the secondary. The secondary keeps the
  backup chain for incrementals, so prune it by hand for now.
- It does not upload to MinIO. The agent on proxy1 does that after the
  fetch, with the minio-upload plugin.

## Requirements

- Target host is a secondary database node. XtraBackup reads the datadir
  directly, so it must run where the database lives.
- The Percona `ps-84-lts` repository is enabled on the target (it already
  is on a node running Percona Server 8.4).
- `/etc/percona-backup/client.cnf` exists on the control node (proxy1).
  The recevo_agent role writes it there.
- The backup MySQL user exists (created by hand) and has the BACKUP_ADMIN
  grant (for LOCK INSTANCE FOR BACKUP).

## Variables

| Variable | Default | Description |
|---|---|---|
| recevo_backup_id | (none, required) | Backup id, e.g. 20261119T100000_ab12cd34 |
| recevo_backup_kind | full | full or incremental |
| recevo_backup_base | (none) | Base backup id for an incremental |
| recevo_backup_path | /var/backup/mysql | Where backups land (both hosts) |
| recevo_datadir | /var/lib/mysql | Datadir XtraBackup reads (secondary) |
| recevo_client_file | /etc/percona-backup/client.cnf | Path to client.cnf (both hosts) |
| recevo_backup_user | backup | MySQL backup user (for reference) |

## Usage

Run by the Recevo agent on proxy1, not by hand. The agent passes the
backup id, kind and base as extra-vars:

```bash
ansible-playbook -i <secondary-ip>, playbooks/operations/recevo-xtrabackup.yml \
  --extra-vars "backup_id=<id> backup_kind=full"
```
