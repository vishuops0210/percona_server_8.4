#!/usr/bin/env python3
"""Logical mysqldump backups taken from the ProxySQL host, with retention
pruning and status, optional verified-TLS MinIO publication.
No password arguments, shell interpolation, or in-place prepare.

Runs on the ProxySQL host, never on a database node. Discovers the current
secondaries (replicas) from the group members on every run, then takes one
consistent single-transaction mysqldump from the first one that answers over
TCP. Falls back to the primary only when no secondary answers, so the backup
read load stays off the writer. No incremental backups: every backup is a
standalone full dump, so restore is a plain SQL load.

Retention is grandfather-father-son (GFS): daily full backups are kept
7 days, one full per week is kept 7 weeks, and one full per month is
kept 12 months. Weekly and monthly keepers are full snapshots only;
binlogs are kept only for point-in-time recovery inside the daily
window, not for 12 months.
"""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Invalid backup identifier')
    return value


def run(argv, log=None):
    return subprocess.run(argv, check=True, text=True,
                          stdout=log or subprocess.PIPE,
                          stderr=subprocess.STDOUT if log else subprocess.PIPE)


def mysql_query(config, host, query):
    """Run one read-only query against a database host over TCP.

    Returns the stripped stdout. Raises CalledProcessError when the host
    does not answer, so callers can try the next host.
    """
    result = run([config.get('mysql', 'mysql'),
                  '--defaults-file=' + config['client_file'],
                  '-h', host, '-P', '3306',
                  '--batch', '--skip-column-names', '-e', query])
    return result.stdout.strip()


def find_backup_sources(config):
    """Return ordered (host, role) backup candidates, secondaries first.

    Asks the first database host that answers for the full member list
    (MEMBER_HOST, MEMBER_ROLE). Candidates follow db_hosts order, with
    SECONDARY members first and the PRIMARY last, so the backup reads
    from a replica and only falls back to the writer when no replica
    answers. A failover or a dead node is picked up on the next run,
    because nothing is hardcoded. MEMBER_HOST must match the inventory
    addresses (IPs here), or the member is skipped.
    """
    hosts = config.get('db_hosts') or []
    if not hosts:
        raise ValueError('No database hosts configured (backup_db_hosts)')
    roles = {}
    for host in hosts:
        try:
            rows = mysql_query(
                config, host,
                "SELECT MEMBER_HOST, MEMBER_ROLE FROM performance_schema.replication_group_members")
        except subprocess.CalledProcessError:
            continue
        for line in rows.splitlines():
            parts = line.split('\t')
            if len(parts) == 2:
                roles[parts[0].strip()] = parts[1].strip()
        if roles:
            break
    if not roles:
        raise ValueError('No database host answered; cannot find a backup source')
    candidates = ([(h, 'SECONDARY') for h in hosts if roles.get(h) == 'SECONDARY']
                  + [(h, 'PRIMARY') for h in hosts if roles.get(h) == 'PRIMARY'])
    if not candidates:
        raise ValueError('No configured database host is a group member')
    return candidates


def take_backup(config, kind):
    if kind != 'full':
        raise ValueError('Only full backups are supported; mysqldump has no incremental mode')
    root = Path(config['path'])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidates = find_backup_sources(config)
    backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:10]
    work = root / ('.partial-' + backup_id)
    work.mkdir(mode=0o700)
    dump_path = work / 'dump.sql.gz'
    dumped = None
    last_error = None
    for source_host, source_role in candidates:
        try:
            source = mysql_query(
                config, source_host,
                'SELECT @@server_uuid, @@version, @@GLOBAL.gtid_executed').split('\t')
            if len(source) != 3 or not source[1].startswith('8.4.'):
                raise ValueError('Backup source must be MySQL-compatible 8.4')
            source_uuid, version, gtid = source
            argv = [config.get('mysqldump', 'mysqldump'),
                    '--defaults-file=' + config['client_file'],
                    '-h', source_host, '-P', '3306',
                    '--single-transaction', '--routines', '--events', '--triggers',
                    '--source-data=2', '--set-gtid-purged=OFF', '--all-databases']
            # Stream the dump through the compressor in this process. Passing the
            # gzip file object as the child's stdout would write raw SQL straight
            # to the file descriptor, bypassing compression and corrupting the file.
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with gzip.open(dump_path, 'wb') as gz:
                shutil.copyfileobj(proc.stdout, gz, length=1024 * 1024)
            _, stderr = proc.communicate()
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(
                    proc.returncode, argv,
                    stderr=stderr.decode('utf-8', 'replace'))
            dumped = (source_host, source_role, source_uuid, version, gtid)
            break
        except (subprocess.CalledProcessError, ValueError) as exc:
            last_error = exc
            continue
    if dumped is None:
        raise last_error
    source_host, source_role, source_uuid, version, gtid = dumped
    binlog_file, binlog_pos = '', 0
    with gzip.open(dump_path, 'rt', errors='replace') as stream:
        head = stream.read(65536)
    match = re.search(r"SOURCE_LOG_FILE='([^']+)', SOURCE_LOG_POS=(\d+)", head)
    if match:
        binlog_file, binlog_pos = match.group(1), int(match.group(2))
    manifest = {'id': backup_id, 'kind': 'full', 'parent': None,
                'base': backup_id, 'source_uuid': source_uuid,
                'source_host': source_host, 'source_role': source_role,
                'server_version': version, 'gtid': gtid,
                'binlog_file': binlog_file, 'binlog_pos': binlog_pos,
                'files': {'dump.sql.gz': digest(dump_path)},
                'created_utc': datetime.now(timezone.utc).isoformat()}
    atomic_json(work / 'manifest.json', manifest)
    marker = {'manifest_sha256': digest(work / 'manifest.json')}
    # Upload data and manifest first; a final success marker is the publication boundary.
    if config['storage'] == 'minio':
        remote = config['remote'].rstrip('/') + '/' + backup_id
        mc = [config.get('mc', 'mc'), '--config-dir', config['mc_config_dir']]
        run(mc + ['mirror', '--overwrite', str(work), remote])
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            marker_file = Path(temporary) / '_SUCCESS.json'
            atomic_json(marker_file, marker)
            run(mc + ['cp', str(marker_file), remote + '/_SUCCESS.json'])
    atomic_json(work / '_SUCCESS.json', marker)
    os.replace(work, root / backup_id)
    return manifest


