```bash
# CONTROLLER — run from the project directory; use your actual private-key path.
cd ~/percona_server
ansible --version
ansible-config dump --only-changed
ansible-inventory -i inventories/group-one-proxy.yml --graph

# Install required collections; check which MySQL module is loaded.
ansible-galaxy collection install -r requirements.yml
ansible-galaxy collection list community.mysql
ansible-galaxy collection list community.proxysql
ansible-doc -t module community.mysql.mysql_user | head -5
ansible-doc -t module community.mysql.mysql_user | grep -A 5 'salt'

# First SSH connection: verify each fingerprint before accepting it.
ssh -i ~/.ssh/default ubuntu@one.percona.internal
# Run exit to return to the controller; repeat for the remaining hosts.
exit
ssh -i ~/.ssh/default ubuntu@two.percona.internal
exit
ssh -i ~/.ssh/default ubuntu@three.percona.internal
exit
ssh -i ~/.ssh/default ubuntu@proxysql.percona.internal
exit

# Connectivity: --private-key is optional when already set in ansible.cfg.
ansible all -m ping --ask-vault-pass
ansible all --private-key ~/.ssh/default -m ping --ask-vault-pass

# Python troubleshooting; replace db2 with the affected inventory host.
ansible db2 -m raw -a 'command -v python3' --ask-vault-pass
# Only if Python is missing:
ansible db2 -b -m raw -a 'apt-get update && apt-get install -y python3' --ask-vault-pass
# Set ansible_python_interpreter: /usr/bin/python3 in group_vars/all/main.yml if needed.
nano inventories/group_vars/all/main.yml

# RAM and disk on database hosts.
ansible database -m setup -a 'filter=ansible_memtotal_mb' --ask-vault-pass
ansible database -m command -a 'free -h' --ask-vault-pass
ansible database -m command -a 'df -h /var/lib/mysql' --ask-vault-pass

# Secrets: run create only if vault.yml does not exist.
# YAML files in group_vars/all/ are ALL loaded, including vault.example.yml.
# Keep the example outside that directory when using a separate vault.yml.
ansible-vault create inventories/group_vars/all/vault.yml
# Existing plaintext secrets file: encrypt once.
ansible-vault encrypt inventories/group_vars/all/vault.yml
# Existing encrypted file: edit without printing passwords into terminal logs.
ansible-vault edit inventories/group_vars/all/vault.yml

# Preflight and deployment — choose ONE topology, not both.
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/preflight.yml --ask-vault-pass
ansible-playbook -i inventories/group-one-proxy.yml playbooks/group-one-proxy.yml --ask-vault-pass
# Alternative: two proxies, with its own inventory.
ansible-playbook -i inventories/group-two-proxies.yml playbooks/operations/preflight.yml --ask-vault-pass
ansible-playbook -i inventories/group-two-proxies.yml playbooks/group-two-proxies.yml --ask-vault-pass

# Later operations — these use the default inventory in ansible.cfg.
ansible-playbook playbooks/operations/access.yml --ask-vault-pass
ansible-playbook playbooks/operations/configure.yml --ask-vault-pass
ansible-playbook playbooks/operations/proxysql.yml --ask-vault-pass
ansible-playbook playbooks/operations/backup.yml --ask-vault-pass
ansible-playbook playbooks/operations/monitoring.yml --ask-vault-pass
# Verify expects a completed recent backup.
ansible-playbook playbooks/operations/verify.yml --ask-vault-pass

# Proxy-facing accounts use the '%' host wildcard, so IP changes need no account updates.
nano inventories/group_vars/proxies.yml
# After credential changes:
ansible-playbook playbooks/operations/proxysql.yml --ask-vault-pass
```

```bash
# PROXY VM — DNS, service, ports and logs.
getent ahostsv4 one.percona.internal
getent ahostsv4 two.percona.internal
getent ahostsv4 three.percona.internal
getent ahostsv4 proxysql.percona.internal
sudo systemctl is-active proxysql
sudo systemctl status proxysql --no-pager
sudo ss -ltnp | grep -E ':6032|:6033'
sudo journalctl -u proxysql -n 100 --no-pager
sudo tail -n 100 /var/lib/proxysql/proxysql.log
# Inspect logs locally; redact credentials before sharing output.

# Inspect the source IP selected toward a database.
DB_IP=$(getent ahostsv4 one.percona.internal | awk 'NR==1 {print $1}')
ip route get "$DB_IP"

# ADMIN CONSOLE: port 6032, vault_proxysql_admin_password at the prompt.
mysql --protocol=TCP -h 127.0.0.1 -P 6032 -u admin -p
```

