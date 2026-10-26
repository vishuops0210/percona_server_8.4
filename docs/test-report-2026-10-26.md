# Lab test report: 2026-10-26, AWS Mumbai

One proxy topology, Ubuntu 24.04, Percona Server 8.4.11-11, ProxySQL 3.0.1,
Ansible Core 2.16.3. Three database nodes (c7i-flex.large, 3812 MiB visible
memory, 40 GB root disks) plus one proxy node acting as Ansible controller.
Two test applications inserted one row per second each through ProxySQL
port 6033 into appdb.app_traffic, for the whole test window.

This is a lab validation, not a production guarantee. Single proxy is still
a single point of failure. Use two proxies with a VIP for proxy redundancy.

## Deploy

- Preflight passed with zero failures. Deploy passed with zero failures:
  db1 63 tasks, db2 49, db3 71, proxy1 24.
- Verify passed fully: 3 ONLINE members with 1 PRIMARY, exactly 1 ONLINE
  writer in ProxySQL hostgroup 10, application login through 6033, recent
  complete backup, every rotated binlog archived, mysql_up 1 on all nodes.
- First full backup is manual after deploy. The backup role installs the
  daily cron, it does not run the first backup for you. Backup used here:
  20261026T075709_03cb74fc15, full, GTID
  8ed0516a-0c1f-493e-a9d6-eea569045ff1:1-2681:1000001-1000768.

## Bugs found and fixed in this run

1. Replication password over 32 characters fails Group Replication with
   MySQL error 3056. Preflight now asserts the replication password is at
   most 32 characters, so this fails before deploy, not during it.
2. Verify compared ProxySQL hostgroup_id as an integer, ProxySQL returns
   it as a string, so the writer check could never pass. Verify now
   compares as a string.
3. Configure restarted MySQL on every config change, even for dynamic
   variables. Configure now applies max-connections, long-query-time and
   binlog-expire-logs-seconds with SET PERSIST and no restart. Every other
   config change still uses the old rolling restart path.

## Config rerun: no downtime for dynamic variables

- No-change configure rerun: changed 0 on all nodes, no restart, 0 app errors.
- Before the fix, max_connections 150 to 200 restarted all 3 nodes and
  caused 2 lost-connection errors per app (error 2013, one at writer
  restart, one at first secondary restart). Apps auto-recovered each time.
- After the fix, max_connections 250 to 300: 0 restarts, 0 new app errors,
  running value 300 and file value 300 on all 3 nodes.
- A plain configure run without -e would now render the inventory default
  150 and count as a dynamic-only change back to 150. Always pass the
  intended value, or update the inventory default first.

## Failover while apps were running

Writer stop (systemctl stop mysql on db1, then current PRIMARY):

- Apps: 1 failed insert per app, next successful insert about 7 seconds
  after the last successful insert, app retry logged as recovered after
  2.0 seconds. No other errors.
- Group: db2 elected PRIMARY, db3 SECONDARY, db1 gone from the member list.
- ProxySQL: writer hostgroup 10 moved to db2, db1 moved to offline
  hostgroup 40, readers stayed db2 and db3 in hostgroup 20.
- Restart db1: it rejoined as SECONDARY ONLINE with no Ansible run, db2
  stayed PRIMARY, 0 new app errors.

Reader stop (systemctl stop mysql on db3):

- 0 app errors. Restart db3: it rejoined as SECONDARY ONLINE, 0 app errors.
- This was a graceful stop, not a hard crash or power loss. For a hard
  crash, keep the README estimate of roughly 15 to 30 seconds of write
  outage until a hard-crash drill proves otherwise.

## Day-2 database and user operations while apps were running

- Add database testdb2 and user testuser2 with access.yml: succeeded,
  writer discovered as db2 (current PRIMARY, not hardcoded db1).
- Direct login as testuser2 to db2:3306: INSERT and SELECT succeeded.
- Add testuser2 to ProxySQL with proxysql.yml: succeeded, ProxySQL
  service uptime unchanged (no restart), login through 6033 succeeded,
  apps saw 0 new errors.
- Remove both with access.yml (database state absent plus
  percona_allow_database_drop=true, user state absent) and proxysql.yml
  (user state absent): verified gone from MySQL and from ProxySQL runtime
  users, apps saw 0 new errors.
- ProxySQL user addition needs two steps: create the MySQL account with
  access.yml, then add the matching ProxySQL credential with proxysql.yml.
  One step alone is not enough for app traffic through ProxySQL.
- A ProxySQL rerun with no listener change does not restart ProxySQL.
  Changing proxysql_listen_address does require a restart, because
  mysql-interfaces is a restart-required variable. Plan that change as a
  maintenance window on a single proxy setup.

## Backup restore

- Restore target: a separate disposable Ubuntu 24.04 VM with Percona
  8.4.11-11 installed, not a group member. Backup chain copied from db3
  to the restore target, restore.yml run with the backup id above.
- Restore playbook passed with zero failures. Restored server: new UUID,
  read_only 1, GTID exactly matching the backup, app_traffic count 3425
  with max id 3425, matching the backup-time state.
- Live cluster was unchanged during restore: apps continued, 0 new errors.
- Restore target was terminated after the test. It is disposable by design.

## Rules proven in this run

- Ubuntu 24.04 only. Preflight enforces it.
- Replication password: at most 32 characters, or error 3056.
- Disk: preflight needs at least 20 GB free on the datadir filesystem.
  This lab used 40 GB root disks, 36 GB free after growing.
- Memory: 3812 MiB visible on c7i-flex.large passes the 3584 MiB check.
- No TLS for database internals in this setup, by design. GR TLS and
  require-secure-transport default to off. See docs/day-2-configuration.md
  for the reasoning and the VPC-only safety note.
- No second connection pooler. ProxySQL multiplexing is the pooling layer
  for MySQL. PgBouncer is for Postgres, not MySQL.
