# Group Replication interview questions and answers

Questions people ask about MySQL Group Replication, with answers written
against the lab setup this repo was tested on. Use it to explain the
cluster to a team, or to prepare for an interview.

The setup these answers refer to: three Percona Server 8.4.11 nodes in
single primary mode, db1 192.168.8.56, db2 192.168.8.57, db3
192.168.8.54, fronted by ProxySQL on 192.168.8.55. Group name is a UUID
shared by all three members. GTID is on, binlogs are row based,
`group_replication_consistency` is `BEFORE_ON_PRIMARY_FAILOVER`, and
`group-replication-start-on-boot` is ON.

## 1. Fundamentals

### Q1. What is MySQL Group Replication, in one paragraph?

It is three or more MySQL servers acting as one database. They form a
group, replicate every write to every member, and agree on the order of
transactions before any of them commits. One member takes writes, the
others take reads and stand by. If the writer dies, the survivors elect
a new one on their own. If a member drops out, it rejoins and catches
up on its own. The group keeps the data identical on every member, so
there is no separate replica that lags behind and no manual failover.

### Q2. Single primary vs multi primary: what is the difference, and which one are we running?

In single primary mode, one member accepts writes and the rest are
read only. In multi primary mode, every member accepts writes and
conflicts are settled by certification: the first write to be certified
wins and the other rolls back. We run single primary mode. It is the
safer default: no write conflicts by construction, and every
application writes to one place. Multi primary only makes sense when
the application is built to handle rolled back writes and genuinely
needs to write on several nodes at once.

### Q3. Why three nodes? Why not two, or five?

Three is the minimum for majority based fault tolerance. The group
makes decisions by majority, and with three nodes it can lose one and
still have a majority of two, so it keeps working. With two nodes,
losing one leaves no majority and the group stops taking writes. Five
tolerates two failures, but every write pays for consensus across more
members, so it is slower and heavier. Three is the standard choice for
high availability: one failure tolerated, lowest cost.

### Q4. What is a GTID and why does Group Replication need it?

A GTID is a global transaction ID: the UUID of the server that created
the transaction plus a sequence number, for example
`45eb1c1a-b6a0-49b3-95cb-7f32d0e162d8:1-13`. It gives every
transaction one identity on every member. The group needs it for three
things: certification, so members can agree on which transactions
exist; recovery, so a rejoining member knows exactly which transactions
it missed; and failover, so the new primary knows what it has already
applied. Without GTIDs the members could not prove they hold the same
data.

### Q5. What ports does Group Replication use, and what is each for?

3306 is the MySQL client port, where applications and ProxySQL connect.
33060 is the X protocol port. 33061 is the group communication port,
where the members talk to each other, exchange transactions and run
consensus. If 33061 is blocked between two members, they cannot see
each other as group members even though MySQL itself answers on 3306.
That distinction matters when diagnosing a member that will not join.

## 2. The write path

### Q6. Walk me through what happens when an application writes a row.

1. The app sends the INSERT to ProxySQL on port 6033.
2. ProxySQL sends it to the current writer. It knows who the writer is
   because it checks `read_only` on all three nodes every 1.5 seconds,
   and only the primary has `read_only` OFF.
3. The primary runs the transaction and writes it to its binlog, row
   format, with a GTID attached.
4. The transaction goes to the group over port 33061 and is certified:
   the group checks it against the transactions every member has
   already applied.
5. Once certified, the primary commits, and the secondaries apply the
   same transaction through their applier channel. GTIDs end up
   identical on all three, so all three hold the same data.
6. Reads can go to any node, so ProxySQL sends reads to the
   secondaries. Writes go only to the primary.

### Q7. What is certification, and what happens when two members write the same row?

Certification is the group's conflict check. Before a transaction
commits, the group compares its write set with the write sets of all
transactions already certified. If two transactions wrote the same
row, the first one certified wins and the second one is rejected: the
application gets an error and the transaction rolls back. In single
primary mode this almost never fires, because only one node takes
writes, but it is the guard that stops the members from diverging.
Certification is what makes the replication synchronous in effect,
even though the secondaries apply the transaction a moment after the
primary commits.

### Q8. Why are secondaries read only? What is super_read_only?

