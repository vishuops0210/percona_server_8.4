"""Failure-path tests with fake executables, not live database recovery tests."""
import fcntl
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'roles/percona_backup/files/backup.py'
spec = importlib.util.spec_from_file_location('backup_workflow', SCRIPT)
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)

FAKE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as f:
    f.write(json.dumps([Path(sys.argv[0]).name] + args) + '\\n')
name = Path(sys.argv[0]).name
if name == 'mysql':
    if os.environ.get('FAKE_MYSQL_FAIL') == '1':
        sys.exit(1)
    host = args[args.index('-h') + 1]
    if os.environ.get('FAKE_FAIL_HOST') == host:
        sys.exit(1)
    query = args[-1]
    if 'MEMBER_HOST' in query:
        primary = os.environ.get('FAKE_PRIMARY', '192.168.8.56')
        if os.environ.get('FAKE_NO_SECONDARY') == '1':
            print(primary + '\tPRIMARY')
        else:
            for h in ['192.168.8.56', '192.168.8.57', '192.168.8.54']:
                print(h + '\t' + ('PRIMARY' if h == primary else 'SECONDARY'))
    else:
        print(os.environ.get('FAKE_UUID', 'source-uuid') + '\\t8.4.8-8\\t' + os.environ.get('FAKE_GTID', 'source-uuid:1-13'))
elif name == 'mysqldump':
    host = args[args.index('-h') + 1]
    if (os.environ.get('FAKE_DUMP_FAIL') == '1'
            or host in os.environ.get('FAKE_DUMP_FAIL_HOST', '').split(',')):
        print('mysqldump: Got error', file=sys.stderr)
        sys.exit(3)
    sys.stdout.write("-- MySQL dump\\n-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='mysql-bin.000001', SOURCE_LOG_POS=123;\\nCREATE DATABASE testdb;\\n")
elif name == 'mc':
    if os.environ.get('FAKE_UPLOAD_FAIL') == '1':
        sys.exit(4)
