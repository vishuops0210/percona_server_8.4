# Ansible Galaxy release: opstree.percona

Collection: opstree.percona, version 1.0.0, MIT license, dependencies
community.mysql 4.0.1 and community.proxysql 1.7.0. Galaxy metadata is in
galaxy.yml at the repo root.

## Pre-publish checklist

Before publishing, check these, in order:

- Build: ansible-galaxy collection build produces
  opstree-percona-1.0.0.tar.gz.
- Tarball: check it includes the code and docs you intend to ship, and
  excludes .git, dist, LOCAL-RUN.md, vault files and test inventories.
  No secrets in the tarball, ever.
- Install: installing the built tarball succeeds and ansible-galaxy
  collection list shows opstree.percona 1.0.0.
- Tests: run tests/check.py, which covers YAML, module arguments,
  playbook syntax, templates, backup tests and documentation links.

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