Secondaries run with `read_only` ON and `super_read_only` ON, and
Group Replication manages those flags itself. They exist so a stray
write to a secondary fails fast with error 1290 instead of silently
diverging the data. If a secondary accepted a write, its data would
differ from the group and no certification would catch it, because
the write never went through the group. The flags are the fence that
keeps all writes flowing through the primary and through
certification. An application that writes to a secondary by mistake
learns about it at once, which is the correct behaviour.

### Q9. What does group_replication_consistency BEFORE_ON_PRIMARY_FAILOVER do?

It controls what a new primary must do before it accepts writes after
a failover. With `BEFORE_ON_PRIMARY_FAILOVER`, the new primary applies
every transaction the old primary had committed before it takes its
first write. That is what makes failover lossless: no committed
transaction is lost when the writer changes. The cost is a short
pause on the new primary while it catches up. The other common value,
`EVENTUAL`, lets the new primary take writes at once and catch up in
the background, which is faster but can serve stale reads for a
moment. For a setup that promises no lost writes, the BEFORE value is
the right one.

## 3. Failover and quorum

### Q10. The primary dies. What happens, step by step?

1. The group notices within seconds, because the dead member stops
   answering on port 33061.
2. The survivors need a majority: 2 out of 3. They elect a new
   primary, lowest server UUID among the survivors, since all member
   weights are equal in this setup.
3. Before the new primary takes its first write, it applies every
   transaction the old primary had committed. That is the
   `BEFORE_ON_PRIMARY_FAILOVER` setting from Q9, and it is what makes
   the failover lossless.
4. The new primary turns `super_read_only` OFF and starts taking
   writes.
5. ProxySQL sees `read_only` flip on its next 1.5 second check and
   moves the writer to the new primary.
6. The write that was in flight at the moment of failure fails and
   the application must retry it, but the next write goes to the new
   primary. Visible downtime is a few seconds, and no committed data
   is lost.

### Q11. How does ProxySQL know the writer changed?

ProxySQL runs a monitor user against all three backends. Every 1500
ms it checks `read_only` on each one (`mysql-monitor_read_only_interval`).
Only the primary has `read_only` OFF, so the monitor always knows
which node is the writer. It also pings the backends every 10000 ms
and checks replication lag every 10000 ms against
`proxysql_max_transactions_behind`, which is 100 transactions here:
a replica more than 100 transactions behind the primary is shunned
for reads until it catches up. When the writer changes, ProxySQL
moves it to writer hostgroup 10 and the old writer, if it is still
reachable, to reader hostgroup 20. The application never has to know
the new writer's address: it keeps talking to ProxySQL on 6033.

### Q12. Two of three nodes are down. Can the last one take writes? Why not?

No. The group makes decisions by majority, and one node out of three
is not a majority. The surviving member cannot elect a primary, cannot
certify transactions, and cannot be sure it is the real group rather
than a partitioned half. So it blocks writes instead of guessing. That
is by design, not a bug: it is what prevents a split brain, where two
sides each think they are the writer and accept different data. Reads
on the surviving member still work. Writes resume as soon as a second
member is back and a majority exists again.

### Q13. How is the new primary chosen?

By election among the surviving members. The candidates are sorted by
member weight first, highest wins, then by server UUID, lowest wins.
This setup does not set member weights, so every member has the
default weight and the lowest UUID wins. That is deterministic: given
the same set of survivors, the same member is elected every time. If
you want a particular node to be preferred as primary, set a higher
`group_replication_member_weight` on it.

## 4. Bootstrap and recovery

### Q14. How do you bootstrap a group for the first time?

On the seed node only, and only once: `SET GLOBAL
group_replication_bootstrap_group=ON`, then `START GROUP_REPLICATION`,
then `SET GLOBAL group_replication_bootstrap_group=OFF`. The other
members just run `START GROUP_REPLICATION` and join the group the seed
started. Bootstrap is a one time act. Running it twice creates two
separate groups that do not know each other, and the second bootstrap
will fail the assertion in the role that an established group is never
re-bootstrapped. The role's bootstrap task runs only when no member is
online, which is exactly the first start condition.

### Q15. A node restarts and rejoins. What happens?

