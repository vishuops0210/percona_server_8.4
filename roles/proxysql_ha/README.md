# proxysql_ha

Add one optional virtual IP for two proxies.

```mermaid
flowchart LR
  A[Application] --> V[VIP] --> P[Healthy proxy]
```

Keepalived uses unicast VRRP and checks the proxy process/listener. Both proxy hosts need the correct interface, reachable peer IPs and permission from the network to move the VIP. Disabled by default until those values are supplied. Automatically skipped when the inventory has only one proxy.

```yaml
proxysql_ha_enabled: true
proxysql_ha_interface: ens18
proxysql_ha_vip: 192.0.2.50/24
```

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/operations/proxysql.yml --ask-vault-pass
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
