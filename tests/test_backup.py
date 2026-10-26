"""Failure-path tests with fake executables, not live database recovery tests."""
import fcntl
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
args=sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as f: f.write(json.dumps([Path(sys.argv[0]).name] + args)+'\\n')
name=Path(sys.argv[0]).name
if name == 'mysql':
 print(os.environ.get('FAKE_UUID','source-uuid')+'\\t8.4.8-8')
elif name == 'mc':
 if os.environ.get('FAKE_UPLOAD_FAIL') == '1':sys.exit(4)
elif '--backup' in args:
 if os.environ.get('FAKE_BACKUP_FAIL') == '1':sys.exit(3)
 target=Path(next(a.split('=',1)[1] for a in args if a.startswith('--target-dir=')))
 target.mkdir(parents=True)
 start=int(next((a.split('=',1)[1] for a in args if a.startswith('--incremental-lsn=')), '0'))
 if os.environ.get('FAKE_BAD_LSN') == '1':start+=1
 (target/'xtrabackup_checkpoints').write_text('from_lsn = %s\\nto_lsn = %s\\n'%(start,start+100))
 (target/'xtrabackup_binlog_info').write_text('mysql-bin.000001\\t123\\tuuid:1-10\\n')
 (target/'table.ibd').write_text('fixture data '+str(start))
