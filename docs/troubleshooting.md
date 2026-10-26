# Troubleshooting

Symptoms first, causes second. Every fix ends with the verify step that proves
it worked.

## A member is not ONLINE

Check the state:

```sql
SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE, MEMBER_VERSION
FROM performance_schema.replication_group_members;
```

| State | Meaning | Fix |
|---|---|---|
| RECOVERING | Joining, catching up | Wait. If it never finishes, the donor may have purged the binlogs it needs; provision a fresh node instead of fighting it. |
| ERROR | Join failed | Check `/var/log/mysql/error.log` on the member. Common cause: `server_id` or `server_uuid` duplicated, or `group_replication_ip_allowlist` missing the member's address. |
| UNREACHABLE | Network or firewall | From the member: `nc -zv <peer> 33061`. Open TCP 33061 and 3306 between members. |
| OFFLINE | Group Replication stopped | `START GROUP_REPLICATION;` and watch the state. |

After the fix: all members ONLINE, one PRIMARY, then `playbooks/operations/verify.yml`.

## No PRIMARY (writes stopped)

The group lost quorum: two of three members are gone. Bring back at least
one more member so two are up, then follow "Full outage recovery" in
`docs/operations.md`. Never bootstrap a group while any old member might
still be writing; that is how split brains happen.

## ProxySQL writer hostgroup is empty or wrong

On the proxy admin interface:

```sql
SELECT * FROM monitor.mysql_server_group_replication_log ORDER BY time_start_us DESC LIMIT 5;
```

- If the monitor shows connection errors, the monitor user is broken.
  Recreate it by rerunning `playbooks/operations/proxysql.yml`; the role creates
  `proxysql_monitor` on the current writer with the needed grants.
- If the monitor is fine but the writer never moves, check
  `mysql_group_replication_hostgroups`: exactly one row, `active=1`,
  `max_writers=1`. Rerun `playbooks/operations/proxysql.yml` to reconcile it.
- Remember: ProxySQL moves the writer only after the group elects a new
  PRIMARY. Fix the group first.

## Application gets "read-only" errors after failover

The session is still pinned to the old writer. Reconnect. If the app holds a
long-lived connection pool, it must test connections on checkout or the pool
keeps serving the dead writer.

## VIP does not move to the surviving proxy

1. `systemctl status keepalived` on both proxies.
2. Check VRRP traffic: `sudo tcpdump -i <interface> vrrp`. Cloud networks
   usually block VRRP (IP protocol 112); that is why the checklist says to
   prefer the cloud load balancer there.
3. The bundled health check only watches the local ProxySQL service and port.
   It does not detect a proxy that is up but cut off from the databases.
   Test this failure mode before promising it to a client.

## Backup fails

- `Backup workflow failed: ...` with xtrabackup errors: check disk space on
  the backup source and that no other backup is running (the `.lock` file).
- MinIO upload fails: check the endpoint, bucket and keys with
  `/usr/local/bin/mc --config-dir /etc/percona-backup/mc ls backup/<bucket>/`.
- `Source identity or previous LSN changed`: take a new full backup; the
  incremental chain was broken (usually because someone restored or rebuilt
  the source).

## Restore says "No unique writable database"

The writer discovery found zero or two writers. Check the group state first;
restore targets must be isolated standalone servers, not group members.

## Playbook fails reading `query_result`

This repository pins `community.mysql 4.0.1`, whose `query_result` is a list
of lists (one sublist per query). If you change the collection version and
tasks start failing on `query_result[0][0]`, check the module's return docs
for your version before editing the tasks.

## Ansible cannot reach a host

`playbooks/operations/preflight.yml` covers the common cases. Beyond that: check the
`ansible_host` IPs in the inventory, the `ubuntu` user, and that your SSH key
is in `authorized_keys`. `host_key_checking` is on by default in
`ansible.cfg`; the first connection asks to accept the host key.
