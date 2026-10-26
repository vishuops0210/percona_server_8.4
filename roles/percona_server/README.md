# percona_server

Installs and configures Percona Server 8.4 as a Group Replication member.

```mermaid
flowchart LR
  A[Packages] --> B[Local login] --> C[mysqld.cnf] --> D[Restart if changed]
```

Owns the MySQL configuration. Data and binlogs live in `/var/lib/mysql`.
Every host in the `gr_members` inventory group gets the group configuration;
there is no other mode. Application databases and users are created once on
the elected writer by [percona_access](../percona_access/README.md), after the group forms.

```yaml
percona_max_connections: 150
percona_innodb_buffer_pool_size: 2G
```

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/group-two-proxies.yml --ask-vault-pass
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
