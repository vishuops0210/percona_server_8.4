#!/bin/sh
# Copy finished (rotated) binary logs to ARCHIVE_DIR for point-in-time recovery.
# Runs every few minutes from cron. The active log is skipped because the
# server is still writing to it; rotated logs are immutable, so a missing
# file in the archive is always safe to copy. Uses the protected socket-based
# client file, never TCP and never a password on the command line.
set -eu
ARCHIVE_DIR="${1:?archive directory}"
CLIENT_CNF="${2:?client defaults file}"
mkdir -p "$ARCHIVE_DIR"
MYSQL="mysql --defaults-file=$CLIENT_CNF --batch --skip-column-names"
DATADIR="$($MYSQL -e "SELECT @@datadir")"
LOGS="$($MYSQL -e 'SHOW BINARY LOGS' | cut -f1)"
ACTIVE="$(printf '%s\n' "$LOGS" | tail -n 1)"
printf '%s\n' "$LOGS" | while IFS= read -r log; do
  [ -n "$log" ] || continue
  if [ "$log" = "$ACTIVE" ]; then
    continue
  fi
  if [ ! -f "$ARCHIVE_DIR/$log" ]; then
    cp -p "$DATADIR/$log" "$ARCHIVE_DIR/$log"
    chmod 600 "$ARCHIVE_DIR/$log"
  fi
done
