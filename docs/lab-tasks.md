# Lab walkthrough

```mermaid
flowchart LR
  A[Edit hosts and credentials] --> B[Install]
  B --> C[See group members]
  C --> D[Connect through proxy]
  D --> E[View metrics and take backup]
```

1. Prepare fresh Ubuntu 24.04 amd64 VMs and edit [HA inventory](../inventories/group-two-proxies.yml).
2. Fill the existing Vault keys from [the example](../inventories/group_vars/all/vault.example.yml).
3. Run the setup:

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/group-two-proxies.yml --ask-vault-pass
```

4. On a database VM, open `mysql -u root -p`:

```sql
SELECT VERSION();
SELECT MEMBER_HOST, MEMBER_STATE, MEMBER_ROLE
FROM performance_schema.replication_group_members;
```

5. Connect through either proxy:

```bash
mysql -h <proxy-ip> -P 6033 -u appuser -p appdb
```

```sql
CREATE TABLE demo (id INT PRIMARY KEY, note VARCHAR(100));
INSERT INTO demo VALUES (1, 'hello from Group Replication');
SELECT @@hostname;
SELECT * FROM appdb.demo;
```

Application tables need primary keys for Group Replication.

6. See exporter metrics: `curl http://<db-ip>:9104/metrics`.
7. On db3, run `sudo /usr/local/bin/percona-backup backup full`.
8. Optionally enable the proxy VIP in [proxy settings](../inventories/group_vars/proxies.yml), then run `playbooks/operations/proxysql.yml` with the same inventory.

Do not run the group play with `--limit`: bootstrap needs the full group inventory.

Stopping the current writer can demonstrate election in a disposable lab, but active connections may fail and need reconnecting. Full outage recovery and failure acceptance are a later phase.