elif '--prepare' in args:
 target=Path(next(a.split('=',1)[1] for a in args if a.startswith('--target-dir=')))
 (target/'prepared-fixture').write_text('prepared working copy')
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
        for name in ['mysql', 'xtrabackup', 'mc']:
            p = self.base / name; p.write_text(FAKE); p.chmod(0o700)
        self.config = {'path': str(self.root), 'storage': 'local', 'parallel': 2,
                       'client_file': str(self.base / 'private.cnf'),
                       'mysql': str(self.base / 'mysql'), 'xtrabackup': str(self.base / 'xtrabackup'),
                       'mc': str(self.base / 'mc'), 'mc_config_dir': str(self.base / 'mc-conf'),
                       'remote': 'backup/bucket/db1'}

    def calls_for(self, name):
        return [json.loads(s) for s in self.calls.read_text().splitlines()
                if json.loads(s)[0] == name]

    def test_full_incremental_state_and_checksums(self):
        full = workflow.take_backup(self.config, 'full')
        inc = workflow.take_backup(self.config, 'incremental')
        self.assertEqual(inc['parent'], full['id'])
        self.assertEqual(inc['from_lsn'], full['to_lsn'])
        folder, manifest = workflow.load_complete(self.root, inc['id'])
        workflow.verify(folder, manifest)
        self.assertEqual(json.loads((self.root/'state.json').read_text())['to_lsn'], 200)

    def test_first_incremental_requires_full(self):
        with self.assertRaises(FileNotFoundError): workflow.take_backup(self.config, 'incremental')
        self.assertFalse((self.root/'state.json').exists())

    def test_backup_failure_preserves_state(self):
        workflow.take_backup(self.config, 'full')
        previous = (self.root/'state.json').read_bytes()
        with patch.dict(os.environ, {'FAKE_BACKUP_FAIL':'1'}):
            with self.assertRaises(subprocess.CalledProcessError): workflow.take_backup(self.config,'incremental')
        self.assertEqual((self.root/'state.json').read_bytes(), previous)

    def test_upload_failure_preserves_state_and_has_no_success_marker(self):
        workflow.take_backup(self.config, 'full')
        previous = (self.root/'state.json').read_bytes()
        self.config['storage'] = 'minio'
        with patch.dict(os.environ, {'FAKE_UPLOAD_FAIL':'1'}):
            with self.assertRaises(subprocess.CalledProcessError): workflow.take_backup(self.config,'incremental')
        self.assertEqual((self.root/'state.json').read_bytes(), previous)
        partial = list(self.root.glob('.partial-*'))
        self.assertTrue(partial)
        self.assertFalse((partial[0]/'_SUCCESS.json').exists())

    def test_minio_publishes_marker_last_with_no_insecure_flag(self):
        self.config['storage'] = 'minio'
        workflow.take_backup(self.config, 'full')
        calls = self.calls_for('mc')
        self.assertIn('mirror', calls[0]); self.assertIn('cp', calls[-1])
        self.assertTrue(calls[-1][-1].endswith('/_SUCCESS.json'))
        self.assertNotIn('--insecure', str(calls))

    def test_changed_source_requires_new_full(self):
        workflow.take_backup(self.config, 'full')
        with patch.dict(os.environ, {'FAKE_UUID':'different-source'}):
            with self.assertRaisesRegex(ValueError, 'Source identity'): workflow.take_backup(self.config,'incremental')

    def test_mismatched_lsn_rejects_backup(self):
        workflow.take_backup(self.config, 'full')
        previous = (self.root/'state.json').read_bytes()
        with patch.dict(os.environ, {'FAKE_BAD_LSN':'1'}):
            with self.assertRaisesRegex(ValueError,'continuity'): workflow.take_backup(self.config,'incremental')
        self.assertEqual((self.root/'state.json').read_bytes(), previous)

    def test_prepare_order_keeps_originals_unchanged(self):
        full = workflow.take_backup(self.config,'full')
        workflow.take_backup(self.config,'incremental')
        last = workflow.take_backup(self.config,'incremental')
        original = (self.root/full['id']/'data/table.ibd').read_bytes()
        result = workflow.prepare(self.config,last['id'],str(self.base/'restore'))
        prepare_calls = [c for c in self.calls_for('xtrabackup') if '--prepare' in c]
        self.assertEqual(len(prepare_calls), 3)
        self.assertIn('--apply-log-only', prepare_calls[0]); self.assertIn('--apply-log-only', prepare_calls[1])
        self.assertNotIn('--apply-log-only', prepare_calls[2])
        self.assertEqual((self.root/full['id']/'data/table.ibd').read_bytes(), original)
        self.assertFalse((self.root/full['id']/'data/prepared-fixture').exists())
        self.assertTrue(Path(result['prepared']).exists())

    def test_corruption_rejected_before_prepare(self):
        full = workflow.take_backup(self.config,'full')
        (self.root/full['id']/'data/table.ibd').write_text('corrupted')
        with self.assertRaisesRegex(ValueError,'Checksum mismatch'):
            workflow.prepare(self.config,full['id'],str(self.base/'restore'))
        self.assertFalse(any('--prepare' in c for c in self.calls_for('xtrabackup')))

    def test_missing_parent_or_marker_rejected(self):
        full = workflow.take_backup(self.config,'full')
        inc = workflow.take_backup(self.config,'incremental')
        (self.root/full['id']/'_SUCCESS.json').unlink()
        with self.assertRaises(FileNotFoundError):workflow.prepare(self.config,inc['id'],str(self.base/'restore'))

    def test_manifest_tampering_rejected(self):
        full = workflow.take_backup(self.config,'full')
        p=self.root/full['id']/'manifest.json'
        data=json.loads(p.read_text());data['to_lsn']=999;p.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'manifest'):workflow.prepare(self.config,full['id'],str(self.base/'restore'))

    def test_existing_workspace_rejected(self):
        full=workflow.take_backup(self.config,'full')
        with self.assertRaisesRegex(ValueError,'nonexistent'):
            workflow.prepare(self.config,full['id'],str(self.base))

    def test_path_traversal_identifier_rejected(self):
        with self.assertRaises(ValueError):workflow.load_complete(self.root,'../other')

    def test_workspace_inside_backup_store_rejected(self):
        full=workflow.take_backup(self.config,'full')
        with self.assertRaisesRegex(ValueError,'outside'):
            workflow.prepare(self.config,full['id'],str(self.root/'restore'))

    def test_shared_lock_prevents_overlap(self):
        settings=self.base/'settings.json';settings.write_text(json.dumps(self.config))
        with (self.root/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=subprocess.run(['python3',str(SCRIPT),'--config',str(settings),'backup','full'],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'state.json').exists())


if __name__ == '__main__': unittest.main()
