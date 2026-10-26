# Ansible Galaxy release: opstree.percona

Collection: opstree.percona, version 1.0.0, MIT license, dependencies
community.mysql 4.0.1 and community.proxysql 1.7.0. Galaxy metadata is in
galaxy.yml at the repo root.

## Status on 2026-10-26

- Build verified: ansible-galaxy collection build succeeds and produces
  opstree-percona-1.0.0.tar.gz.
- Tarball verified: includes the tested code, the new ProxySQL variables
  task, and the new docs (test report, day-2 configuration). It excludes
  .git, dist, LOCAL-RUN.md, vault files and test inventories. No secrets
  in the tarball.
- Install verified: installing the built tarball with --no-deps succeeds
  and ansible-galaxy collection list shows opstree.percona 1.0.0.
- Local test suite verified on the lab controller: YAML, module arguments,
  playbook syntax, templates, backup tests and documentation links all pass.
- Dependency fetch from galaxy.ansible.com failed on the assistant VM
  with a network read error. That is a VM network issue, not a tarball
  issue. Dependencies are already installed on the lab controller, and a
  normal Galaxy install resolves them automatically.

## Do not publish yet: two blockers

1. Namespace: the opstree namespace must be requested once on
   galaxy.ansible.com by an organization admin. Until that exists, Galaxy
   will not accept opstree.percona.
2. Source repo: galaxy.yml points to OT-OSM/percona_server, not the
   personal test repo. Publish from the OT-OSM branch after the tested
   code is merged there. Do not publish the personal repo as opstree.

## Publish steps, after both blockers are cleared

```bash
ansible-galaxy collection build
ansible-galaxy collection publish opstree-percona-1.0.0.tar.gz --api-key "$GALAXY_API_KEY"
ansible-galaxy collection install opstree.percona
```

The API key is a Galaxy secret. Never put it in chat, in the repo, or in
a script. Pass it as an environment variable only for the publish command.

## Version rule

Keep 1.0.0 for the first Galaxy release, since 1.0.0 has never been
published. Bump the version in galaxy.yml only after a release is live,
for the next change set.
