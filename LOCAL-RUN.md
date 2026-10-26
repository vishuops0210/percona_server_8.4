# Local run: 4 servers

Run the whole HA stack on 4 VMs with 2 vCPU and 8 GB RAM each: three Percona 8.4 Group Replication nodes plus one ProxySQL. The reference example uses five VMs (it adds a second proxy); everything else here is identical.

| Host | Role | Example IP |
|---|---|---|
| db1 | Percona 8.4, group seed | 192.0.2.11 |
| db2 | Percona 8.4, group member | 192.0.2.12 |
| db3 | Percona 8.4, group member, backup source | 192.0.2.13 |
| proxy1 | ProxySQL 3.0.1 | 192.0.2.21 |

## 1. Prepare the servers

Each VM: fresh Ubuntu 22.04 or 24.04 amd64, user `ubuntu` with passwordless sudo, your SSH public key in `~/.ssh/authorized_keys`, outbound internet for packages.

Open these ports (security group or host firewall):

| From | To | Port |
|---|---|---|
| db1, db2, db3 | db1, db2, db3 | TCP 3306, 33061 |
| proxy1 | db1, db2, db3 | TCP 3306 |
| your laptop / app | proxy1 | TCP 6033 |
| prometheus | db1, db2, db3 | TCP 9104 |

Port 6032 (ProxySQL admin) stays on localhost; never expose it.

With one proxy there is no VIP in this lab, so `proxysql_ha_enabled` stays `false`. Keepalived and the second proxy are client-environment pieces, not lab pieces.

## 2. Control node

Your laptop needs Ansible Core 2.16 or newer:

```bash
ansible-galaxy collection install -r requirements.yml
```

## 3. Inventory, group name, secrets

Edit `inventories/group-one-proxy.yml` and replace the four example IPs with your real ones.

Give this cluster its own group UUID in `inventories/group_vars/all/main.yml`:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

Create the vault file from the example and fill in your own passwords:

```bash
ansible-vault create inventories/group_vars/all/vault.yml
```

Copy the key names from `inventories/group_vars/all/vault.example.yml`. Never paste these passwords into chat or tickets.

Check reachability, then fail fast on inventory mistakes before anything is installed:

```bash
ansible -i inventories/group-one-proxy.yml all -m ping
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/preflight.yml --ask-vault-pass
```

## 4. Run the playbook

```bash
ansible-playbook -i inventories/group-one-proxy.yml playbooks/group-one-proxy.yml --ask-vault-pass
```

Do not add `--limit`. Bootstrap needs the full group inventory in one run. The first run takes a while (Percona 8.4, XtraBackup and ProxySQL packages).

## 5. Verify everything in one command

```bash
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/verify.yml --ask-vault-pass
```

It checks group membership, ProxySQL writer routing, application login through the proxy, backup freshness, binlog archiving and exporters. If it passes, the manual checks below should pass too; they are still worth doing once so you know what healthy looks like.

## 6. Verify the group

On any db node (`mysql -u root -p`):

```sql
SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE
FROM performance_schema.replication_group_members;
```

Expect three rows, all `ONLINE`, one `PRIMARY` and two `SECONDARY`.

## 7. Verify routing through the proxy

```bash
mysql -h <proxy1-ip> -P 6033 -u appuser -p appdb
```

```sql
CREATE TABLE demo (id INT PRIMARY KEY, note VARCHAR(100));
INSERT INTO demo VALUES (1, 'hello from Group Replication');
SELECT @@hostname;
SELECT * FROM appdb.demo;
```

`SELECT @@hostname` tells you which db node served the write. Application tables need primary keys; Group Replication requires them.

## 8. Verify metrics and backup

```bash
curl http://<db-ip>:9104/metrics | head -20
```

On db3:

```bash
sudo /usr/local/bin/percona-backup backup full
```

## 9. Failover drill

This is the point of the lab. Do it at least once.

1. Find the writer from the step 6 query.
2. On the writer: `sudo systemctl stop mysql`.
3. On a surviving node, rerun the step 6 query. A new `PRIMARY` should appear, usually within a minute.
4. On proxy1, check the admin interface: `mysql -h 127.0.0.1 -P 6032 -u admin -p`, then `SELECT hostgroup_id, hostname, status FROM runtime_mysql_servers;`. Hostgroup 10 (writer) should now list the new primary.
5. Reconnect your step 7 session (active connections drop on failover; that is expected behavior, and apps must reconnect) and insert another row.
6. On the stopped node: `sudo systemctl start mysql`. It rejoins as `SECONDARY` by itself. Confirm three `ONLINE` members again.

## 10. Reset

The playbooks will not re-bootstrap an established group. To start over, reimage the three db VMs (or `apt purge percona-server-server`, wipe `/var/lib/mysql`, then rerun). Fresh VMs are the predictable path.

## 11. Note on `query_result` shape

Several tasks index the MySQL query result as a list of lists (`query_result[0][0].gtid`, `selectattr('query_result.0', ...)`). That matches the pinned `community.mysql 4.0.1` module source, which documents `query_result` as "list of lists (sublist for each query)". Do not upgrade the collection without re-checking: if a newer release changes the shape, writer discovery and group bootstrap break. `requirements.yml` pins the version for exactly this reason.

## 12. What this lab deliberately skips

Second ProxySQL, keepalived VIP, binlog archiving/PITR, backup retention, cross-site DR, alert rules, rolling upgrades. Those are deferred in the main README; prove the core here first.
