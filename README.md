# Verified Restore branch

This branch is the working branch for the Verified Restore product. It holds only what the product needs, so the product work stays separate from the full Percona lab role on main.

## What is in this branch

- `roles/percona_server/` : the Ansible role that installs and configures Percona Server 8.4. This is the engine the product runs on.
- `mock-website/` : the Verified Restore mock website, the :8080 site. React source in `src/`, built files in `dist/`. Run `npm install` then `npm run build` to rebuild, or serve `dist/` directly.
- `db-admin-app/` : the working database admin app, the :5000 site. Flask app in `app.py`, React UI built files in `static/`. Run with the systemd service or `python3 app.py`.

## What is not in this branch

Everything else from main is deliberately left out: the Group Replication role, ProxySQL, backup role, monitoring, playbooks, inventories, docs, and tests. Those stay on main. If the product needs one of them later, bring it in from main with a clean cherry-pick or copy, do not merge main into this branch.
