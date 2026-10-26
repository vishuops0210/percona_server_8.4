# mysql_exporter

Expose database and Group Replication metrics.

```mermaid
flowchart LR
  D[MySQL] --> E[Exporter :9104] --> P[Existing Prometheus]
```

Installs the exporter on monitoring_targets. Its account is created through the current writer. Group membership/statistics collectors are enabled. Prometheus/Grafana deployment is external; see the monitoring guide for scrape and alert examples.

```yaml
exporter_listen_address: '0.0.0.0'
exporter_port: 9104
```

```bash
ansible-playbook -i inventories/group-two-proxies.yml playbooks/operations/monitoring.yml --ask-vault-pass
```

[Defaults](defaults/main.yml) · [Tasks](tasks/main.yml) · [Project setup](../../README.md)
