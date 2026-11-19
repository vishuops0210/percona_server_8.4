# Recevo agent: deploy on the ProxySQL host (proxy1)

The agent runs on proxy1, the separate server. It takes backups from the
database nodes by running an Ansible playbook against a secondary. The
playbook installs XtraBackup on the secondary, takes the backup there, and
fetches the archive back to proxy1. The agent then uploads it to MinIO.

XtraBackup reads the datadir directly, so it cannot run on proxy1. That is
why the backup work happens on the secondary, while the agent itself stays
on proxy1.

Setup is done by the `recevo_agent` Ansible role, not by hand and not by
the agent itself. The role installs the tools and Ansible, writes the
client config, creates the directories, deploys the remote XtraBackup
Ansible files, and installs and starts the service. See
`roles/recevo_agent/README.md` and `roles/recevo_xtrabackup/README.md`.

Deploy in this order. Each step depends on the one before it.

## 0. Confirm the cluster is up

On db1, as root:

```bash
sudo mysql -e "SELECT MEMBER_HOST, MEMBER_ROLE, MEMBER_STATE FROM performance_schema.replication_group_members;"
```

Expect 3 rows, all ONLINE, one PRIMARY and two SECONDARY. If the cluster
is not up, rebuild it first with the branch playbook (preflight, then
group-one-proxy.yml) and come back here.

The agent picks a secondary itself at backup time, so you do not choose
one here. If the secondary it picks becomes PRIMARY after a failover, the
agent picks another secondary on the next run.

## 1. Create the backup MySQL user

You create this user yourself. The agent never gets DB admin access, and
the roles do not create the user either. The roles only write and copy
`client.cnf` with the password you provide in step 3.

Run this SQL on the primary (it replicates to the secondaries). Running it
on each node with `IF NOT EXISTS` is also safe. Pick a strong password and
remember it: you will pass the same password to the role in step 3.
Do not paste the password in chat.

XtraBackup needs BACKUP_ADMIN on top of the old mysqldump grants
(for LOCK INSTANCE FOR BACKUP).

```sql
CREATE USER IF NOT EXISTS 'backup'@'%' IDENTIFIED WITH caching_sha2_password BY 'CHOOSE_A_STRONG_PASSWORD';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, RELOAD, PROCESS, REPLICATION CLIENT, REPLICATION SLAVE, BACKUP_ADMIN ON *.* TO 'backup'@'%';
FLUSH PRIVILEGES;
```

## 2. Set up SSH from proxy1 to the secondary nodes

The agent on proxy1 runs Ansible against a secondary, so it needs SSH
access from proxy1 to the DB nodes. This is Ansible only, using a key, to
run the remote XtraBackup playbook. The agent does not get sudo on the DB
nodes, and it does not run arbitrary commands there.

On proxy1, as vishal, create a key if there is not one already:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
```

Copy the public key to each DB node (db1, db2, db3) so the agent can reach
whichever node is secondary:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub vishal@<db1-ip>
ssh-copy-id -i ~/.ssh/id_ed25519.pub vishal@<db2-ip>
ssh-copy-id -i ~/.ssh/id_ed25519.pub vishal@<db3-ip>
```

Verify it works without a password:

```bash
ssh vishal@<db2-ip> "echo ok"
```

## 3. Run the recevo_agent role on proxy1

Add proxy1 to a `recevo_agent` group in your inventory. Then run the
playbook from the repo root, on the verified-restore branch:

```bash
cd ~/Desktop/percona_server_8.4
git checkout verified-restore
git pull origin verified-restore
ansible-playbook -i <inventory> playbooks/operations/recevo-agent.yml \
  --extra-vars "recevo_backup_password=<the password from step 1>"
```

The playbook copies the agent code and the remote XtraBackup Ansible
files to proxy1, then the role installs the MySQL client tools and
Ansible, writes `/etc/percona-backup/client.cnf`, creates
`/var/backup/mysql` and `/var/backup/binlog`, deploys the Ansible files
to `/home/vishal/ansible/`, and installs, enables and starts the service.

A full XtraBackup is about the size of the datadir, plus one incremental
per hour until the next full. Make sure the disk holding `/var/backup` on
proxy1 has room for at least one full plus a day of incrementals. The
secondary also keeps its own copy of the backup chain, so it needs the
same room.

## 4. Verify the agent is running and the first backup landed

On proxy1:

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
[backup] full backup of 192.168.8.57 -> /var/backup/mysql/<id>
[backup] done: <id> full (<bytes> bytes)
[binlog] fetching mysql-bin.00000x from 192.168.8.56
```

The first run is a full backup. The next runs (every hour) are
incremental until a full is older than 24 hours, then a full again.

`[heartbeat] no 'url' in config` and `[minio] disabled or no endpoint`
are expected for now: heartbeat has no URL yet and MinIO is disabled in
`config.json`.

Confirm the backup landed on proxy1:

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

## 5. MinIO (optional, later)

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
| Role fails: no 8.4 Percona Server candidate | Percona repo not enabled on proxy1 | Enable the ps-84-lts repo, then re-run the playbook |
| Role fails: backup password not set | no password passed | Pass `--extra-vars "recevo_backup_password=..."` |
| `[backup] failed: no SECONDARY node found` | cluster down, or no secondary | Step 0 |
| `[backup] failed: ansible-playbook not found` | Ansible not installed on proxy1 | Re-run the playbook in step 3 |
| Ansible fails: permission denied (publickey) | SSH key not copied to the secondary | Step 2 |
| Ansible fails: no 8.4 XtraBackup candidate | Percona repo not enabled on the secondary | Enable the ps-84-lts repo on the secondary, then wait for the next backup run |
| xtrabackup fails on BACKUP_ADMIN / lock | missing BACKUP_ADMIN grant | Step 1, check the GRANT |
| binlog fetch fails | missing REPLICATION SLAVE grant | Step 1, check the GRANT |
| Agent runs but no backup appears | backup runs once at start, then hourly | Wait for the first run, or check the log for `[backup] failed` |
