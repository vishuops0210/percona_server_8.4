# MinIO setup and secrets for Percona backups

How to set up MinIO as the backup store for the Percona MySQL role, and
how to create the access key the backup script uses. MinIO here is the
shared server at `https://minio.ldc.opstree.dev:9000`, the same server
the Outline backups already use. In MinIO there are no real folders,
only buckets and prefixes inside them. The "folder for MySQL" is a
bucket called `percona-mysql-backup`, and the backups sit inside it under
their own prefixes.

## 1. Install mc, the MinIO client, on your laptop

Do NOT use `sudo apt install mc`. That installs Midnight Commander, a
different program. Do NOT use the `dl.min.io` download link either:
MinIO shut it off and it now returns HTTP 410 Gone. The binary comes
from the MinIO GitHub releases.

```bash
sudo rm -f /usr/local/bin/mc
curl -sSL -o /tmp/mc https://github.com/minio/mc/releases/download/RELEASE.2025-08-13T08-35-41Z/mc.linux-amd64.RELEASE.2025-08-13T08-35-41Z
chmod +x /tmp/mc
sudo mv /tmp/mc /usr/local/bin/mc
mc --version
```

Optional check that the download is intact, run it before the `mv`:

```bash
echo "01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891  /tmp/mc" | sha256sum -c -
```

It should print `/tmp/mc: OK`. `mc --version` should print
`RELEASE.2025-08-13T08-35-41Z`.

## 2. Log in with your admin id and password

```bash
mc alias set ldc https://minio.ldc.opstree.dev:9000
```

It prompts for the Access Key and the Secret Key. Type your admin id
and password there, so they never sit in your shell history. Verify
the login worked:

```bash
mc admin info ldc
mc ls ldc
```

The first shows the server info. The second lists the buckets. You
should see your existing buckets there, for example the Outline
backup bucket.

## 3. Create the bucket for MySQL

```bash
mc mb ldc/percona-mysql-backup
mc ls ldc
```

This bucket is the one the backup role will use. The role's settings
currently name the bucket `percona-backup`, so when MinIO is wired
into the role, the role is updated to `percona-mysql-backup` to match
this bucket, not the other way round.

## 4. Policy: the script key may touch only this bucket

The backup script gets its own key, never your admin key, and that key
is limited to the `percona-mysql-backup` bucket. It needs PutObject to upload
dumps and binlogs, GetObject to read them back for verify and restore,
DeleteObject so prune can remove old backups, and ListBucket to list
what is there. Write this file on your laptop:

```bash
cat > /tmp/percona-mysql-backup-rw.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::percona-mysql-backup"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::percona-mysql-backup/*"]
    }
  ]
}
EOF
```

## 5. Create the service account for the backup script

A service account is MinIO's name for a dedicated access key and
secret key. Create one for the root user `minioadmin`, with the policy
file from step 4 attached at creation. Generate the secret first and
keep it in a file with strict permissions, so you have it for the
vault step and it never appears in your shell history:

```bash
openssl rand -hex 20 > /tmp/percona-mysql-backup-secret.txt
chmod 600 /tmp/percona-mysql-backup-secret.txt
mc admin user svcacct add ldc minioadmin --access-key perconabackup --secret-key "$(cat /tmp/percona-mysql-backup-secret.txt)" --policy /tmp/percona-mysql-backup-rw.json --name percona-mysql-backup
```

The access key is `perconabackup`. The secret key is the contents of
`/tmp/percona-mysql-backup-secret.txt`. The command prints both back when it
succeeds. If you leave out `--secret-key`, MinIO generates one and
prints it once, but then you must copy it from the output, so
generating it first is the safer path.

## 6. Where the key goes

The access key and secret key go into the Ansible vault,
`inventories/group_vars/all/vault.yml`, when the backup role is wired
to MinIO. That is the step after this doc: the backup playbook puts
the key into mc on proxy1 (192.168.8.55) and switches the backup
store from local disk to MinIO. The key pair never goes in chat, never
in git, and never in a plain file on the repo. After the vault step,
delete the temporary files:

```bash
rm -f /tmp/percona-mysql-backup-secret.txt /tmp/percona-mysql-backup-rw.json
```

## 7. Verify the script key works

Set a temporary alias with the new key, list the bucket, then remove
the alias. This proves the key can reach the bucket and nothing else:

```bash
mc alias set percona-test https://minio.ldc.opstree.dev:9000 perconabackup "$(cat /tmp/percona-mysql-backup-secret.txt)"
mc ls percona-test/percona-mysql-backup
mc alias remove percona-test
```

Run step 7 before deleting the files in step 6. A test upload is also
worth doing once the bucket exists:

```bash
echo test > /tmp/minio-test.txt
mc cp /tmp/minio-test.txt percona-test/percona-mysql-backup/test.txt
mc rm percona-test/percona-mysql-backup/test.txt
rm -f /tmp/minio-test.txt
```

## Security notes

- The admin key is only for admin tasks: buckets, users, policies.
  The script key is bucket scoped and cannot see any other bucket.
- If a key ever leaks, delete the service account with
  `mc admin user svcacct rm ldc <access-key>` and create a fresh one,
  then update the vault.
- The MinIO root user here is `minioadmin`. If your admin id is a
  different user, use that name in step 5 instead of `minioadmin`.
