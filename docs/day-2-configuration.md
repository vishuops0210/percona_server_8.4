# Day-2 configuration: change settings without breaking live apps

Read this before running configure, access or proxysql on a cluster with
live applications. The short version: dynamic variables do not restart
MySQL, everything else restarts one node at a time, and database or user
changes never restart MySQL.

## Dynamic variables: no restart, no app errors

Configure compares the old and new mysqld.cnf. If only these variables
changed, it applies them with SET PERSIST and skips the restart:

| Variable | Role variable |
|---|---|
| max-connections | percona_max_connections |
| long-query-time | percona_slow_query_time |
| binlog-expire-logs-seconds | percona_binlog_expire_logs_seconds |

Example: changing max_connections applies with no restart, and the
running value and file value stay in sync on all nodes.

```bash
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/configure.yml \
  --ask-vault-pass -e percona_max_connections=300
```

Warning: a plain configure run without -e renders the inventory default.
If the live value differs from the inventory default, that run counts as
a change. Update the inventory default first, or pass the intended value.

## Static variables: rolling restart, brief app errors

Every other config change uses the old path: one node at a time, wait for
ONLINE, then the next node. A restart can drop in-flight connections, so
apps need retry logic, and static changes should be planned for a
maintenance window.

Deliberately not in the dynamic set: InnoDB buffer pool size (heavy
resize), binlog format, GTID settings, read-only flags (Group Replication
manages them), require-secure-transport (could break non-TLS apps), and
all group_replication variables.

## Database and user changes: no restart

Use access.yml on the discovered writer. It never restarts MySQL.

```bash
# Add a database and a user (edit group_vars or pass a vars file):
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/access.yml --ask-vault-pass
```

- Every MySQL account host is % unless the human restricts it per entry.
- Omitting a user from the list does not delete it. To remove a user,
  list it with state absent. Same for a database, plus the drop guard.
- Drop guard: a database drop needs state absent AND
  percona_allow_database_drop=true. System databases can never be targeted.
- Tables must be created by root or an admin account. Application users
  in the examples have SELECT, INSERT, UPDATE, DELETE only, no CREATE.

For app traffic through ProxySQL, two steps are needed, in order:

1. Create the MySQL account with access.yml.
2. Add the matching ProxySQL credential with proxysql.yml.

Removing a ProxySQL credential also needs proxysql.yml with that user
listed as state absent. ProxySQL users are not removed automatically when
they disappear from the inventory list.

## ProxySQL changes: runtime, except the listener

- Users, backends, query rules and runtime variables apply through the
  admin API with LOAD TO RUNTIME and SAVE TO DISK. No restart.
- Tested: adding and removing a ProxySQL user left ProxySQL uptime
  unchanged and caused 0 app errors.
- Exception: changing proxysql_listen_address changes mysql-interfaces,
  which is restart-required. On a single proxy setup, that drops app
  connections. Plan it as a maintenance window, or use two proxies with
  a VIP so the other proxy carries traffic.

## ProxySQL pooling: no second pooler needed

ProxySQL multiplexing reuses backend MySQL connections across many
frontend connections. That is the connection pooling layer for MySQL.
PgBouncer is for Postgres, not MySQL, and adding a second pooler would add
complexity with no proven gap. Safe defaults are set in
roles/proxysql/defaults/main.yml under proxysql_mysql_variables:
connect timeout 3000 ms, charset utf8mb4, multiplexing on, frontend max
connections 1000, backend max connections 100 per server, and monitor
intervals that drive failover detection in seconds.

## No TLS for database internals: the decision

This setup runs Group Replication and application traffic without TLS,
by design. Reasons: simpler configuration, no certificate distribution or
renewal to maintain, and no TLS overhead on database traffic. GR TLS
(percona_gr_ssl) and require-secure-transport default to false.

Safety note: this assumes the database nodes, proxies and applications
are inside a private network or VPC that only trusted hosts can reach.
If that assumption stops being true, revisit TLS before exposing the
cluster further. Do not enable TLS on a running group without a plan:
it needs a rolling STOP and START of Group Replication on every member.

## Hard rules

- Ubuntu 24.04 only. Preflight enforces it.
- Replication password: at most 32 characters. Longer passwords fail
  Group Replication with MySQL error 3056. Preflight now checks this.
- Disk: at least 20 GB free on the datadir filesystem, checked by preflight.
- Group name: generate one UUID per deployment, never reuse the example.
- Single proxy is a single point of failure. Two proxies with a VIP
  remove that failure point, but do not remove the short write outage on
  writer failover.
