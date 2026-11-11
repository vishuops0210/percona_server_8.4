# percona_backup

Back up the group with mysqldump, taken from the ProxySQL host.

```mermaid
flowchart LR
  D[Current secondary] --> B[mysqldump full] --> L[Local staging on ProxySQL host] --> M[Optional MinIO]
```

The backup runs on the ProxySQL host, never on a database node, so no
backup software is installed on any database node. On every run it finds
the current secondaries (replicas) from the group members, then takes
one consistent single-transaction mysqldump from the first one that
answers over TCP. It falls back to the primary only when no secondary
answers, so the backup read load stays off the writer. Every backup is a
standalone full dump; there is no incremental mode, so restore is a
plain SQL load. Default scheduling is daily full backups in the lab
inventory. Restore is a separate explicit playbook, never part of normal
setup. Retention is grandfather-father-son: daily full backups are kept
7 days, one full per week is kept 7 weeks, and one full per month is
kept 12 months. Weekly and monthly keepers are full snapshots only.
Binlog archiving for point-in-time recovery inside the daily window is
included, downloaded from the primary with mysqlbinlog; binlogs are not
kept for 12 months.

```yaml
backup_schedule_enabled: true
backup_storage: local
backup_path: /var/backup/mysql
backup_retention_days: 7
backup_retention_weeks: 7
backup_retention_months: 12
backup_binlog_archive_enabled: true
```

```bash
ansible-playbook -i inventories/local-dc.yml playbooks/backup.yml --ask-vault-pass --ask-become-pass
```

Manual commands on the backup source (the ProxySQL host):

```bash
sudo /usr/local/bin/percona-backup backup full
sudo /usr/local/bin/percona-backup status
sudo /usr/local/bin/percona-backup prune --retention-days 7
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
