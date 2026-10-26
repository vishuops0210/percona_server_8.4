#!/usr/bin/env python3
"""Local staged XtraBackup 8.4 chains with retention pruning and status,
optional verified-TLS MinIO publication.
No password arguments, shell interpolation, or in-place prepare.
"""
import argparse
import fcntl
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


def checkpoints(path):
    result = {}
    for line in path.read_text().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            result[k.strip()] = v.strip()
    for key in ('from_lsn', 'to_lsn'):
        if not result.get(key, '').isdigit():
            raise ValueError('Missing/invalid checkpoint ' + key)
        result[key] = int(result[key])
    return result


def load_complete(root, backup_id):
    folder = root / safe_id(backup_id)
    manifest = json.loads((folder / 'manifest.json').read_text())
    marker = json.loads((folder / '_SUCCESS.json').read_text())
    if manifest['id'] != backup_id or marker['manifest_sha256'] != digest(folder / 'manifest.json'):
        raise ValueError('Incomplete or modified backup manifest: ' + backup_id)
    return folder, manifest


def verify(folder, manifest):
    actual = {str(p.relative_to(folder / 'data')) for p in (folder / 'data').rglob('*') if p.is_file()}
    if actual != set(manifest['files']):
        raise ValueError('Missing or unexpected backup files')
    for name, expected in manifest['files'].items():
        path = folder / 'data' / name
        if Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink():
            raise ValueError('Unsafe file in backup')
        if digest(path) != expected:
            raise ValueError('Checksum mismatch: ' + name)


def take_backup(config, kind):
    root = Path(config['path'])
    state_path = root / 'state.json'
    source = run([config.get('mysql', 'mysql'), '--defaults-file=' + config['client_file'],
                  '--batch', '--skip-column-names', '-e', 'SELECT @@server_uuid, @@version']).stdout.strip().split('\t')
    if len(source) != 2 or not source[1].startswith('8.4.'):
        raise ValueError('Backup source must be MySQL-compatible 8.4')
    source_uuid, version = source
    previous = None
    if kind == 'incremental':
        previous = json.loads(state_path.read_text())
        _, parent = load_complete(root, previous['id'])
        if parent['source_uuid'] != source_uuid or previous['to_lsn'] != parent['to_lsn']:
            raise ValueError('Source identity or previous LSN changed; take a new full backup')
    backup_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:10]
    work = root / ('.partial-' + backup_id)
    work.mkdir(mode=0o700)
    data = work / 'data'
    argv = [config.get('xtrabackup', 'xtrabackup'), '--defaults-file=' + config['client_file'],
            '--backup', '--target-dir=' + str(data), '--parallel=' + str(config['parallel'])]
    if previous:
        argv.append('--incremental-lsn=' + str(previous['to_lsn']))
    with (work / 'backup.log').open('w') as log:
        run(argv, log)
    cp = checkpoints(data / 'xtrabackup_checkpoints')
    expected_from = previous['to_lsn'] if previous else 0
    if cp['from_lsn'] != expected_from or cp['to_lsn'] < cp['from_lsn']:
        raise ValueError('Backup checkpoint continuity failed')
    files = {}
    for path in sorted(data.rglob('*')):
        if path.is_symlink():
            raise ValueError('External/symlink tablespaces require a separate restore procedure')
        if path.is_file():
            files[str(path.relative_to(data))] = digest(path)
    binlog_info = data / 'xtrabackup_binlog_info'
    fields = binlog_info.read_text().strip().split() if binlog_info.exists() else []
    manifest = {'id': backup_id, 'kind': kind, 'parent': previous['id'] if previous else None,
                'base': previous['base'] if previous else backup_id,
                'source_uuid': source_uuid, 'server_version': version,
                'from_lsn': cp['from_lsn'], 'to_lsn': cp['to_lsn'], 'files': files,
                'gtid': fields[2] if len(fields) >= 3 else '',
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
    atomic_json(state_path, {'id': backup_id, 'base': manifest['base'], 'to_lsn': cp['to_lsn']})
    return manifest


def prepare(config, backup_id, workdir):
    root = Path(config['path'])
    chain, seen = [], set()
    current = safe_id(backup_id)
    while current:
        if current in seen:
            raise ValueError('Cycle in backup chain')
        seen.add(current)
        folder, manifest = load_complete(root, current)
        verify(folder, manifest)
        chain.append((folder, manifest))
        current = manifest['parent']
    chain.reverse()
    base = chain[0][1]
    if base['kind'] != 'full' or base['from_lsn'] != 0 or base['base'] != base['id']:
        raise ValueError('Chain does not start with a full backup')
    for (_, previous), (_, following) in zip(chain, chain[1:]):
        if (following['kind'] != 'incremental' or following['from_lsn'] != previous['to_lsn']
                or following['source_uuid'] != base['source_uuid'] or following['base'] != base['id']):
            raise ValueError('Broken incremental chain')
    destination = Path(workdir)
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError('Restore workspace must be outside the backup store')
    if destination.exists():
        raise ValueError('Use a new, nonexistent restore workspace')
    destination.mkdir(mode=0o700, parents=True)
    prepared = destination / 'prepared'
    shutil.copytree(chain[0][0] / 'data', prepared)
    tool = config.get('xtrabackup', 'xtrabackup')
    with (destination / 'prepare.log').open('w') as log:
        run([tool, '--prepare', '--target-dir=' + str(prepared)] +
            (['--apply-log-only'] if len(chain) > 1 else []), log)
        for number, (folder, _) in enumerate(chain[1:], start=1):
            incremental = destination / ('incremental-' + str(number))
            shutil.copytree(folder / 'data', incremental)
            run([tool, '--prepare', '--target-dir=' + str(prepared), '--incremental-dir=' + str(incremental)] +
                (['--apply-log-only'] if number < len(chain) - 1 else []), log)
    return {'prepared': str(prepared), 'gtid': chain[-1][1]['gtid'],
            'source_uuid': base['source_uuid'], 'backup_id': backup_id}


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


def prune(config, retention_days):
    """Delete complete backups older than retention, keeping the newest chain.

    The newest complete backup and its whole ancestor chain are always kept,
    so pruning can never remove the only restorable chain. Suspect entries
    are reported and left alone. Stale partial directories from crashed
    runs are removed after 24 hours.
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
    pruned_binlogs = prune_binlogs(config, remaining)
    return {'pruned': pruned, 'protected': sorted(protected),
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
    backup.add_argument('kind', choices=['full', 'incremental'])
    restoration = sub.add_parser('prepare')
    restoration.add_argument('--backup-id', required=True)
    restoration.add_argument('--workdir', required=True)
    pruning = sub.add_parser('prune')
    pruning.add_argument('--retention-days', type=int, required=True)
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
            result = prune(config, args.retention_days)
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print('Backup workflow failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
