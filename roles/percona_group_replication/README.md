# percona_group_replication

Form one single-primary group.

```mermaid
flowchart LR
  A[Fresh members] --> B[Recovery account] --> C[Bootstrap seed once] --> D[Join members]
```

The initial seed is not a permanent writer. Reuses online members on reruns. Fresh secondaries recover from available binary logs; cloning and existing-data migration are deferred. A bootstrap marker prevents ordinary setup from starting a second group after a full outage. Never delete it to force recovery.

```yaml
percona_group_seed: db1
percona_group_name: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
percona_group_password: "{{ vault_replication_password }}"
```

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/group-two-proxies.yml --ask-vault-pass
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
