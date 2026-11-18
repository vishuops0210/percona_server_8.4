# Recevo agent: deploy on the ProxySQL host

The agent runs on proxy1 (192.168.8.55). It talks to the Group Replication
nodes over TCP. Nothing runs on the database nodes.

Deploy in this order. Each step depends on the one before it.

## 0. Confirm the cluster is up

On db1, as root:

```bash
sudo mysql -e "SELECT MEMBER_HOST, MEMBER_ROLE, MEMBER_STATE FROM performance_schema.replication_group_members;"
```

Expect 3 rows, all ONLINE, one PRIMARY and two SECONDARY. If the cluster
is not up, rebuild it first with the branch playbook (preflight, then
group-one-proxy.yml) and come back here.

## 1. Create the backup MySQL user

You create this user yourself. The agent never gets DB admin access.

Run this SQL on the primary (it replicates to the secondaries). Running it
on each node with `IF NOT EXISTS` is also safe. Pick a strong password and
remember it: you will use the same password in `client.cnf` in step 3.
Do not paste the password in chat.

```sql
CREATE USER IF NOT EXISTS 'backup'@'%' IDENTIFIED WITH caching_sha2_password BY 'CHOOSE_A_STRONG_PASSWORD';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, RELOAD, PROCESS, REPLICATION CLIENT, REPLICATION SLAVE ON *.* TO 'backup'@'%';
FLUSH PRIVILEGES;
```

## 2. Install the MySQL client tools on proxy1

The agent shells out to `mysql`, `mysqldump` and `mysqlbinlog`. On
Percona 8.4, `mysqlbinlog` ships in the server package, not the client
package, so install both. Then stop and mask the mysql service: no
database ever runs on the backup host.

```bash
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y percona-server-client percona-server-server
sudo systemctl stop mysql || true
sudo systemctl disable mysql
sudo systemctl mask mysql
which mysql mysqldump mysqlbinlog
```

If apt cannot find the packages, the Percona repo is not enabled. Enable
it first, then retry:

```bash
sudo percona-release setup ps-84-lts
sudo apt update
```

Check the candidate is 8.4 with `apt-cache policy percona-server-client`.

## 3. Write /etc/percona-backup/client.cnf on proxy1

This file holds the backup user's credentials. The agent reads it with
`--defaults-file`, so no password ever appears on a command line. Use the
same password you chose in step 1. The `host` line is only a default for
manual `mysql` calls; the agent overrides it with the discovered source
on every call.

```bash
sudo mkdir -p /etc/percona-backup
sudo tee /etc/percona-backup/client.cnf > /dev/null <<'EOF'
[client]
user = "backup"
password = "THE_SAME_PASSWORD_FROM_STEP_1"
host = "192.168.8.56"
port = 3306
EOF
sudo chown vishal:vishal /etc/percona-backup/client.cnf
sudo chmod 600 /etc/percona-backup/client.cnf
```

## 4. Create the backup directories on proxy1

```bash
sudo mkdir -p /var/backup/mysql /var/backup/binlog
sudo chown -R vishal:vishal /var/backup
sudo chmod 700 /var/backup/mysql /var/backup/binlog
```

## 5. Copy the agent to proxy1

From your laptop, on the verified-restore branch:

```bash
cd ~/Desktop/percona_server_8.4
git checkout verified-restore
git pull origin verified-restore
scp -r agent vishal@192.168.8.55:/home/vishal/agent
```

## 6. First run in the foreground

On proxy1:

```bash
cd ~/agent
python3 agent.py
```

Watch the output. Good output looks like this:

```text
[agent] loaded plugins: backup-mysqldump, binlog-ship, discovery, heartbeat, minio-upload
[discovery] 192.168.8.56 -> PRIMARY
[discovery] 192.168.8.57 -> SECONDARY
[discovery] 192.168.8.54 -> SECONDARY
[backup] dumping from 192.168.8.57 -> /var/backup/mysql/<id>/dump.sql.gz
[backup] done: <id> from 192.168.8.57 (<bytes> bytes)
[binlog] fetching mysql-bin.00000x from 192.168.8.56
```

`[heartbeat] no 'url' in config` and `[minio] disabled or no endpoint`
are expected for now: heartbeat has no URL yet and MinIO is disabled in
`config.json`. Press Ctrl+C to stop.

Confirm the backup landed:

```bash
ls -l /var/backup/mysql/<id>/
cat /var/backup/mysql/<id>/manifest.json
ls -l /var/backup/binlog/
```

## 7. Run it as a service

```bash
sudo cp ~/agent/recevo-agent.service /etc/systemd/system/recevo-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now recevo-agent.service
sudo systemctl status recevo-agent.service
sudo journalctl -u recevo-agent.service -f
```

## 8. MinIO (optional, later)

When you want backups shipped off-box:

```bash
cd ~/agent
pip install -r requirements.txt
```

Edit `config.json`: set `minio.enabled` to `true` and fill `endpoint`,
`access_key`, `secret_key`, `bucket`. Then restart:

```bash
sudo systemctl restart recevo-agent.service
```

## Verify and troubleshoot

| Symptom | Cause | Fix |
|---|---|---|
| discovery shows UNKNOWN for all nodes | cluster down, or client.cnf wrong, or backup user missing | Step 0, then steps 1 and 3 |
| `[backup] failed: no backup source found` | no group member answered | Same as above |
| `mysql: command not found` | client tools not installed | Step 2 |
| `mysqlbinlog: command not found` | server package not installed | Step 2 (install percona-server-server) |
| Permission denied on `/var/backup` | directories owned by root | Step 4 |
| binlog fetch fails | missing REPLICATION SLAVE grant | Step 1, check the GRANT |
| Agent runs but no backup appears | backup interval is 24h; it runs once at start, then daily | Wait for the first run, or check the log for `[backup] failed` |
