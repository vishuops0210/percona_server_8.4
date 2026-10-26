# Pick one inventory

An inventory is your server list. These two files are **alternative setups**,
not two groups you deploy together.

| What you want | Use this file | Servers |
|---|---|---|
| Group Replication + one ProxySQL | `group-one-proxy.yml` | 3 databases + 1 ProxySQL |
| Group Replication + two ProxySQL | `group-two-proxies.yml` | 3 databases + 2 ProxySQL |

Replace `ansible_host` with your VM IPs. `db1`, `db2`, `db3`, `proxy1` and
`proxy2` are labels for those VMs. Keep database server IDs unique.
Each database member must also have a different VM IP.

## SSH connection

Run commands from the project root so Ansible loads `ansible.cfg`.
Its default inventory is `group-one-proxy.yml`; `-i` overrides it.
Set `ansible_user` in `group_vars/all/main.yml` to the VM's SSH user.
The public key belongs in that user's `~/.ssh/authorized_keys` on each VM.
Keep the private key on your Ansible controller.

For a custom private key, set `private_key_file` in `ansible.cfg` or pass it:

```bash
ansible all --private-key ~/.ssh/my_key -m ping --ask-vault-pass
```

Default SSH keys or keys loaded in `ssh-agent` do not need an explicit path.
Host-key checking is enabled: verify each VM's fingerprint and connect once
with SSH before the first Ansible run. Successful Ansible ping returns `pong`;
it checks SSH and remote Python, not database readiness.

The same database appears under `database`, `backup_sources` and
`monitoring_targets` because those groups select its jobs. It does **not**
create another VM.

## Shared settings

- `group_vars/all/main.yml`: SSH user, database settings, database names and backup settings.
- `group_vars/all/vault.yml`: your encrypted passwords; never distributed in the collection.
- `group_vars/all/vault.example.yml`: password key names to fill.
- `group_vars/proxies.yml`: application accounts and the optional two-proxy VIP.

These settings apply to whichever inventory you choose. For a different
client, copy this directory into a separate client directory with separate
passwords and settings.

Always pass **one file**, for example `-i inventories/group-one-proxy.yml`.
Do not pass `-i inventories/`, because that combines both alternative
inventories.

[Deployment runbook](../docs/deployment-runbook.md)