```sql
-- PROXYSQL ADMIN ONLY (6032). Finish each statement with a semicolon.
SELECT hostgroup_id, hostname, port, status
FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname;
-- 10=writer, 20=readers, 30=extra writers, 40=excluded from normal routing.

SELECT * FROM runtime_mysql_group_replication_hostgroups;

SELECT rule_id, active, match_pattern, destination_hostgroup
FROM runtime_mysql_query_rules ORDER BY rule_id;

SELECT hostgroup, srv_host, status, ConnUsed, ConnFree, Queries
FROM stats_mysql_connection_pool;

SELECT hostname, viable_candidate, read_only, transactions_behind, error
FROM monitor.mysql_server_group_replication_log
ORDER BY time_start_us DESC LIMIT 9;

SELECT hostname, connect_error
FROM monitor.mysql_server_connect_log
ORDER BY time_start_us DESC LIMIT 9;

SELECT username, active, default_hostgroup
FROM runtime_mysql_users;

SELECT variable_name, variable_value FROM runtime_global_variables
WHERE variable_name IN ('mysql-monitor_username', 'mysql-interfaces', 'admin-mysql_ifaces');
exit
```

```bash
# APPLICATION CONNECTION: port 6033, vault_app_password at the prompt.
# ProxySQL admin tables do NOT exist through this connection.
mysql --protocol=TCP -h proxysql.percona.internal -P 6033 -u appuser -p appdb
```

```sql
-- APPLICATION SESSION (6033).
SELECT DATABASE(), @@hostname AS connected_server, @@server_uuid, @@read_only;
SHOW TABLES;
-- Replace your_table with an existing application table.
SELECT * FROM your_table LIMIT 10;
exit
```

```bash
# DATABASE VM — service and local administrator login.
sudo systemctl is-active mysql
sudo systemctl status mysql --no-pager
sudo journalctl -u mysql -n 100 --no-pager
sudo tail -n 100 /var/log/mysql/error.log
sudo mysql -u root -p
```

```sql
-- DATABASE ADMIN: local socket, vault_percona_root_password.
SELECT @@hostname, @@server_uuid, @@read_only, @@super_read_only;
SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE
FROM performance_schema.replication_group_members;
SHOW DATABASES;
SHOW TABLES FROM appdb;
SELECT user, host, plugin FROM mysql.user
WHERE user IN ('proxysql_monitor', 'appuser', 'mysqld_exporter', 'backup');
SHOW VARIABLES LIKE 'skip_name_resolve';
SHOW VARIABLES LIKE 'group_replication_ssl_mode';
SHOW VARIABLES LIKE 'group_replication_recovery_use_ssl';
SHOW BINARY LOGS;
SELECT @@global.gtid_executed;
exit
```

```bash
# MONITORING — run from a machine that can reach the database network.
curl --connect-timeout 5 --max-time 15 -fsS http://one.percona.internal:9104/metrics | grep '^mysql_up'
curl --connect-timeout 5 --max-time 15 -fsS http://two.percona.internal:9104/metrics | grep '^mysql_up'
curl --connect-timeout 5 --max-time 15 -fsS http://three.percona.internal:9104/metrics | grep '^mysql_up'
curl --connect-timeout 5 --max-time 15 -fsS http://one.percona.internal:9104/metrics | grep '^mysql_perf_schema_replication_group'

# On a database VM:
sudo systemctl status mysqld_exporter --no-pager
sudo journalctl -u mysqld_exporter -n 100 --no-pager
sudo ss -ltnp | grep ':9104'

# On the monitoring VM, after downloading Prometheus and configuring its targets:
./promtool check config prometheus.yml
./prometheus --config.file=prometheus.yml
# Browser: http://<MONITORING_VM>:9090/targets
```

