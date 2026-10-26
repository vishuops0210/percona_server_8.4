# Architecture

```mermaid
flowchart LR
  I[Inventory] --> S[percona_server]
  S --> GR[percona_group_replication]
  GR --> P[proxysql]
  P --> VIP[proxysql_ha: optional VIP]
  GR --> B[percona_backup]
  GR --> E[mysql_exporter]
```

| Layer | Owner |
|---|---|
| Packages, identity, mysqld.cnf | percona_server |
| Group membership and recovery channel | percona_group_replication |
| Primary election | MySQL Group Replication itself |
| Application routing | ProxySQL native group monitor |
| Proxy endpoint failover | Keepalived, or your existing load balancer |
| Scheduled backup / restore | percona_backup |
| Metrics | mysql_exporter → existing monitoring platform |

## Placement

| Profile | VMs |
|---|---|
| Standalone | One database VM |
| HA example | Three database VMs + two proxy VMs |
| Three-VM lab | Proxies may share two database VMs; use the same inventory host names in both groups |

Place databases on different physical hosts. A three-member group can retain quorum after one member fails. Losing quorum stops writes; setup never force-bootstraps an old group.

The example has a local backup source on db3. Configure existing MinIO for off-host copies. A local HA group is not a remote DR site.

## Network

| Traffic | Port |
|---|---|
| App → proxies | TCP 6033 |
| Proxy → databases; member recovery | TCP 3306 |
| Group communication between databases | TCP 33061 |
| Prometheus → exporters | TCP 9104 |
| Proxy administration | TCP 6032, localhost |
| VIP election between proxies | IP protocol 112 (VRRP), not TCP/UDP |

## References

- [Percona Group Replication architecture](https://docs.percona.com/percona-distribution-for-mysql/8.4/deploy-pdps-group-replication.html)
- [ProxySQL native Group Replication monitor](https://proxysql.com/documentation/group-replication-configuration/)
- [8.4 removed options](https://dev.mysql.com/doc/refman/8.4/en/added-deprecated-removed.html)

The older guide's removed variables and legacy sys view are not copied into this setup.
