# Recevo agent: deploy on a database node (secondary)

The agent runs on a database node, on a SECONDARY. XtraBackup reads the
datadir directly, so it cannot run from the ProxySQL host. Everything else
the agent does (discovery, binlog shipping, MinIO upload, heartbeat) talks
over TCP, so those work from any host.

Deploy in this order. Each step depends on the one before it.

## 0. Confirm the cluster is up and pick a secondary

On db1, as root:

```bash
sudo mysql -e "SELECT MEMBER_HOST, MEMBER_ROLE, MEMBER_STATE FROM performance_schema.replication_group_members;"
```

Expect 3 rows, all ONLINE, one PRIMARY and two SECONDARY. If the cluster
is not up, rebuild it first with the branch playbook (preflight, then
group-one-proxy.yml) and come back here.

Install the agent on one of the SECONDARY nodes (in the original lab db1
was the primary, so db2 or db3). If that node becomes PRIMARY after a
failover, move the agent to another secondary: a backup running on the
writer puts load on the node serving writes.

From here on, "the DB node" means the secondary you picked.

## 1. Create the backup MySQL user

You create this user yourself. The agent never gets DB admin access.

Run this SQL on the primary (it replicates to the secondaries). Running it
on each node with `IF NOT EXISTS` is also safe. Pick a strong password and
remember it: you will use the same password in `client.cnf` in step 3.
Do not paste the password in chat.

XtraBackup needs BACKUP_ADMIN on top of the old mysqldump grants
(for LOCK INSTANCE FOR BACKUP).

```sql
CREATE USER IF NOT EXISTS 'backup'@'%' IDENTIFIED WITH caching_sha2_password BY 'CHOOSE_A_STRONG_PASSWORD';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, RELOAD, PROCESS, REPLICATION CLIENT, REPLICATION SLAVE, BACKUP_ADMIN ON *.* TO 'backup'@'%';
FLUSH PRIVILEGES;
```

## 2. Install the tools on the DB node

The agent shells out to `mysql`, `mysqlbinlog` and `xtrabackup`.
`mysqlbinlog` already ships with the server package on a DB node.
Install the client and XtraBackup:

```bash
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y percona-server-client percona-xtrabackup-84
which mysql mysqlbinlog xtrabackup
```

If apt cannot find the packages, the Percona repo is not enabled. Enable
it first, then retry:

```bash
sudo percona-release setup ps-84-lts
sudo apt update
```

Check the candidate is 8.4 with `apt-cache policy percona-xtrabackup-84`.

Do NOT stop, disable or mask the mysql service on a DB node. It runs the
database. That step only applied to the old ProxySQL install.

## 3. Give the agent read access to the datadir

XtraBackup reads `/var/lib/mysql` directly. The agent runs as `vishal`,
so give that user read access through the mysql group:

```bash
sudo usermod -aG mysql vishal
sudo -u vishal ls /var/lib/mysql | head
```

If the listing works, access is fine. If it still fails, the fallback is
to run the service as root: change `User=vishal` to `User=root` in
`recevo-agent.service` before step 7.

## 4. Write /etc/percona-backup/client.cnf on the DB node

This file holds the backup user's credentials. The agent reads it with
`--defaults-file`, so no password ever appears on a command line. Use the
same password you chose in step 1.

```bash
sudo mkdir -p /etc/percona-backup
sudo tee /etc/percona-backup/client.cnf > /dev/null <<'EOF'
[client]
user = "backup"
password = "THE_SAME_PASSWORD_FROM_STEP_1"
host = "127.0.0.1"
port = 3306
EOF
sudo chown vishal:vishal /etc/percona-backup/client.cnf
sudo chmod 600 /etc/percona-backup/client.cnf
```

## 5. Create the backup directories on the DB node

```bash
sudo mkdir -p /var/backup/mysql /var/backup/binlog
sudo chown -R vishal:vishal /var/backup
sudo chmod 700 /var/backup/mysql /var/backup/binlog
```

A full XtraBackup is about the size of the datadir, plus one incremental
per hour until the next full. Make sure the disk holding `/var/backup`
has room for at least one full plus a day of incrementals.

## 6. Copy the agent to the DB node

From your laptop, on the verified-restore branch:

```bash
cd ~/Desktop/percona_server_8.4
git checkout verified-restore
git pull origin verified-restore
scp -r agent vishal@<DB_NODE_IP>:/home/vishal/agent
```

Replace `<DB_NODE_IP>` with the secondary you picked in step 0.

## 7. First run in the foreground

On the DB node:

```bash
cd ~/agent
python3 agent.py
```

Watch the output. Good output looks like this:

```text
[agent] loaded plugins: backup-xtrabackup, binlog-ship, discovery, heartbeat, minio-upload
[discovery] 192.168.8.56 -> PRIMARY
[discovery] 192.168.8.57 -> SECONDARY
[discovery] 192.168.8.54 -> SECONDARY
[backup] full backup -> /var/backup/mysql/<id>
[backup] done: <id> full (<bytes> bytes)
[binlog] fetching mysql-bin.00000x from 192.168.8.56
```

The first run is a full backup. The next runs (every hour) are
incremental until a full is older than 24 hours, then a full again.

`[heartbeat] no 'url' in config` and `[minio] disabled or no endpoint`
are expected for now: heartbeat has no URL yet and MinIO is disabled in
`config.json`. Press Ctrl+C to stop.

Confirm the backup landed:

```bash
ls -l /var/backup/mysql/<id>/
cat /var/backup/mysql/<id>/manifest.json
ls -l /var/backup/binlog/
```

## 8. Run it as a service

```bash
sudo cp ~/agent/recevo-agent.service /etc/systemd/system/recevo-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now recevo-agent.service
sudo systemctl status recevo-agent.service
sudo journalctl -u recevo-agent.service -f
```

## 9. MinIO (optional, later)

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
| discovery shows UNKNOWN for all nodes | cluster down, or client.cnf wrong, or backup user missing | Step 0, then steps 1 and 4 |
| `[backup] failed: no backup source found` | no group member answered | Same as above |
| `xtrabackup binary not found` | percona-xtrabackup-84 not installed | Step 2 |
| `mysql: command not found` | client not installed | Step 2 |
| Permission denied reading `/var/lib/mysql` | vishal cannot read the datadir | Step 3 |
| xtrabackup fails on BACKUP_ADMIN / lock | missing BACKUP_ADMIN grant | Step 1, check the GRANT |
| Permission denied on `/var/backup` | directories owned by root | Step 5 |
| binlog fetch fails | missing REPLICATION SLAVE grant | Step 1, check the GRANT |
| Agent runs but no backup appears | backup runs once at start, then hourly | Wait for the first run, or check the log for `[backup] failed` |