```promql
# PROMETHEUS QUERY PAGE — exporter reachability, MySQL reachability, group membership.
up{job="percona"}
mysql_up{job="percona"}
mysql_perf_schema_replication_group_member_info{job="percona"}
mysql_perf_schema_transactions_in_queue{job="percona"}
```

```bash
# BACKUPS — run on db3, the configured backup source.
# Backups stay on db3 unless remote storage is configured.
sudo /usr/local/bin/percona-backup backup full
sudo /usr/local/bin/percona-backup status
sudo ls -lh /var/backup/mysql
sudo ls -lh /var/backup/binlog
sudo crontab -l
sudo tail -n 100 /var/log/percona-backup.log
# Optional incremental: requires an existing completed backup chain.
sudo /usr/local/bin/percona-backup backup incremental
# Manually archive rotated binlogs.
sudo /usr/local/bin/percona-binlog-ship /var/backup/binlog /etc/percona-backup/client.cnf

# Alternatively trigger a full backup from the controller.
ansible backup_sources -b -m command -a '/usr/local/bin/percona-backup backup full' --ask-vault-pass
ansible backup_sources -b -m command -a '/usr/local/bin/percona-backup status' --ask-vault-pass

# MINIO — after setting backup_storage, endpoint, bucket, credentials and mc download settings.
# Run on the controller; this does not install a MinIO server or upload old backups.
ansible-playbook playbooks/operations/backup.yml --ask-vault-pass
# Run on the backup VM, only after MinIO configuration:
sudo /usr/local/bin/mc --config-dir /etc/percona-backup/mc ls backup/
```

```bash
# FAILOVER DEMO — disposable lab only; first confirm all THREE members are ONLINE.
# Identify the current PRIMARY using the membership SQL above.
# On ONLY that primary:
sudo systemctl stop mysql

# From an application client: reconnect and query the replacement writer.
mysql --protocol=TCP -h proxysql.percona.internal -P 6033 -u appuser -p appdb \
  -e 'SELECT @@hostname, @@server_uuid, @@read_only;'

# On the stopped node:
sudo systemctl start mysql
# Recheck membership: all three must be ONLINE before stopping another member.
# Never stop two members together during this demo; do not force-bootstrap the group.
```

```sql
-- RESTORE DEMO DATA — run locally as root on the CURRENT PRIMARY, before backup.
-- appuser does not have CREATE TABLE privilege in the example grants.
CREATE TABLE IF NOT EXISTS appdb.restore_demo (
  id INT PRIMARY KEY,
  note VARCHAR(100) NOT NULL
);
INSERT INTO appdb.restore_demo VALUES (1, 'restore demonstration')
ON DUPLICATE KEY UPDATE note = 'restore demonstration';
SELECT * FROM appdb.restore_demo;
```

```bash
# RESTORE — separate isolated VM ONLY; never an active group member.
# Target prerequisites: Percona 8.4 already installed/running, /var/lib/mysql datadir,
# same root password as the source, enough space for backup + working copy + restored data.
# Add restore1 under restore_targets in the chosen inventory, NOT under database/gr_members.
# Copy the completed backup directory to /var/backup/mysql/<BACKUP_ID> on restore1.
# Include manifest.json and _SUCCESS.json; incrementals need the complete ancestor chain.
# Use a fresh workdir each time. This stops target MySQL and preserves its old datadir.

# Controller: replace BACKUP_ID with the completed backup ID.
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/restore.yml \
  --ask-vault-pass --limit restore1 \
  -e backup_restore_target=restore1 \
  -e backup_restore_id=BACKUP_ID \
  -e backup_restore_workdir=/var/tmp/restore-demo-01

# On restore1; use the root password stored in the backup.
sudo mysql -u root -p
```

```sql
-- RESTORED VM ONLY: expect read_only=1 and a UUID different from the source.
SELECT @@hostname, @@server_uuid, @@read_only;
SELECT * FROM appdb.restore_demo;
-- Restore does not join this VM to the group or attach it to ProxySQL.
exit
```

```bash
# LOCAL PROJECT CHECKS — no live database deployment.
PYTHONDONTWRITEBYTECODE=1 python3 tests/check.py
ansible-playbook -i inventories/group-one-proxy.yml playbooks/group-one-proxy.yml --syntax-check --ask-vault-pass
git diff --check
```
