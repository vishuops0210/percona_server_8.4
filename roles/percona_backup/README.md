# percona_backup

Back up a selected node with XtraBackup 8.4.

```mermaid
flowchart LR
  D[Database] --> B[Full / incremental] --> L[Local staging] --> M[Optional MinIO]
```

The existing backup engine keeps chain metadata and prepares working copies. Default scheduling is daily full backups in the lab inventory. Restore is a separate explicit playbook, never part of normal setup. Retention pruning and binlog archiving for point-in-time recovery are included.

```yaml
backup_schedule_enabled: true
backup_storage: local
backup_path: /var/backup/mysql
backup_retention_days: 7
backup_binlog_archive_enabled: true
```

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/operations/backup.yml --ask-vault-pass
```

Manual commands on the backup source:

```bash
sudo /usr/local/bin/percona-backup backup full
sudo /usr/local/bin/percona-backup backup incremental
sudo /usr/local/bin/percona-backup status
sudo /usr/local/bin/percona-backup prune --retention-days 7
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