def load_complete(root, backup_id):
    folder = root / safe_id(backup_id)
    manifest = json.loads((folder / 'manifest.json').read_text())
    marker = json.loads((folder / '_SUCCESS.json').read_text())
    if manifest['id'] != backup_id or marker['manifest_sha256'] != digest(folder / 'manifest.json'):
        raise ValueError('Incomplete or modified backup manifest: ' + backup_id)
    return folder, manifest


def verify(folder, manifest):
    actual = {str(p.relative_to(folder)) for p in folder.iterdir() if p.is_file()
              and p.name not in ('manifest.json', '_SUCCESS.json')}
    if actual != set(manifest['files']):
        raise ValueError('Missing or unexpected backup files')
    for name, expected in manifest['files'].items():
        path = folder / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink():
            raise ValueError('Unsafe file in backup')
        if digest(path) != expected:
            raise ValueError('Checksum mismatch: ' + name)


def prepare(config, backup_id, workdir):
    root = Path(config['path'])
    folder, manifest = load_complete(root, backup_id)
    verify(folder, manifest)
    if manifest.get('kind') != 'full':
        raise ValueError('Only a full backup can be prepared')
    destination = Path(workdir)
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError('Restore workspace must be outside the backup store')
    if destination.exists():
        raise ValueError('Use a new, nonexistent restore workspace')
    destination.mkdir(mode=0o700, parents=True)
    prepared = destination / 'dump.sql'
    with gzip.open(folder / 'dump.sql.gz', 'rb') as source, prepared.open('wb') as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    return {'prepared': str(prepared), 'gtid': manifest.get('gtid', ''),
            'source_uuid': manifest['source_uuid'], 'backup_id': backup_id}


def list_backups(root):
    """Split stored backups into complete and suspect, oldest first.

    Complete means manifest.json plus _SUCCESS.json with a matching checksum.
    Suspect entries are reported, never deleted.
    """
    complete, suspect = [], []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith('.partial-'):
            continue
        manifest_path = folder / 'manifest.json'
        marker_path = folder / '_SUCCESS.json'
        if not manifest_path.exists() or not marker_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            marker = json.loads(marker_path.read_text())
            if marker.get('manifest_sha256') != digest(manifest_path):
                raise ValueError('manifest checksum mismatch')
            manifest['_folder'] = folder
            complete.append(manifest)
        except (OSError, ValueError, KeyError) as error:
            suspect.append((folder.name, str(error)))
    complete.sort(key=lambda item: item.get('created_utc', ''))
    return complete, suspect


def status(config):
    root = Path(config['path'])
    complete, suspect = list_backups(root)
    latest = complete[-1] if complete else None
    result = {'complete_backups': len(complete),
              'suspect_backups': [name for name, _ in suspect],
              'latest': None}
    if latest:
        created = datetime.fromisoformat(latest['created_utc'])
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        result['latest'] = {'id': latest['id'], 'kind': latest['kind'],
                            'created_utc': latest['created_utc'],
                            'age_hours': round(age_hours, 1)}
    return result


