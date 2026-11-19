# Recevo agent: deploy on a database node (secondary)

The agent runs on a database node, on a SECONDARY. XtraBackup reads the
datadir directly, so it cannot run from the ProxySQL host. Everything else
the agent does (discovery, binlog shipping, MinIO upload, heartbeat) talks
over TCP, so those work from any host.

Setup is done by the `recevo_agent` Ansible role, not by hand and not by
the agent itself. The role installs the tools, writes the client config,
creates the directories, gives the agent datadir access, and installs and
starts the service. See `roles/recevo_agent/README.md` for what the role
does and does not do.

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

## 1. Create the backup MySQL user

You create this user yourself. The agent never gets DB admin access, and
the role does not create the user either. The role only writes `client.cnf`
with the password you provide in step 2.

Run this SQL on the primary (it replicates to the secondaries). Running it
on each node with `IF NOT EXISTS` is also safe. Pick a strong password and
remember it: you will pass the same password to the role in step 2.
Do not paste the password in chat.

XtraBackup needs BACKUP_ADMIN on top of the old mysqldump grants
(for LOCK INSTANCE FOR BACKUP).

```sql
CREATE USER IF NOT EXISTS 'backup'@'%' IDENTIFIED WITH caching_sha2_password BY 'CHOOSE_A_STRONG_PASSWORD';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, RELOAD, PROCESS, REPLICATION CLIENT, REPLICATION SLAVE, BACKUP_ADMIN ON *.* TO 'backup'@'%';
FLUSH PRIVILEGES;
```

## 2. Run the recevo_agent role

Add the secondary you picked in step 0 to a `recevo_agent` group in your
inventory. Then run the playbook from the repo root, on the
verified-restore branch:

```bash
cd ~/Desktop/percona_server_8.4
git checkout verified-restore
git pull origin verified-restore
ansible-playbook -i <inventory> playbooks/operations/recevo-agent.yml \
  --extra-vars "recevo_backup_password=<the password from step 1>"
```

The playbook copies the agent code to the node, then the role installs
`percona-xtrabackup-84` and `percona-server-client`, writes
`/etc/percona-backup/client.cnf`, creates `/var/backup/mysql` and
`/var/backup/binlog`, adds the agent user to the mysql group so it can
read the datadir, and installs, enables and starts the service.

A full XtraBackup is about the size of the datadir, plus one incremental
per hour until the next full. Make sure the disk holding `/var/backup`
has room for at least one full plus a day of incrementals.

Do NOT stop, disable or mask the mysql service on a DB node. It runs the
database. That step only applied to the old ProxySQL install.

## 3. Verify the agent is running and the first backup landed

On the DB node:

```bash
sudo systemctl status recevo-agent.service
sudo journalctl -u recevo-agent.service -f
```

Good output looks like this:

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
`config.json`.

Confirm the backup landed:

```bash
ls -l /var/backup/mysql/<id>/
cat /var/backup/mysql/<id>/manifest.json
ls -l /var/backup/binlog/
```

To run the agent in the foreground for debugging, stop the service first
and run it by hand:

```bash
sudo systemctl stop recevo-agent.service
cd ~/agent
python3 agent.py
```

## 4. MinIO (optional, later)

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
| discovery shows UNKNOWN for all nodes | cluster down, or client.cnf wrong, or backup user missing | Step 0, then steps 1 and 2 |
| Role fails: no 8.4 XtraBackup candidate | Percona repo not enabled | Enable the ps-84-lts repo, then re-run the playbook |
| Role fails: backup password not set | no password passed | Pass `--extra-vars "recevo_backup_password=..."` |
| Role fails: agent user cannot read the datadir | mysql group not applied | Re-run the playbook; if it still fails, check `id vishal` |
| `[backup] failed: no backup source found` | no group member answered | Same as the discovery row above |
| `xtrabackup binary not found` | role did not install it | Re-run the playbook |
| xtrabackup fails on BACKUP_ADMIN / lock | missing BACKUP_ADMIN grant | Step 1, check the GRANT |
| binlog fetch fails | missing REPLICATION SLAVE grant | Step 1, check the GRANT |
| Agent runs but no backup appears | backup runs once at start, then hourly | Wait for the first run, or check the log for `[backup] failed` |
