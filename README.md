# Verified Restore branch

This branch is the working branch for the Verified Restore product. It holds only what the product needs, so the product work stays separate from the full Percona lab role on main.

## What is in this branch

- `roles/percona_server/` : the Ansible role that installs and configures Percona Server 8.4. This is the engine the product runs on.
- `mock-website/` : the Verified Restore mock website, the :8080 site. React source in `src/`, built files in `dist/`. Run `npm install` then `npm run build` to rebuild, or serve `dist/` directly.
- `db-admin-app/` : the working database admin app, the :5000 site. Flask app in `app.py`, React UI built files in `static/`. Run with the systemd service or `python3 app.py`.

## Full Percona setup

This branch also carries the complete Percona setup, so a client can take
this branch alone and run the entire setup: three Percona Server 8.4 nodes
in single-primary Group Replication, fronted by one ProxySQL node (proxy1).

What the setup contains:

- `playbooks/group-one-proxy.yml` : the one playbook for the entire setup.
  It prepares the database nodes (`percona_server`), forms the group
  (`percona_group_replication`), creates databases and users on the elected
  writer (`percona_access`), then sets up routing (`operations/proxysql.yml`).
- `roles/percona_server/` : install and configure Percona Server 8.4.
- `roles/percona_group_replication/` : bootstrap and join the group.
- `roles/percona_access/` : create application databases and users on the writer.
- `roles/proxysql/` : ProxySQL install, backends, routing and accounts.
- `playbooks/operations/proxysql.yml` : reapply proxy settings on their own.
- `playbooks/operations/preflight.yml` : safety checks to run before deploying.
- `inventories/group-one-proxy.yml` : example inventory, three database
  hosts and one proxy. Replace the example `ansible_host` addresses with
  your real server IPs.
- `inventories/group_vars/all/main.yml` : shared values for the setup.
  Generate your own group UUID and put it in `percona_group_name`.
- `ansible.cfg` and `requirements.yml` : Ansible config and collections.
- `docs/` : architecture, deployment runbook, ProxySQL endpoint guide,
  troubleshooting, production checklist, and Group Replication interview
  questions and answers.

How to run it:

```bash
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventories/group-one-proxy.yml playbooks/operations/preflight.yml --ask-vault-pass
ansible-playbook -i inventories/group-one-proxy.yml playbooks/group-one-proxy.yml --ask-vault-pass --ask-become-pass
```

Vault: passwords live in the encrypted `inventories/group_vars/all/vault.yml`,
created with `ansible-vault create inventories/group_vars/all/vault.yml`.
That file is gitignored and is not on this branch; create your own. The keys
the setup needs, names only:

- `vault_percona_root_password` : MySQL root password, also used as the admin password.
- `vault_replication_password` : Group Replication recovery password, 32 characters or fewer.
- `vault_proxysql_admin_password` : ProxySQL admin password.
- `vault_proxysql_monitor_password` : ProxySQL monitor password.
- `vault_testuser1_password` : password for the example ProxySQL user testuser1.
- `vault_appuser1_password` : password for the example ProxySQL user appuser1.

If you define your own users in `percona_users`, add a vault key per user
following the examples in `roles/percona_access/defaults/main.yml`.

The example databases are testdb, app01 and app02, and the example ProxySQL
users testuser1 and appuser1 expect app01 and app02 to exist. Change the
databases and the ProxySQL users together.

## What is not in this branch

Backup, monitoring, and tests are deliberately left out. The two-proxy
playbook and inventory (group-two-proxies.yml) and the Keepalived VIP role
(proxysql_ha) were removed from this branch; it ships one ProxySQL node
only. The backup role
(`percona_backup`), the exporter role (`mysql_exporter`), the backup and
monitoring playbooks, and the test suite stay on main. This branch ships
the highly available group and its proxy endpoint only; off-site copies and
point-in-time recovery are not part of it. If the product needs one of the
left-out pieces later, bring it in from main with a clean cherry-pick or
copy, do not merge main into this branch.
