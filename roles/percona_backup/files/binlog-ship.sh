#!/bin/sh
# Download finished (rotated) binary logs from the primary to ARCHIVE_DIR
# for point-in-time recovery. Runs every few minutes from cron on the
# ProxySQL host. Uses mysqlbinlog --read-from-remote-server, so no backup
# software runs on any database node. Each run first seals the active log
# with FLUSH BINARY LOGS, so the last few minutes become a closed file and
# ship; without it a lost primary loses everything since the last rotation.
# The new active log is skipped because the server is still writing to it;
# rotated logs are immutable, so a missing file is always safe to download.
# Uses the protected TCP client file, never a password on the command line.
set -eu
ARCHIVE_DIR="${1:?archive directory}"
CLIENT_CNF="${2:?client defaults file}"
DB_HOSTS="${3:?space-separated database hosts}"
mkdir -p "$ARCHIVE_DIR"
MYSQL="mysql --defaults-file=$CLIENT_CNF --batch --skip-column-names"
# Find the primary by asking each database host in turn.
PRIMARY=""
for host in $DB_HOSTS; do
  PRIMARY="$($MYSQL -h "$host" -P 3306 -e "SELECT MEMBER_HOST FROM performance_schema.replication_group_members WHERE MEMBER_ROLE='PRIMARY'" 2>/dev/null || true)"
  if [ -n "$PRIMARY" ]; then break; fi
done
if [ -z "$PRIMARY" ]; then echo "binlog-ship: no primary found" >&2; exit 1; fi
# Seal the active log so the last few minutes of transactions become a
# closed, shippable file. FLUSH needs the RELOAD privilege, which the
# backup user has. A failed flush only warns; already rotated logs still
# ship, and the next run retries.
if ! $MYSQL -h "$PRIMARY" -P 3306 -e 'FLUSH BINARY LOGS' 2>/dev/null; then
  echo "binlog-ship: FLUSH BINARY LOGS failed on $PRIMARY, shipping rotated logs only" >&2
fi
LOGS="$($MYSQL -h "$PRIMARY" -P 3306 -e 'SHOW BINARY LOGS' | cut -f1)"
ACTIVE="$(printf '%s\n' "$LOGS" | tail -n 1)"
printf '%s\n' "$LOGS" | while IFS= read -r log; do
  [ -n "$log" ] || continue
  if [ "$log" = "$ACTIVE" ]; then
    continue
  fi
  if [ ! -f "$ARCHIVE_DIR/$log" ]; then
    mysqlbinlog --defaults-file="$CLIENT_CNF" --read-from-remote-server \
      --host="$PRIMARY" --port=3306 --raw --result-file="$ARCHIVE_DIR/" "$log"
    chmod 600 "$ARCHIVE_DIR/$log"
  fi
done