'''


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'backups'; self.root.mkdir()
        self.calls = self.base / 'calls.jsonl'
        self.env = patch.dict(os.environ, {'FAKE_CALLS': str(self.calls)})
        self.env.start(); self.addCleanup(self.env.stop)
        for name in ['mysql', 'mysqldump', 'mc']:
            p = self.base / name; p.write_text(FAKE); p.chmod(0o700)
        self.config = {'path': str(self.root), 'storage': 'local',
                       'client_file': str(self.base / 'private.cnf'),
                       'mysql': str(self.base / 'mysql'), 'mysqldump': str(self.base / 'mysqldump'),
                       'mc': str(self.base / 'mc'), 'mc_config_dir': str(self.base / 'mc-conf'),
                       'remote': 'backup/bucket/db1',
                       'db_hosts': ['192.168.8.56', '192.168.8.57', '192.168.8.54']}

    def calls_for(self, name):
        return [json.loads(s) for s in self.calls.read_text().splitlines()
                if json.loads(s)[0] == name]

    def test_full_backup_manifest_fields_and_verify(self):
        full = workflow.take_backup(self.config, 'full')
        self.assertEqual(full['kind'], 'full')
        self.assertIsNone(full['parent'])
        self.assertEqual(full['base'], full['id'])
        self.assertEqual(full['source_uuid'], 'source-uuid')
        self.assertEqual(full['source_host'], '192.168.8.57')
        self.assertEqual(full['source_role'], 'SECONDARY')
        self.assertEqual(full['server_version'], '8.4.8-8')
        self.assertEqual(full['gtid'], 'source-uuid:1-13')
        self.assertEqual(full['binlog_file'], 'mysql-bin.000001')
        self.assertEqual(full['binlog_pos'], 123)
        self.assertEqual(set(full['files']), {'dump.sql.gz'})
        folder, manifest = workflow.load_complete(self.root, full['id'])
        workflow.verify(folder, manifest)
        sql = gzip.open(folder / 'dump.sql.gz', 'rt').read()
        self.assertIn('CREATE DATABASE testdb', sql)

    def test_incremental_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Only full backups'):
            workflow.take_backup(self.config, 'incremental')

    def test_find_backup_sources_skips_dead_host(self):
        with patch.dict(os.environ, {'FAKE_FAIL_HOST': '192.168.8.56',
                                     'FAKE_PRIMARY': '192.168.8.57'}):
            self.assertEqual(workflow.find_backup_sources(self.config),
                             [('192.168.8.56', 'SECONDARY'),
                              ('192.168.8.54', 'SECONDARY'),
                              ('192.168.8.57', 'PRIMARY')])
        hosts = [c[c.index('-h') + 1] for c in self.calls_for('mysql')]
        self.assertEqual(hosts, ['192.168.8.56', '192.168.8.57'])

    def test_find_backup_sources_no_host_answers(self):
        with patch.dict(os.environ, {'FAKE_MYSQL_FAIL': '1'}):
            with self.assertRaisesRegex(ValueError, 'No database host answered'):
                workflow.find_backup_sources(self.config)

    def test_backup_prefers_secondary(self):
        full = workflow.take_backup(self.config, 'full')
        self.assertEqual(full['source_host'], '192.168.8.57')
        self.assertEqual(full['source_role'], 'SECONDARY')
        dump_hosts = [c[c.index('-h') + 1] for c in self.calls_for('mysqldump')]
        self.assertEqual(dump_hosts, ['192.168.8.57'])

    def test_backup_falls_back_to_primary_when_no_secondary(self):
        with patch.dict(os.environ, {'FAKE_NO_SECONDARY': '1'}):
            full = workflow.take_backup(self.config, 'full')
        self.assertEqual(full['source_host'], '192.168.8.56')
        self.assertEqual(full['source_role'], 'PRIMARY')

    def test_backup_skips_failed_secondary_for_next(self):
        with patch.dict(os.environ, {'FAKE_DUMP_FAIL_HOST': '192.168.8.57'}):
            full = workflow.take_backup(self.config, 'full')
        self.assertEqual(full['source_host'], '192.168.8.54')
        self.assertEqual(full['source_role'], 'SECONDARY')
        dump_hosts = [c[c.index('-h') + 1] for c in self.calls_for('mysqldump')]
        self.assertEqual(dump_hosts, ['192.168.8.57', '192.168.8.54'])

    def test_backup_falls_back_to_primary_when_secondaries_fail(self):
        with patch.dict(os.environ, {'FAKE_DUMP_FAIL_HOST': '192.168.8.57,192.168.8.54'}):
            full = workflow.take_backup(self.config, 'full')
        self.assertEqual(full['source_host'], '192.168.8.56')
        self.assertEqual(full['source_role'], 'PRIMARY')

    def test_dump_failure_leaves_no_complete_backup(self):
        with patch.dict(os.environ, {'FAKE_DUMP_FAIL': '1'}):
            with self.assertRaises(subprocess.CalledProcessError):
                workflow.take_backup(self.config, 'full')
        complete, _ = workflow.list_backups(self.root)
        self.assertEqual(complete, [])
        self.assertTrue(list(self.root.glob('.partial-*')))

    def test_upload_failure_leaves_no_success_marker(self):
        self.config['storage'] = 'minio'
        with patch.dict(os.environ, {'FAKE_UPLOAD_FAIL': '1'}):
            with self.assertRaises(subprocess.CalledProcessError):
                workflow.take_backup(self.config, 'full')
        complete, _ = workflow.list_backups(self.root)
        self.assertEqual(complete, [])
        partial = list(self.root.glob('.partial-*'))
        self.assertTrue(partial)
        self.assertFalse((partial[0] / '_SUCCESS.json').exists())

    def test_minio_publishes_marker_last_with_no_insecure_flag(self):
        self.config['storage'] = 'minio'
        workflow.take_backup(self.config, 'full')
        calls = self.calls_for('mc')
        self.assertIn('mirror', calls[0]); self.assertIn('cp', calls[-1])
        self.assertTrue(calls[-1][-1].endswith('/_SUCCESS.json'))
        self.assertNotIn('--insecure', str(calls))

    def test_prepare_extracts_dump_sql_and_keeps_original(self):
        full = workflow.take_backup(self.config, 'full')
        before = (self.root / full['id'] / 'dump.sql.gz').read_bytes()
        result = workflow.prepare(self.config, full['id'], str(self.base / 'restore'))
        prepared = Path(result['prepared'])
        self.assertTrue(prepared.exists())
        self.assertIn('CREATE DATABASE testdb', prepared.read_text())
        self.assertEqual(result['gtid'], 'source-uuid:1-13')
        self.assertEqual(result['backup_id'], full['id'])
        self.assertEqual((self.root / full['id'] / 'dump.sql.gz').read_bytes(), before)
        self.assertFalse((self.root / full['id'] / 'dump.sql').exists())

    def test_corruption_rejected_before_prepare(self):
        full = workflow.take_backup(self.config, 'full')
        (self.root / full['id'] / 'dump.sql.gz').write_text('corrupted')
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            workflow.prepare(self.config, full['id'], str(self.base / 'restore'))
        self.assertFalse((self.base / 'restore').exists())

    def test_missing_success_marker_rejected(self):
        full = workflow.take_backup(self.config, 'full')
        (self.root / full['id'] / '_SUCCESS.json').unlink()
        with self.assertRaises(FileNotFoundError):
            workflow.prepare(self.config, full['id'], str(self.base / 'restore'))

    def test_manifest_tampering_rejected(self):
        full = workflow.take_backup(self.config, 'full')
        p = self.root / full['id'] / 'manifest.json'
        data = json.loads(p.read_text()); data['gtid'] = 'tampered:1-1'; p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'manifest'):
            workflow.prepare(self.config, full['id'], str(self.base / 'restore'))

    def test_existing_workspace_rejected(self):
        full = workflow.take_backup(self.config, 'full')
        with self.assertRaisesRegex(ValueError, 'nonexistent'):
            workflow.prepare(self.config, full['id'], str(self.base))

    def test_path_traversal_identifier_rejected(self):
        with self.assertRaises(ValueError):
            workflow.load_complete(self.root, '../other')

    def test_workspace_inside_backup_store_rejected(self):
        full = workflow.take_backup(self.config, 'full')
        with self.assertRaisesRegex(ValueError, 'outside'):
            workflow.prepare(self.config, full['id'], str(self.root / 'restore'))

    def test_shared_lock_prevents_overlap(self):
        settings = self.base / 'settings.json'; settings.write_text(json.dumps(self.config))
        with (self.root / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(['python3', str(SCRIPT), '--config', str(settings), 'backup', 'full'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        complete, _ = workflow.list_backups(self.root)
        self.assertEqual(complete, [])

    def test_status_reports_latest(self):
        full = workflow.take_backup(self.config, 'full')
        result = workflow.status(self.config)
        self.assertEqual(result['complete_backups'], 1)
        self.assertEqual(result['suspect_backups'], [])
        self.assertEqual(result['latest']['id'], full['id'])
        self.assertEqual(result['latest']['kind'], 'full')
        self.assertLess(result['latest']['age_hours'], 1)


class GfsTests(BackupTests):
    # Inherit BackupTests.setUp so self.root and self.config exist.
    def fake_complete(self, days_ago):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        out = []
        for n in days_ago:
            created = now - timedelta(days=n)
            out.append({'id': 'full-%d' % n, 'kind': 'full', 'parent': None,
                        'created_utc': created.isoformat()})
        return out

    def test_gfs_keepers(self):
        # 400 days of daily fulls, newest first in time order is oldest first in list.
        complete = self.fake_complete(list(range(400, -1, -1)))
        weekly, monthly = workflow.gfs_keepers(complete, 7, 12)
        # One weekly keeper per week in the last 7 weeks, one monthly per month in 12 months.
        self.assertEqual(len(weekly), 7)
        self.assertEqual(len(monthly), 12)
        # The newest backup is both a weekly and a monthly keeper.
        self.assertIn('full-0', weekly)
        self.assertIn('full-0', monthly)

    def test_prune_keeps_gfs_and_deletes_others(self):
        import hashlib
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        kept = set()
        for n in [0, 1, 6, 8, 15, 30, 90, 200, 400]:
            created = now - timedelta(days=n)
            bid = 'full-%d' % n
            folder = self.root / bid
            folder.mkdir()
            manifest = {'id': bid, 'kind': 'full', 'parent': None,
                        'created_utc': created.isoformat(), 'files': {}}
            (folder / 'manifest.json').write_text(json.dumps(manifest))
            digest = hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
            (folder / '_SUCCESS.json').write_text(json.dumps({'manifest_sha256': digest}))
        result = workflow.prune(self.config, 7, 7, 12)
        remaining = {d.name for d in self.root.iterdir() if d.is_dir()}
        # 400 days old is outside 12 months, so it must be pruned.
        self.assertNotIn('full-400', remaining)
        # Daily keepers, plus one weekly and one monthly keeper, must survive.
        self.assertIn('full-0', remaining)
        self.assertIn('full-6', remaining)
        self.assertIn('full-200', remaining)
        self.assertIn('full-400', result['pruned'])


if __name__ == '__main__':
    unittest.main()
