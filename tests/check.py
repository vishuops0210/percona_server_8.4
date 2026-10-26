#!/usr/bin/env python3
"""Local syntax, module and rendering checks. Never connects to database VMs."""
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
ENV = dict(os.environ, ANSIBLE_LOCAL_TEMP='/tmp/percona-test-ansible',
           ANSIBLE_REMOTE_TEMP='/tmp/percona-test-remote',
           ANSIBLE_GALAXY_CACHE_DIR='/tmp/percona-test-galaxy',
           PYTHONDONTWRITEBYTECODE='1')


def run(argv):
    result = subprocess.run(argv, env=ENV, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(' '.join(argv) + '\n' + result.stdout + result.stderr)
    return result.stdout


def walk_tasks(value):
    if isinstance(value, list):
        for item in value:
            yield from walk_tasks(item)
    elif isinstance(value, dict):
        yield value
        for key in ('block', 'rescue', 'always', 'tasks', 'pre_tasks', 'post_tasks'):
            yield from walk_tasks(value.get(key, []))


def main():
    files = [*ROOT.glob('*.yml'), *ROOT.glob('roles/**/*.yml'),
             *ROOT.glob('playbooks/**/*.yml'), *ROOT.glob('inventories/**/*.yml')]
    modules = {}
    for path in files:
        if path.name == 'vault.yml':
            continue
        data = yaml.safe_load(path.read_text())
        for task in walk_tasks(data):
            for key, value in task.items():
                if key.startswith(('community.', 'ansible.builtin.')) and isinstance(value, dict):
                    if key != 'ansible.builtin.set_fact':
                        modules.setdefault(key, []).append((path, value))
    docs = json.loads(run(['ansible-doc', '--json', *modules]))
    for module, uses in modules.items():
        options = docs[module]['doc'].get('options', {})
        valid = set(options)
        for spec in options.values():
            valid.update(spec.get('aliases', []))
        for path, args in uses:
            assert not set(args) - valid, (path, module, set(args) - valid)
    print('YAML and installed module arguments: passed')
    plays = sorted(ROOT.glob('playbooks/**/*.yml'))
    for play in plays:
        run(['ansible-playbook', '-i', str(ROOT/'tests/inventory.yml'), str(play), '--syntax-check'])
    print(f'Playbook syntax: {len(plays)} passed')
    with tempfile.TemporaryDirectory(prefix='percona-render-') as temporary:
        folder = Path(temporary)
        inventory = {'all': {'children': {
            'gr_members': {'hosts': {f'db{i}': {'ansible_host': f'192.0.2.{10+i}', 'ansible_connection': 'local'} for i in range(1,4)}},
            'proxies': {'hosts': {'proxy1': {'ansible_host': '192.0.2.21'}, 'proxy2': {'ansible_host': '192.0.2.22'}}}
        }}}
        inv = folder/'hosts.yml'; inv.write_text(yaml.safe_dump(inventory))
        tasks = []
        for role in sorted(ROOT.glob('roles/*')):
            defaults = yaml.safe_load((role/'defaults/main.yml').read_text())
            assert '```mermaid' in (role/'README.md').read_text()
            for template in role.glob('templates/*.j2'):
                variables = dict(defaults, ansible_facts={'memtotal_mb':4096},
                                 percona_root_password='fixture-root', backup_db_password='fixture-backup',
                                 exporter_db_password='fixture-exporter', proxysql_admin_password='fixture-proxy')
                tasks.append({'name': 'Render '+role.name+'/'+template.name,
                              'ansible.builtin.template': {'src':str(template), 'dest':str(folder/(role.name+'-'+template.name[:-3]))},
                              'vars':variables})
        # GR runs in a separate play and must not depend on server-role defaults.
        gr_defaults = yaml.safe_load((ROOT/'roles/percona_group_replication/defaults/main.yml').read_text())
        member = yaml.safe_load((ROOT/'roles/percona_group_replication/tasks/member.yml').read_text())
        recovery = next(t for t in member if 'block' in t)['block']
        query = next(t for t in recovery if t['name'] == 'Save recovery channel credentials')['community.mysql.mysql_query']['query']
        for tls in (None, True):
            variables = dict(gr_defaults)
            if tls is not None:
                variables['percona_gr_ssl'] = tls
            tasks.extend([
                {'name': 'Render recovery query with only GR defaults',
                 'ansible.builtin.set_fact': {'recovery_query': query}, 'vars': variables},
                {'name': 'Recovery TLS follows the selected setting', 'ansible.builtin.assert':
                 {'that': "('SOURCE_SSL=1' in recovery_query) == " + ('true' if tls else 'false')}}
            ])
        # Proxy-facing accounts use the '%' host wildcard; no source-address detection.
        for filename in ('accounts.yml', 'users.yml'):
            account_tasks = yaml.safe_load((ROOT/'roles/proxysql/tasks'/filename).read_text())
            account = next(t['community.mysql.mysql_user'] for t in account_tasks if 'community.mysql.mysql_user' in t)
            assert account['host'] == '%', filename
        # Exercise the actual selection expression with a promoted db2, not the initial seed.
        selection = next(t for t in yaml.safe_load((ROOT/'roles/percona_access/tasks/discover.yml').read_text())
                         if t['name']=='Select writer inventory name')
        selection=dict(selection,vars={'percona_writer_results':{'results':[
            {'item':'db1','query_result':[[]]}, {'item':'db2','query_result':[[{'MEMBER_HOST':'192.0.2.12'}]]},
            {'item':'db3','query_result':[[]]}]}})
        tasks.extend([selection,{'name':'Writer follows election','ansible.builtin.assert':{'that':"percona_writers == ['db2']"}}])
        # Removing an account must not require its old password or grants.
        user_task = yaml.safe_load((ROOT/'roles/percona_access/tasks/users.yml').read_text())[0]
        args = user_task['community.mysql.mysql_user']
        tasks.extend([
            {'name': 'Render removed user arguments', 'ansible.builtin.set_fact':
             {'removed_user': {k: args[k] for k in ('name', 'host', 'state', 'plugin', 'plugin_auth_string', 'salt', 'priv')}},
             'vars': {'item': {'name': 'old_user', 'state': 'absent'}}},
            {'name': 'Removal needs only identity and state', 'ansible.builtin.assert': {'that': [
                "removed_user == {'name': 'old_user', 'host': '%', 'state': 'absent'}"]}}
        ])
        p=folder/'render.yml';p.write_text(yaml.safe_dump([{'hosts':'db1','gather_facts':False,'tasks':tasks}],sort_keys=False))
        run(['ansible-playbook','-i',str(inv),str(p)])
        group=(folder/'percona_server-mysqld.cnf').read_text()
        assert 'group-replication-group-seeds = 192.0.2.11:33061,192.0.2.12:33061,192.0.2.13:33061\n' in group
        assert 'group-replication-bootstrap-group = OFF' in group
        for p in folder.glob('*.json'):
            json.loads(p.read_text())
        print('All role templates and promoted-writer selection: passed')
    run(['python3','-m','unittest','discover','-s','tests','-p','test_*.py'])
    print('Existing backup tests: passed')
    for path in [ROOT/'README.md', *ROOT.glob('docs/*.md'), *ROOT.glob('roles/*/README.md')]:
        body=re.sub(r'```.*?```','',path.read_text(),flags=re.S)
        for link in re.findall(r'\[[^\]]+\]\(([^)]+)\)',body):
            if '://' not in link:
                assert (path.parent/link.split('#')[0]).exists(), (path,link)
    print('Documentation links: passed. No live deployment performed.')


if __name__ == '__main__':
    main()
