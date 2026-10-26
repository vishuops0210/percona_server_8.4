# First time using Ansible?

You run Ansible on your laptop/control machine. It connects to existing VMs over SSH and installs/configures services.

| Word | Meaning |
|---|---|
| Inventory | Your VM names and IP addresses |
| Playbook | The setup command's instruction file |
| Role | Reusable installation/configuration steps |
| Vault | An encrypted file containing passwords |

## 1. Prepare access

Use Ubuntu 24.04 amd64 VMs. You need SSH access and sudo on each VM. Install Ansible Core 2.16+ on your control machine and check `ansible --version`.

From this repository's root:

```bash
ansible-galaxy collection install -r requirements.yml
```

## 2. Pick one setup

Open [the deployment runbook](deployment-runbook.md) and choose one of the
two topologies. Edit only its inventory file, replacing example
`ansible_host` IP addresses. Group labels select jobs; they are not extra
VMs.

For example, with the one-proxy inventory:

```bash
ansible -i inventories/group-one-proxy.yml all -m ping
```

Expect `pong`. Fix SSH/IP problems before deploying. Use the matching inventory file for your chosen setup in every later command.

## 3. Set your values and passwords

Edit `inventories/group_vars/all/main.yml`: SSH username, database names, connection limit and backup settings.

Open `inventories/group_vars/all/vault.example.yml` for the password key names, then:

```bash
# New password file:
ansible-vault create inventories/group_vars/all/vault.yml
# If it already exists, edit it instead:
ansible-vault edit inventories/group_vars/all/vault.yml
```

Fill each required key with your own password. `--ask-vault-pass` asks for the password that unlocks this file, not the database password. Keep the same database credentials on reruns.

For a new group, generate a UUID and put it in `percona_group_name` in shared settings:

```bash
python3 -c 'import uuid; print(uuid.uuid4())'
```

Keep that UUID unchanged after creating the group. For the two-proxy setup, edit `group_vars/proxies.yml` if you want a VIP; otherwise use your existing load balancer or the individual proxy addresses.

## 4. Deploy, then verify

Run the one deployment command from [the four-setup runbook](deployment-runbook.md). It also sets up backups and exporters.

Take the first full backup on the selected backup VM:

```bash
sudo /usr/local/bin/percona-backup backup full
sudo /usr/local/bin/percona-binlog-ship /var/backup/binlog /etc/percona-backup/client.cnf
```

Then run `playbooks/operations/verify.yml` with the same inventory. Use `playbooks/operations/configure.yml` after changing settings. Exact examples are in the [main README](../README.md#later-change-settings-verify-health).
