# recevo_agent

Prepares the ProxySQL host (proxy1) to run the Recevo agent.

The agent runs on proxy1, the separate server. It takes backups from the
database nodes by running an Ansible playbook against a secondary (the
recevo_xtrabackup role), fetches the backup back to proxy1, and uploads it
to MinIO.

## What it does

- Installs the MySQL client and the server package (for mysqlbinlog), then
  stops and masks the MySQL service on proxy1. Proxy1 does not run MySQL;
  the server package is only there for the mysqlbinlog tool.
- Installs Ansible, so the agent can run the remote XtraBackup playbook.
- Creates the backup directories on proxy1.
- Writes `/etc/percona-backup/client.cnf` for the backup user.
- Deploys the remote XtraBackup Ansible files to proxy1, so the agent can
  run them (the recevo_xtrabackup role and its playbook).
- Installs, enables and starts the agent systemd service.

## What it does NOT do

- It does not install XtraBackup on proxy1. XtraBackup runs on the
  secondary, via the recevo_xtrabackup role.
- It does not create the backup MySQL user. Create that by hand first (see
  `agent/DEPLOY.md`). The role only writes `client.cnf` with the password
  you provide.
- It does not set up SSH from proxy1 to the secondary. Create a key for the
  agent user on proxy1 and copy it to the secondary yourself (see
  `agent/DEPLOY.md`).

## Requirements

- Target host is proxy1 (the ProxySQL host, the separate server).
- The Percona `ps-84-lts` repository is enabled on the target.
- The backup MySQL user exists (created by hand).
- The backup user's password is provided (vault or `--extra-vars`).

## Variables

| Variable | Default | Description |
|---|---|---|
| recevo_agent_user | vishal | User the agent runs as |
| recevo_agent_group | vishal | Group the agent runs as |
| recevo_agent_home | /home/vishal/agent | Where the agent code lives |
| recevo_ansible_dir | /home/vishal/ansible | Where the Ansible files live on proxy1 |
| recevo_backup_user | backup | MySQL backup user |
| recevo_backup_password | (none, required) | Password for the backup user |
| recevo_client_host | 127.0.0.1 | Host in client.cnf |
| recevo_client_port | 3306 | Port in client.cnf |
| recevo_client_file | /etc/percona-backup/client.cnf | Path to client.cnf |
| recevo_backup_path | /var/backup/mysql | Where backups land on proxy1 |
| recevo_binlog_path | /var/backup/binlog | Where binlogs land on proxy1 |

## Usage

Run the playbook from the repo root:

```bash
ansible-playbook -i <inventory> playbooks/operations/recevo-agent.yml \
  --extra-vars "recevo_backup_password=<the password>"
```

The playbook copies the agent code and the Ansible files, then runs this
role. The agent then runs on proxy1 and triggers the remote XtraBackup
playbook against a secondary when a backup is due.