mysqld starts, and because `group-replication-start-on-boot` is ON,
the node tries to join the group on its own, with no operator action.
It connects to the group seeds on port 33061, the group sends it the
transactions it missed through the recovery channel, it applies them
until it is caught up, then it becomes ONLINE and starts serving reads
again. If the node was down for a long time and the transactions it
missed have already been purged from the other members' binlogs, it
cannot catch up on its own and needs a fresh copy of the data, a
manual restore or a clone, before it can rejoin.

### Q16. What is distributed recovery, and what is the recovery channel?

Distributed recovery is how a joining or rejoining member catches up.
It has two parts. First, the member connects to a donor member over
the `group_replication_recovery` channel, using a dedicated recovery
account, and asks for the transactions it missed. Second, it applies
them through its applier until its GTID set matches the group's. Only
then does it become ONLINE. The channel is separate from the normal
replication stream so that catch up traffic does not mix with live
writes. You can see its state in
`performance_schema.replication_connection_status` for the channel
`group_replication_recovery`: when a member is OFFLINE after a failed
join, that channel shows SERVICE_STATE OFF and no error, which tells
you it never got a donor, rather than that it hit a bad password.

## 5. ProxySQL

### Q17. What does ProxySQL do in this setup?

Three jobs. First, connection pooling: applications hold many frontend
connections on port 6033, and ProxySQL serves them from a small pool of
backend connections to MySQL, at most 100 per node here. Second,
read/write split: writes go to writer hostgroup 10, reads go to reader
hostgroup 20. Third, failover routing: it watches `read_only` on all
three backends and moves the writer role to whichever node is primary,
so the application never has to know the writer's address. The admin
interface is on port 6032 and is only reachable from the ProxySQL host
itself.

### Q18. How does ProxySQL detect a failed primary, and how fast?

Through its monitor user, on three timers. Connect checks every 2000
ms. `read_only` checks every 1500 ms: that is the failover detector,
because only the primary has `read_only` OFF. Ping checks every 10000
ms. Replication lag checks every 10000 ms against the 100 transaction
limit. So a dead primary is noticed in about 1.5 to 2 seconds, and the
writer moves on the next monitor pass. The application still has to
retry the write that was in flight when the primary died, because that
write never got an answer. Everything after the flip goes to the new
primary.

### Q19. What is multiplexing, and when does a connection get pinned?

Multiplexing is ProxySQL's pooling: one backend connection serves many
frontend connections in turn, because most frontend connections are
idle most of the time. It only works when the frontend connection holds
no session state. As soon as a connection holds state, an open
transaction, a temporary table, a prepared statement, a session
variable, or a lock, ProxySQL pins that frontend connection to one
backend connection until the state clears. That is correct behaviour,
not a leak: the state lives on that one backend connection and cannot
move. Pooling is per proxy, so with two proxies each keeps its own
pool of up to 100 connections per node.

## 6. Troubleshooting, from this lab

### Q20. The first deploy fails with MySQL error 3093. What is it and how do you fix it?

Error 3093 means `START GROUP_REPLICATION` failed because the group
is already running on that member. The cause in this lab was
`group-replication-start-on-boot = ON`: mysqld auto starts Group
Replication when it boots, so a member can already have joined by
itself before the role's join task runs. The role then issued START
unconditionally and collided with the running group. The fix has two
parts. First, re-check the member's real state right before START
instead of trusting the fact read at the start of the play. Second,
treat 3093 itself as success, because it only means the member is
already in. The retry loop alone cannot fix it, because 3093 is not
transient: it fails identically on every retry. That is why the first
run failed and the second run, with no changes, passed: on the second
run the member was already online, so the join was skipped.

### Q21. A member shows OFFLINE in replication_group_members and never becomes ONLINE. Why, and what is the fix?

Two layers. The trigger: the member's auto start tried to join while
the seed was still bootstrapping, its connections to the peers' port
33061 failed at that moment, the join failed and the member left the
group, ending OFFLINE with Group Replication not running. You can see
it in the error log: `Error on opening a connection to peer node`,
`The member was unable to join the group`, `The member is leaving a
group without being on one`. The trap: the role's re-check decided a
member needs START only when its query returned zero rows, but an
OFFLINE member still returns one row, itself, OFFLINE. So START was
skipped on every run and the wait task looped 60 retries for an
ONLINE that could never happen, because nothing ever issued START.
The permanent fix checks `MEMBER_STATE`, not row presence: START is
issued when there is no row or the row is OFFLINE, and skipped only
when the row is ONLINE or RECOVERING. After that fix the same fresh
deploy went green on the first run, twice. It was not a firewall, ufw
is inactive and iptables is all ACCEPT, and it was not a crash, mysqld
was active with zero restarts. It was a race at bootstrap time plus a
wrong check in the role.

