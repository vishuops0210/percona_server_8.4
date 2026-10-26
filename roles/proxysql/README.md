# proxysql

Route application traffic to the group writer.

```mermaid
flowchart LR
  A[Application :6033] --> P[ProxySQL] --> W[Current writer]
```

Uses native Group Replication monitoring and caching_sha2_password accounts. All traffic starts on the writer; read rules are optional. Both proxies receive the same inventory settings. Admin listens on localhost:6032.

DNS names are supported for inventory hosts. The monitor and application
accounts use the `%` host wildcard: the database servers keep
`skip-name-resolve = ON`, so account hosts never depend on DNS, and the
private network's security groups remain the access boundary.

```yaml
proxysql_listen_address: '0.0.0.0'
proxysql_query_rules: []
```

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/operations/proxysql.yml --ask-vault-pass
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