def gfs_keepers(complete, retention_weeks, retention_months, now=None):
    """Pick one full backup per week and one per month to keep.

    Weekly keeper is the last full in each ISO week, monthly keeper is
    the last full in each calendar month. Falls back to the last full
    in that week or month when the ideal day was missed, so a missed
    Sunday or 1st does not lose the whole tier. Only full backups are
    keepers, so a keeper is always a standalone restorable snapshot.
    """
    now = now or datetime.now(timezone.utc)
    fulls = [item for item in complete if item.get('kind') == 'full']
    weekly, monthly = set(), set()
    current_month = now.year * 12 + now.month
    by_week, by_month = {}, {}
    for item in fulls:
        created = datetime.fromisoformat(item['created_utc'])
        by_week.setdefault(created.isocalendar()[:2], []).append(item)
        by_month.setdefault((created.year, created.month), []).append(item)
    for key, items in by_week.items():
        # ISO week start, Monday, in UTC.
        week_start = datetime.fromisocalendar(key[0], key[1], 1).replace(tzinfo=timezone.utc)
        if (now - week_start).days > retention_weeks * 7:
            continue
        weekly.add(sorted(items, key=lambda x: x['created_utc'])[-1]['id'])
    for (year, month), items in by_month.items():
        if current_month - (year * 12 + month) >= retention_months:
            continue
        monthly.add(sorted(items, key=lambda x: x['created_utc'])[-1]['id'])
    return weekly, monthly


def prune(config, retention_days, retention_weeks=7, retention_months=12):
    """Delete complete backups outside GFS retention, keeping the newest chain.

    Daily: every backup younger than retention_days is kept.
    Weekly: one full per week is kept for retention_weeks weeks.
    Monthly: one full per month is kept for retention_months months.
    The newest complete backup and its whole ancestor chain are always
    kept, so pruning can never remove the only restorable chain. Suspect
    entries are reported and left alone. Stale partial directories from
    crashed runs are removed after 24 hours.
    """
    root = Path(config['path'])
    complete, suspect = list_backups(root)
    by_id = {item['id']: item for item in complete}
    protected = set()
    if complete:
        current = complete[-1]['id']
        while current and current in by_id and current not in protected:
            protected.add(current)
            current = by_id[current].get('parent')
    weekly, monthly = gfs_keepers(complete, retention_weeks, retention_months)
    protected |= weekly | monthly
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    pruned = []
    for manifest in complete:
        if manifest['id'] in protected:
            continue
        created = datetime.fromisoformat(manifest['created_utc']).timestamp()
        if created >= cutoff:
            continue
        if config['storage'] == 'minio':
            remote = config['remote'].rstrip('/') + '/' + manifest['id']
            mc = [config.get('mc', 'mc'), '--config-dir', config['mc_config_dir']]
            run(mc + ['rm', '--recursive', '--force', remote])
        shutil.rmtree(manifest['_folder'])
        pruned.append(manifest['id'])
    for folder in root.iterdir():
        if folder.is_dir() and folder.name.startswith('.partial-'):
            age = datetime.now(timezone.utc).timestamp() - folder.stat().st_mtime
            if age > 86400:
                shutil.rmtree(folder)
                pruned.append(folder.name + ' (partial)')
    remaining = [item for item in complete if item['id'] not in pruned]
    # Binlogs are only needed for point-in-time recovery inside the daily
    # window. A monthly keeper is a full snapshot, it does not need a
    # year of binlogs behind it, so prune binlogs against the daily
    # backups only, not against the oldest GFS keeper.
    daily_remaining = [item for item in remaining
                       if datetime.fromisoformat(item['created_utc']).timestamp() >= cutoff]
    binlog_base = daily_remaining if daily_remaining else remaining[-1:]
    pruned_binlogs = prune_binlogs(config, binlog_base)
    return {'pruned': pruned, 'protected': sorted(protected),
            'gfs_weekly_kept': sorted(weekly), 'gfs_monthly_kept': sorted(monthly),
            'suspect_skipped': [name for name, _ in suspect],
            'pruned_binlogs': pruned_binlogs}


def prune_binlogs(config, remaining):
    """Delete archived binlogs older than the oldest kept backup.

    Point-in-time recovery replays binlogs written after a full backup, so a
    binlog older than the oldest remaining complete backup (minus a day of
    margin) can never be needed. When no complete backup remains, everything
    is kept.
    """
    pruned = []
    binlog_path = config.get('binlog_path')
    if not binlog_path or not remaining:
        return pruned
    archive = Path(binlog_path)
    if not archive.is_dir():
        return pruned
    oldest = min(datetime.fromisoformat(item['created_utc']).timestamp()
                 for item in remaining)
    cutoff = oldest - 86400
    for entry in archive.iterdir():
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            entry.unlink()
            pruned.append(entry.name)
    return pruned


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/percona-backup/settings.json')
    sub = parser.add_subparsers(dest='command', required=True)
    backup = sub.add_parser('backup')
    backup.add_argument('kind', choices=['full'])
    restoration = sub.add_parser('prepare')
    restoration.add_argument('--backup-id', required=True)
    restoration.add_argument('--workdir', required=True)
    pruning = sub.add_parser('prune')
    pruning.add_argument('--retention-days', type=int, required=True)
    pruning.add_argument('--retention-weeks', type=int, default=7)
    pruning.add_argument('--retention-months', type=int, default=12)
    sub.add_parser('status')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    root = Path(config['path']); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.command == 'status':
        print(json.dumps(status(config)))
        return
    with (root / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'backup':
            result = take_backup(config, args.kind)
        elif args.command == 'prepare':
            result = prepare(config, args.backup_id, args.workdir)
        else:
            result = prune(config, args.retention_days, args.retention_weeks, args.retention_months)
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print('Backup workflow failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