### Q22. An application gets ERROR 1290 on a write. What happened?

The write went to a secondary. Error 1290 is `super_read_only`: the
node the application reached is not the primary. Either the
application connected directly to a secondary instead of going through
ProxySQL, or ProxySQL had not yet noticed a failover and was still
routing to the old writer. Check which node the session is on with
`SELECT @@hostname, @@read_only`, and check the group members to see
which one is PRIMARY. Route writes through ProxySQL on 6033 and the
problem goes away, because ProxySQL only sends writes to the member
with `read_only` OFF.

### Q23. SHOW GRANTS says the app user has ALL on app01, but CREATE TABLE fails with error 1142. What do you check first?

Which database the session is in. Run `SELECT DATABASE()`. Error
1142 is a privilege denial from MySQL itself, not a ProxySQL problem:
the grant is per database, and ALL on app01 means nothing in any
other database. In this lab that is exactly what happened: the grant
was fine and the session was simply in the wrong database. `USE
app01` and retry. If `SHOW GRANTS` shows something else, for example
the grant on the wrong database or a missing CREATE privilege, then
the grant itself is wrong and you fix the `proxysql_users` entry and
rerun `proxysql.yml`. Check the session database before assuming the
grant is broken.

### Q24. A member will not join and the log shows GCS connection errors to the peers. Is it the firewall?

Check, do not assume. From the member, test TCP to each peer on
33061, check `ufw status` and `iptables -L`. In this lab the answer
was no: ufw was inactive, iptables was all ACCEPT, and TCP to both
peers on 33061 was OPEN, yet the log showed the same GCS errors. The
errors were a moment in time condition: the member tried to join while
the seed was still bootstrapping, so the peers were not ready yet.
The way to tell the two apart is timing. A firewall blocks every
attempt, every time. A bootstrap race fails once, at the moment the
group is forming, and a retry after the group is up succeeds. Always
prove it with a live connection test before changing firewall rules.

## 7. Design tradeoffs

### Q25. When would you NOT use Group Replication?

When the write path cannot pay for consensus. Every write needs a
certification round trip across the group, so cross region groups with
high latency are slow, and very large transactions stall certification
for everyone. It also caps at 9 members, and it is a poor fit for
workloads where several nodes must write the same rows at once,
because certification will roll back all but one. If the requirement
is a delayed replica for point in time recovery, or a reporting
replica that may lag, plain async replication is simpler and cheaper.
Group Replication is for high availability inside one low latency
network, where automatic failover with no lost writes is the
requirement.

### Q26. How does Group Replication compare with classic async replication?

Classic async replication is one way: the primary writes its binlog
and the replicas apply it when they can. A replica can lag, failover
is manual, and a failover can lose the transactions the replica had
not applied yet. Group Replication is agreed replication: every write
is certified by the group before it commits, failover is automatic,
and with `BEFORE_ON_PRIMARY_FAILOVER` no committed write is lost.
The price is a consensus round trip on every write, so latency is
higher and maximum throughput is lower, and writes need a majority of
members alive. Async is simpler and faster for one way copies. Group
Replication is the choice when the database itself must survive a
node loss without an operator and without losing data.

### Q27. What are the RPO and RTO of this setup?

RPO is zero on failover: every committed write is certified by the
group and the new primary applies all of it before taking writes, so
nothing committed is lost. RTO is a few seconds: detection in about
1.5 to 2 seconds through ProxySQL's `read_only` monitor, election
among the survivors, then the writer flip. The one exception is the
write that was in flight at the exact moment the primary died: it gets
no answer and the application must retry it. That is not data loss,
the write simply never committed, but the application has to handle
the retry. For disaster recovery beyond a single node loss, RPO and
RTO come from the backup design, not from Group Replication: that is
the mysqldump backup from the ProxySQL host, which is a separate
topic.

---

Last verified against the live lab: cluster green on a fresh deploy,
all three members ONLINE, ProxySQL routing writes to the primary,
2026-11-11.
