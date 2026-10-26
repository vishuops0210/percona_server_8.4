# Operations runbook

Imperative procedures for the person on call. Run the commands in order; each
step says what to check before moving on.

## Daily health check

```bash
ansible-playbook -i <inventory> playbooks/operations/verify.yml --ask-vault-pass
```

It checks: group members ONLINE with one PRIMARY, ProxySQL writer hostgroup
holding exactly one online backend, application login through the proxy, a
backup newer than 30 hours, every rotated binlog archived, exporters healthy.
If it fails, go to `docs/troubleshooting.md`.

## Planned failover (move the writer)

There is no graceful "move writer" button. Stop MySQL on the current writer;
the group elects a new PRIMARY and ProxySQL follows it.

```bash
# 1. Find the writer
mysql -u root -p -e "SELECT MEMBER_HOST, MEMBER_ROLE FROM performance_schema.replication_group_members;"
# 2. Stop MySQL on the writer host
sudo systemctl stop mysql
# 3. On any surviving node, confirm the new PRIMARY (usually within a minute)
mysql -u root -p -e "SELECT MEMBER_HOST, MEMBER_ROLE FROM performance_schema.replication_group_members;"
# 4. On either proxy, confirm the writer hostgroup moved
mysql -h 127.0.0.1 -P 6032 -u admin -p -e "SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers;"
# 5. Start the old writer again; it rejoins as SECONDARY by itself
sudo systemctl start mysql
```

Application connections open during the failover drop and must reconnect.
That is expected behavior, not a bug.

## Full outage recovery (all three nodes down)

Do not bootstrap by hand unless you understand why the group stopped. The
playbooks refuse to force-bootstrap an established group on purpose.

1. Start MySQL on all three nodes: `sudo systemctl start mysql`.
2. Members with intact relay logs rejoin automatically (`group-replication-start-on-boot = ON`).
3. Run the member query. If a node stays in RECOVERING or ERROR, see
   `docs/troubleshooting.md` before touching anything else.
4. Only if every node is down with no viable relay data, bootstrap
   deliberately: on the node with the most advanced GTID set,
   `SET GLOBAL group_replication_bootstrap_group=ON; START GROUP_REPLICATION;`
   then `SET GLOBAL group_replication_bootstrap_group=OFF`, then start the
   others normally. Record why this was needed.

## Restore a backup to an isolated VM

```bash
# 1. Prepare a fresh Ubuntu 24.04 VM with Percona 8.4 installed (use the percona_server role on a host outside the group)
# 2. Copy the backup chain into its backup_path, or download from MinIO
# 3. Run the restore playbook
ansible-playbook -i <inventory> playbooks/operations/restore.yml --ask-vault-pass \
  --limit restore1 \
  -e backup_restore_target=restore1 \
  -e backup_restore_id=<backup-id> \
  -e backup_restore_workdir=/var/tmp/restore-run1
```

The target starts read-only with a new server UUID. It never joins the group
by itself. The previous datadir is preserved next to it, never deleted.

## Point-in-time recovery

1. Restore the newest full backup at or before the target time to an isolated
   VM, following the procedure above.
2. Find the archived binlogs on the backup source (`/var/backup/binlog`,
   or the `<host>-binlog` prefix in MinIO). You need every binlog written
   after the backup was taken, up to the target time.
3. Replay them in order on the isolated VM:

```bash
mysqlbinlog --stop-datetime="2026-10-19 14:30:00" \
  /var/backup/binlog/mysql-bin.000042 /var/backup/binlog/mysql-bin.000043 \
  | mysql -u root -p
```

4. Verify the data, then decide: keep it as a reporting copy, or promote it
   (out of scope for this runbook; plan the cutover with the application team).

## Add a member to the group

1. Provision a fresh Ubuntu 24.04 VM; it must have no transactions of its own.
2. Add it to `gr_members` in the inventory with a unique `percona_server_id`.
3. Give it the same `percona_group_name`.
4. Run `playbooks/operations/preflight.yml`, then `playbooks/group-two-proxies.yml`.
5. The new node joins as SECONDARY via distributed recovery. Confirm with the
   member query and `playbooks/operations/verify.yml`.

## Remove a member from the group

1. On the member: `STOP GROUP_REPLICATION;`.
2. Remove it from `gr_members` in the inventory.
3. Rerun `playbooks/operations/proxysql.yml` so the proxies drop the backend.
4. The remaining two nodes keep quorum. Do not run a two-node group for long;
   replace the member.

## Rolling configuration change

```bash
ansible-playbook -i <inventory> playbooks/operations/configure.yml --ask-vault-pass
```

MySQL restarts one node at a time and waits for each to rejoin the group.
Some settings (for example
`group-replication-ssl-mode`) need a group restart to take effect; the
playbook restarts MySQL, which restarts Group Replication with it.

## Manual backup commands

```bash
# On the backup source (db3 in HA)
sudo /usr/local/bin/percona-backup backup full
sudo /usr/local/bin/percona-backup backup incremental
sudo /usr/local/bin/percona-backup status
sudo /usr/local/bin/percona-backup prune --retention-days 7
```
