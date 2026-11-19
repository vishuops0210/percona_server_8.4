# recevo_agent

Prepares a database node to run the Recevo agent (XtraBackup backups).

## What it does

- Installs Percona XtraBackup 8.4 and the MySQL client.
- Gives the agent user read access to the datadir (mysql group).
- Creates the backup directories.
- Writes `/etc/percona-backup/client.cnf` for the backup user.
- Installs, enables and starts the agent systemd service.

The agent code itself is copied by the playbook
(`playbooks/operations/recevo-agent.yml`) before this role runs.

## What it does NOT do

- It does not create the backup MySQL user. Create that by hand first
  (see `agent/DEPLOY.md`). The role only writes `client.cnf` with the
  password you provide.
- It does not stop, disable or mask MySQL. This role runs on a database
  node, and MySQL must keep running there.

## Requirements

- Target host is a database node, on a SECONDARY. XtraBackup reads the
  datadir directly, so the agent cannot run from the ProxySQL host.
- The Percona `ps-84-lts` repository is enabled on the target (it already
  is on a node running Percona Server 8.4).
- The backup MySQL user exists (created by hand).
- The backup user's password is provided (vault or `--extra-vars`).

## Variables

| Variable | Default | Description |
|---|---|---|
| recevo_agent_user | vishal | User the agent runs as |
| recevo_agent_group | vishal | Group the agent runs as |
| recevo_agent_home | /home/vishal/agent | Where the agent code lives |
| recevo_backup_user | backup | MySQL backup user |
| recevo_backup_password | (none, required) | Password for the backup user |
| recevo_client_host | 127.0.0.1 | Host in client.cnf |
| recevo_client_port | 3306 | Port in client.cnf |
| recevo_client_file | /etc/percona-backup/client.cnf | Path to client.cnf |
| recevo_backup_path | /var/backup/mysql | Where backups land |
| recevo_binlog_path | /var/backup/binlog | Where binlogs land |
| recevo_datadir | /var/lib/mysql | Datadir XtraBackup reads |

## Usage

Add the chosen secondary to a `recevo_agent` group in your inventory, then
run the playbook from the repo root:

```bash
ansible-playbook -i <inventory> playbooks/operations/recevo-agent.yml \
  --extra-vars "recevo_backup_password=<the password>"
```

Or set `vault_recevo_backup_password` in your vault and pass the vault
password. The playbook copies the agent code, then runs this role.
