# DB Admin: real MySQL / ProxySQL admin app

A small web app you host with Docker Compose behind nginx on app.tyagi.fun, over HTTPS with your own certificates.

## What it does

1. Connections page: live MySQL processlist, grouped by user. See who is
   connected, from which host, to which database, and what query they are
   running. Auto refreshes every 3 seconds.
2. Explorer page: connect as a specific DB user, pick a database, list
   tables, view rows, insert a row, edit a row, delete a row, create a
   table, drop a table. Plus a read only SQL box for SELECT, SHOW,
   DESCRIBE and EXPLAIN.
3. Failover Test page: continuous writes, one per interval, with a live
   log showing OK, ERROR and RECOVERED, the current writer hostname,
   and how many seconds an outage lasted. Start it, stop the primary
   MySQL with sudo systemctl stop mysql on the primary VM, watch ERROR
   lines, then RECOVERED with the new writer, then start the old
   primary again, it rejoins as SECONDARY. You can use auto mode, which
   inserts app_name, seq and payload into your chosen table, or type
   your own INSERT with {seq} and {app} placeholders, for example for
   a student table: INSERT INTO testdb.student (name) VALUES
   ('failover-test-{seq}'). If you do not have a suitable table, use
   Create failover_test table on that page, if your user has CREATE
   privilege, otherwise create it as root on the writer first.

The app works as the DB user you type in. It has no privilege of its own.

## Host it on your server

Prereqs on the server: Docker and Docker Compose, a DNS A record, and
your certificates for app.tyagi.fun:

- app.tyagi.fun -> your server public IP

On the server:

```
unzip db-admin-app.zip
cd db-admin-app
cp .env.example .env
nano .env
```

Set a strong ADMIN_PASSWORD and a long random SECRET_KEY in .env. This
login protects the whole site, do not leave the default on a public domain.

Put your certificates in the certs/ folder:

```
cp /path/to/your/fullchain.pem certs/fullchain.pem
cp /path/to/your/privkey.pem certs/privkey.pem
```

If your files have different names, rename them to fullchain.pem and
privkey.pem when you copy them, or edit the ssl_certificate lines in
nginx/default.conf to match your names. Do NOT send the private key in
chat, and do NOT commit the certs/ folder to a public repo.

Then:

```
docker compose up -d --build
docker compose ps
docker compose logs -f app
docker compose logs -f nginx
```

Open https://app.tyagi.fun in a browser, log in with the admin user
from .env. HTTP on port 80 automatically redirects to HTTPS on 443.

When your certificate expires, replace the two files in certs/ and run
docker compose restart nginx. No certbot step is needed here, because
you already have your own certificates for app.tyagi.fun.

## Test it against Percona / ProxySQL

Note: the 5 node Percona test cluster from 2026-10-27 was terminated, so
for a live test you need a running cluster again, or any MySQL server.

In the app, after admin login, connect with:

- Host: ProxySQL private IP, if the app server can reach it, otherwise
  ProxySQL public IP, but only if port 6033 is open to the app server
- Port: 6033 for ProxySQL, 3306 for direct MySQL
- User: testuser2, or any user you want to test
- Password: that user's password

Then:

- Connections tab: you should see your own connection, plus other app
  connections if the connected user has PROCESS privilege. Without
  PROCESS, MySQL shows only your own connections.
- Explorer tab: pick testdb2, open app_traffic, add a row, edit it,
  delete it. Create a test table, then drop it.

If the app server is not in the same VPC as ProxySQL, the private IP
will not work. In that case either host this app inside the VPC, or open
ProxySQL port 6033 only to the app server public IP in the security group,
never to 0.0.0.0/0.

## One user, two databases: yes

Yes, one MySQL user can manage two databases, or many. A grant is per
database, and you can give the same user grants on both.

Direct MySQL, as root:

```
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP
  ON appdb1.* TO 'appuser'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP
  ON appdb2.* TO 'appuser'@'%';
FLUSH PRIVILEGES;
```

With the Percona role, proxysql_users priv is passed straight to
community.mysql.mysql_user, which takes multiple databases separated by
a slash, in one string:

```
proxysql_users:
  - name: appuser
    password: '{{ vault_appuser_password }}'
    priv: 'appdb1.*:SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,DROP/appdb2.*:SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,DROP'
```

Then rerun proxysql.yml. That user can then use both databases through
ProxySQL port 6033, and in this app you will see both databases in the
database dropdown, and you can switch between them without reconnecting.

Do not use ON *.* for a normal app user. That gives every database,
including mysql, and is not least privilege.
