"""
Shared Percona / MySQL helpers used by the plugins.

All database access goes through the `mysql` command-line client with a
--defaults-file, exactly like the Ansible role. That way no password is ever
passed on the command line and we do not need a Python MySQL driver installed.
"""

import subprocess

DEFAULT_CLIENT_FILE = "/etc/percona-backup/client.cnf"


def mysql_query(cfg, host, sql):
    """Run one read-only query against a database host over TCP and return stdout.

    Raises subprocess.CalledProcessError if the host does not answer, so callers
    can try the next host.
    """
    argv = [
        cfg.get("mysql_bin", "mysql"),
        "--defaults-file=" + cfg.get("client_file", DEFAULT_CLIENT_FILE),
        "-h", host,
        "-P", str(cfg.get("port", 3306)),
        "--batch", "--skip-column-names",
        "-e", sql,
    ]
    result = subprocess.run(argv, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


def discover(cfg):
    """Discover the group members and their roles.

    Returns a tuple (members, sources, primary):
    members: list of (host, role) for every configured db host
    sources: hosts ordered secondaries-first (preferred backup read order)
    primary: the PRIMARY host, or None

    Asks each configured db host in turn until one answers, so a dead node or a
    failover is handled automatically on the next call. MEMBER_HOST must match
    the inventory addresses (the IPs in db_hosts), or the member is skipped.
    """
    hosts = cfg.get("db_hosts") or []
    if not hosts:
        raise ValueError("no db_hosts configured")

    roles = {}
    for host in hosts:
        try:
            rows = mysql_query(
                cfg, host,
                "SELECT MEMBER_HOST, MEMBER_ROLE "
                "FROM performance_schema.replication_group_members")
        except subprocess.CalledProcessError:
            continue
        for line in rows.splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                roles[parts[0].strip()] = parts[1].strip()
        if roles:
            break

    members = [(h, roles.get(h, "UNKNOWN")) for h in hosts]
    secondaries = [h for h in hosts if roles.get(h) == "SECONDARY"]
    primaries = [h for h in hosts if roles.get(h) == "PRIMARY"]
    sources = secondaries + primaries
    primary = primaries[0] if primaries else None
    return members, sources, primary
