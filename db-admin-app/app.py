#!/usr/bin/env python3
"""
db-admin: a small, real MySQL / ProxySQL admin web app.

What it does, in plain terms:
  1. Login page, so the public site is not open to everyone.
  2. Connect form: enter a MySQL or ProxySQL host, port, user and
     password. The app then talks to the database as THAT user, so it
     can only do what that user is allowed to do.
  3. Connections page: live SHOW PROCESSLIST, grouped by user, so you
     can actually see which users are connected, from where, to which
     database, and what they are doing.
  4. Explorer page: pick a database, list its tables, view rows,
     insert a row, update a row, delete a row, create a table and
     drop a table, all as the connected user.
  5. Failover Test page: continuous writes, one per interval, with a
     live log. When the primary goes down, the log shows ERROR lines,
     then a RECOVERED line with how many seconds the outage lasted
     and which server is the new writer. That is how you measure
     failover time, instead of guessing it.

Security notes, read before hosting this on app.tyagi.fun:
  - Set ADMIN_USER, ADMIN_PASSWORD and SECRET_KEY in .env. Never
    leave the default admin password on a public domain.
  - DB passwords are kept only in server memory, keyed by a random
    session id in a cookie. They are never written to a file, never
    logged, and never returned by any API endpoint.
  - The app itself has no DB privilege. If you connect as testuser2,
    you can only touch databases testuser2 has grants on. If you
    connect as root, you can touch everything, so do not hand the
    admin login to anyone you would not hand root to.
  - To see ALL users on the Connections page, the connected DB user
    needs PROCESS privilege. Without it MySQL only shows that user's
    own connections, which is normal MySQL behaviour, not a bug.
  - The SQL box only runs SELECT / SHOW / DESCRIBE / EXPLAIN.
    Writes go through the Explorer forms, so a typo in the SQL box
    cannot drop a table.

Run locally:  pip install -r requirements.txt && python3 app.py
Run in Docker: see docker-compose.yml and README.md
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import pymysql
from flask import Flask, jsonify, request, make_response, send_from_directory, abort

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# session_id -> {"admin": True} or {"db": {host, port, user, password}}
SESSIONS = {}
FAILOVER_LOCK = threading.Lock()
FAILOVER_THREADS = {}


def _failover_state(sess):
    if "failover" not in sess:
        sess["failover"] = {
            "running": False, "seq": 0, "count": 0, "errors": 0,
            "down_since": None, "downtime_last": None, "last_writer": "",
            "last_message": "Not started yet.", "log": [],
        }
    return sess["failover"]


def _failover_log(state, msg):
    line = f"{datetime.datetime.now().isoformat()} {msg}"
    state["log"].append(line)
    del state["log"][:-200]
    state["last_message"] = msg


def _failover_worker(sid, creds, cfg):
    state = None
    sess = SESSIONS.get(sid)
    if not sess:
        return
    state = _failover_state(sess)
    seq = 0
    errors = 0
    down_since = None
    conn = None
    while True:
        with FAILOVER_LOCK:
            if not state["running"]:
                break
        try:
            if conn is None:
                conn = _connect(creds)
                if down_since is not None:
                    downtime = time.time() - down_since
                    with FAILOVER_LOCK:
                        state["downtime_last"] = round(downtime, 1)
                    down_since = None
            seq += 1
            t0 = time.time()
            if cfg["custom_sql"]:
                sql = cfg["custom_sql"].replace("{seq}", str(seq)).replace("{app}", cfg["app_name"])
                with conn.cursor() as cur:
                    cur.execute(sql)
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO `{cfg['database']}`.`{cfg['table']}` (app_name, seq, payload) VALUES (%s, %s, %s)",
                        (cfg["app_name"], seq, "failover-test"),
                    )
            with conn.cursor() as cur:
                cur.execute("SELECT @@hostname AS h, @@server_uuid AS u, @@read_only AS ro")
                info = cur.fetchone()
            writer = info.get("h", "") if info else ""
            latency = round((time.time() - t0) * 1000)
            msg = f"OK seq={seq} writer={writer} read_only={info.get('ro', '') if info else ''} latency_ms={latency} errors={errors}"
            if down_since is not None:
                pass
            with FAILOVER_LOCK:
                state["seq"] = seq
                state["count"] = seq
                state["errors"] = errors
                state["last_writer"] = writer
                if state["downtime_last"] is not None and errors:
                    msg += f" RECOVERED after {state['downtime_last']}s"
            _failover_log(state, msg)
            time.sleep(cfg["interval"])
        except Exception as e:  # noqa: BLE001 - show any DB error in the live log
            errors += 1
            if down_since is None:
                down_since = time.time()
                with FAILOVER_LOCK:
                    state["down_since"] = down_since
            msg = f"ERROR seq={seq} errors={errors} err={type(e).__name__}: {str(e)[:200]}"
            with FAILOVER_LOCK:
                state["errors"] = errors
            _failover_log(state, msg)
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(2)
    try:
        if conn:
            conn.close()
    except Exception:
        pass
    _failover_log(state, "Stopped by user")

IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
ALLOWED_TYPES = {
    "VARCHAR(255)", "VARCHAR(64)", "VARCHAR(32)", "TEXT",
    "INT", "BIGINT", "DECIMAL(10,2)", "TIMESTAMP", "DATETIME", "DATE", "BOOLEAN",
}


def _session():
    sid = request.cookies.get("dbadmin_session")
    return sid, SESSIONS.get(sid) if sid else None


def _require_admin():
    sid, sess = _session()
    if not sess or not sess.get("admin"):
        return None
    return sess


def _require_db():
    sess = _require_admin()
    if not sess:
        return None, None
    db = sess.get("db")
    if not db:
        return None, None
    return sess, db


def _err(message, code=400):
    return jsonify({"ok": False, "error": str(message)[:500]}), code


def _connect(creds, database=None):
    return pymysql.connect(
        host=creds["host"], port=int(creds["port"]),
        user=creds["user"], password=creds["password"],
        database=database, autocommit=True,
        connect_timeout=5, read_timeout=10, write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _quote(name):
    if not IDENT_RE.match(name or ""):
        raise ValueError(f"Invalid identifier: {name}")
    return f"`{name}`"


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_spa(path):
    # Serve the React build from static/. API routes are registered
    # above and take precedence; anything else falls back to the SPA
    # index so a page refresh on any route still loads the app.
    if path.startswith("api/"):
        abort(404)
    if path:
        full = os.path.join(STATIC_DIR, path)
        if os.path.isfile(full):
            return send_from_directory(STATIC_DIR, path)
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/admin-login", methods=["POST"])
def admin_login():
    data = request.get_json(force=True, silent=True) or {}
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASSWORD:
        sid = str(uuid.uuid4())
        SESSIONS[sid] = {"admin": True}
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("dbadmin_session", sid, httponly=True, samesite="Lax")
        return resp
    return _err("Invalid admin login", 401)


@app.route("/api/admin-logout", methods=["POST"])
def admin_logout():
    sid, _ = _session()
    if sid:
        SESSIONS.pop(sid, None)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("dbadmin_session")
    return resp


@app.route("/api/status")
def status():
    sess = _require_admin()
    if not sess:
        return jsonify({"ok": True, "admin": False, "connected": False})
    db = sess.get("db")
    if not db:
        return jsonify({"ok": True, "admin": True, "connected": False})
    return jsonify({"ok": True, "admin": True, "connected": True,
                    "host": db["host"], "port": db["port"], "user": db["user"]})


@app.route("/api/connect", methods=["POST"])
def connect():
    sess = _require_admin()
    if not sess:
        return _err("Admin login required", 401)
    data = request.get_json(force=True, silent=True) or {}
    creds = {
        "host": data.get("host", "").strip(),
        "port": int(data.get("port") or 6033),
        "user": data.get("user", "").strip(),
        "password": data.get("password", ""),
    }
    if not creds["host"] or not creds["user"]:
        return _err("Host and user are required")
    try:
        conn = _connect(creds)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                dbs = [r["Database"] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        return _err(f"Connection failed: {e}")
    sess["db"] = creds
    return jsonify({"ok": True, "databases": dbs})


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    sess = _require_admin()
    if sess:
        sess.pop("db", None)
    return jsonify({"ok": True})


@app.route("/api/connections")
def connections():
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    try:
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO "
                    "FROM information_schema.PROCESSLIST ORDER BY TIME DESC"
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        return _err(f"Could not read processlist: {e}")
    by_user = {}
    for r in rows:
        by_user[r.get("USER") or "(none)"] = by_user.get(r.get("USER") or "(none)", 0) + 1
    return jsonify({"ok": True, "total": len(rows), "by_user": by_user, "rows": rows})


@app.route("/api/databases")
def databases():
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    try:
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                dbs = [r["Database"] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True, "databases": dbs})


@app.route("/api/tables")
def tables():
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    name = request.args.get("db", "")
    try:
        qdb = _quote(name)
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW TABLES FROM {qdb}")
                rows = cur.fetchall()
                table_list = [list(r.values())[0] for r in rows]
        finally:
            conn.close()
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True, "tables": table_list})


@app.route("/api/table", methods=["GET", "POST", "DELETE"])
def table():
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    if request.method == "GET":
        dbname, tname = request.args.get("db", ""), request.args.get("table", "")
        try:
            qdb, qt = _quote(dbname), _quote(tname)
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SHOW COLUMNS FROM {qdb}.{qt}")
                    cols = cur.fetchall()
                    cur.execute(f"SELECT COUNT(*) AS c FROM {qdb}.{qt}")
                    total = cur.fetchone()["c"]
                    cur.execute(f"SELECT * FROM {qdb}.{qt} LIMIT 100")
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception as e:
            return _err(str(e))
        for r in rows:
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = str(v)
        return jsonify({"ok": True, "columns": cols, "rows": rows, "total": total})
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        try:
            qdb, qt = _quote(data.get("db", "")), _quote(data.get("name", ""))
            cols = data.get("columns") or []
            if not cols:
                return _err("At least one column is required")
            parts, pk_cols, ai_cols = [], [], []
            for c in cols:
                if not IDENT_RE.match(c.get("name", "")):
                    return _err(f"Invalid column name: {c.get('name')}")
                ctype = c.get("type", "VARCHAR(255)")
                if ctype not in ALLOWED_TYPES:
                    return _err(f"Type not allowed: {ctype}")
                is_pk = bool(c.get("primary_key"))
                is_ai = bool(c.get("auto_increment"))
                # A PRIMARY KEY column must be NOT NULL (MySQL 1171 otherwise),
                # and AUTO_INCREMENT must be NOT NULL and a key (MySQL 1075 otherwise).
                not_null = (not c.get("nullable", True)) or is_pk or is_ai
                part = f"`{c['name']}` {ctype}"
                if is_ai:
                    part += " AUTO_INCREMENT"
                part += " NOT NULL" if not_null else " NULL"
                parts.append(part)
                if is_pk:
                    pk_cols.append(f"`{c['name']}`")
                if is_ai:
                    ai_cols.append(f"`{c['name']}`")
            # AUTO_INCREMENT columns must be first in the key; merge AI first, deduped.
            pks = []
            for n in ai_cols + pk_cols:
                if n not in pks:
                    pks.append(n)
            if pks:
                parts.append(f"PRIMARY KEY ({', '.join(pks)})")
            sql = f"CREATE TABLE {qdb}.{qt} ({', '.join(parts)})"
            conn = _connect(db)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
            finally:
                conn.close()
        except Exception as e:
            return _err(str(e))
        return jsonify({"ok": True, "message": f"Table {data.get('name')} created"})
    # DELETE
    dbname, tname = request.args.get("db", ""), request.args.get("table", "")
    try:
        qdb, qt = _quote(dbname), _quote(tname)
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE {qdb}.{qt}")
        finally:
            conn.close()
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True, "message": f"Table {tname} dropped"})


@app.route("/api/row", methods=["POST", "PUT", "DELETE"])
def row():
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    try:
        if request.method == "DELETE":
            dbname, tname = request.args.get("db", ""), request.args.get("table", "")
            pk = request.get_json(force=True, silent=True) or {}
            qdb, qt = _quote(dbname), _quote(tname)
            where = " AND ".join(f"{_quote(k)}=%s" for k in pk)
            params = list(pk.values())
            sql = f"DELETE FROM {qdb}.{qt} WHERE {where} LIMIT 1"
        else:
            data = request.get_json(force=True, silent=True) or {}
            dbname, tname = data.get("db", ""), data.get("table", "")
            qdb, qt = _quote(dbname), _quote(tname)
            values = data.get("values") or {}
            if request.method == "POST":
                cols = ", ".join(_quote(k) for k in values)
                ph = ", ".join(["%s"] * len(values))
                sql = f"INSERT INTO {qdb}.{qt} ({cols}) VALUES ({ph})"
                params = [v if v != "" else None for v in values.values()]
            else:
                pk = data.get("pk") or {}
                sets = ", ".join(f"{_quote(k)}=%s" for k in values)
                where = " AND ".join(f"{_quote(k)}=%s" for k in pk)
                sql = f"UPDATE {qdb}.{qt} SET {sets} WHERE {where} LIMIT 1"
                params = [v if v != "" else None for v in values.values()] + list(pk.values())
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
        finally:
            conn.close()
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True})


@app.route("/api/sql", methods=["POST"])
def run_sql():
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    data = request.get_json(force=True, silent=True) or {}
    sql = (data.get("sql") or "").strip()
    first = sql.split(None, 1)[0].upper() if sql else ""
    if first not in ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN"):
        return _err("SQL box allows SELECT, SHOW, DESCRIBE and EXPLAIN only. Use Explorer for writes.")
    try:
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() if cur.description else []
        finally:
            conn.close()
    except Exception as e:
        return _err(str(e))
    for r in rows:
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                r[k] = str(v)
    return jsonify({"ok": True, "rows": rows[:100]})


@app.route("/api/failover/create-table", methods=["POST"])
def failover_create_table():
    sess, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    data = request.get_json(force=True, silent=True) or {}
    try:
        qdb, qt = _quote(data.get("db", "")), _quote(data.get("table", ""))
        conn = _connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {qdb}.{qt} "
                    "(id BIGINT AUTO_INCREMENT PRIMARY KEY, app_name VARCHAR(32), "
                    "seq BIGINT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, payload VARCHAR(64))"
                )
        finally:
            conn.close()
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True, "message": f"Table {data.get('table')} ready for failover test"})


@app.route("/api/failover/start", methods=["POST"])
def failover_start():
    sid, sess = _session()
    _, db = _require_db()
    if not db:
        return _err("Connect to a database first", 401)
    data = request.get_json(force=True, silent=True) or {}
    state = _failover_state(sess)
    with FAILOVER_LOCK:
        if state["running"]:
            return jsonify({"ok": False, "error": "Failover test already running, press Stop first."})
        state.update(running=True, seq=0, count=0, errors=0, down_since=None,
                     downtime_last=None, last_writer="", last_message="Starting...", log=[])
    cfg = {
        "database": data.get("database", "testdb"),
        "table": data.get("table", "failover_test"),
        "app_name": data.get("app_name", "failover1"),
        "interval": float(data.get("interval") or 1.0),
        "custom_sql": (data.get("custom_sql") or "").strip(),
    }
    try:
        _quote(cfg["database"])
        _quote(cfg["table"])
    except Exception as e:
        state["running"] = False
        return _err(str(e))
    t = threading.Thread(target=_failover_worker, args=(sid, dict(db), cfg), daemon=True)
    FAILOVER_THREADS[sid] = t
    t.start()
    _failover_log(state, f"Started, db={cfg['database']} table={cfg['table']} interval={cfg['interval']}s")
    return jsonify({"ok": True, "message": "Failover test started. Watch the log below."})


@app.route("/api/failover/stop", methods=["POST"])
def failover_stop():
    _, sess = _session()
    if not sess:
        return _err("Admin login required", 401)
    state = _failover_state(sess)
    with FAILOVER_LOCK:
        state["running"] = False
    return jsonify({"ok": True, "message": "Stopping, the worker will exit on its next loop."})


@app.route("/api/failover/status")
def failover_status():
    _, sess = _session()
    if not sess:
        return _err("Admin login required", 401)
    state = _failover_state(sess)
    with FAILOVER_LOCK:
        out = dict(state)
    current_down = None
    if out["running"] and out["down_since"]:
        current_down = round(time.time() - out["down_since"], 1)
    out["current_downtime"] = current_down
    return jsonify({"ok": True, **out})


# ---------------------------------------------------------------------------
# Restore page: pick a backup and a binlog, restore them onto a target server.
# The app runs on the ProxySQL VM, so it reuses the backup setup that is
# already there: mc at /usr/local/bin/mc with config in
# /etc/percona-backup/mc (alias "backup"), and the local backup copies in
# /var/backup/mysql and /var/backup/binlog. No MinIO credentials are asked
# from the user. The target server is reached over SSH with the credentials
# the user types on the Restore page. SSH key and sudo password live only
# in server memory for the session, never in a file, except a 0600 temp
# key file that is deleted after each SSH call.
# ---------------------------------------------------------------------------
MC_BIN = "/usr/local/bin/mc"
MC_CONFIG_DIR = "/etc/percona-backup/mc"
MC_ALIAS = "backup"
MC_BUCKET = "percona-mysql-backup"
RESTORE_LOCK = threading.Lock()
RESTORE_JOBS = {}


def _restore_state(sess):
    if "restore" not in sess:
        sess["restore"] = {
            "running": False, "done": False, "ok": False,
            "message": "Not started yet.", "log": [],
        }
    return sess["restore"]


def _restore_log(state, msg):
    line = f"{datetime.datetime.now().isoformat()} {msg}"
    state["log"].append(line)
    del state["log"][:-300]
    state["last_message"] = msg
    # Also append to a file on disk so the restore can be followed even
    # if the web log does not update. One file per restore run.
    log_file = state.get("log_file")
    if log_file:
        try:
            with open(log_file, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _run_local(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _mc_cmd(*args):
    # mc runs as root via sudo, so it can read and write its config dir
    # under /etc/percona-backup/mc. vishal has a passwordless sudo rule
    # for mc only, see /etc/sudoers.d/dbadmin-mc on the ProxySQL VM.
    return ["sudo", "-n", MC_BIN, "--config-dir", MC_CONFIG_DIR, *args]


def _mc_ls_detailed(prefix):
    """List objects under prefix, returning (name, mtime_utc) pairs.
    mc ls prints a timestamp like [2026-11-12 12:57:15 IST] at the start
    of each line. The machine running mc is in IST, so parse that as
    Asia/Kolkata and convert to UTC for comparison with manifest times.
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
    except Exception:
        ist = timezone.utc
    code, out, err = _run_local(
        _mc_cmd("ls", f"{MC_ALIAS}/{MC_BUCKET}/{prefix}"),
        timeout=60)
    if code != 0:
        raise RuntimeError(f"mc ls failed: {(err or out).strip()[:300]}")
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        mtime = None
        rest = line
        if line.startswith("["):
            end = line.find("]")
            if end > 0:
                ts_text = line[1:end].strip()
                rest = line[end + 1:].strip()
                # ts_text is like "2026-11-12 12:57:15 IST"
                parts = ts_text.split()
                if len(parts) >= 2:
                    try:
                        naive = datetime.strptime(
                            f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S")
                        mtime = naive.replace(tzinfo=ist).astimezone(timezone.utc)
                    except Exception:
                        mtime = None
        parts = rest.split()
        if parts:
            items.append((parts[-1].rstrip("/"), mtime))
    return items


def _parse_utc(ts_text):
    """Parse an ISO 8601 UTC timestamp from a manifest, or return None."""
    from datetime import datetime, timezone
    if not ts_text:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _mc_ls(prefix):
    code, out, err = _run_local(
        _mc_cmd("ls", f"{MC_ALIAS}/{MC_BUCKET}/{prefix}"),
        timeout=60)
    if code != 0:
        raise RuntimeError(f"mc ls failed: {(err or out).strip()[:300]}")
    names = []
    for line in out.splitlines():
        parts = line.split()
        if parts:
            names.append(parts[-1].rstrip("/"))
    return sorted(names)


def _mc_cat(path):
    code, out, err = _run_local(
        _mc_cmd("cat", f"{MC_ALIAS}/{MC_BUCKET}/{path}"),
        timeout=60)
    if code != 0:
        raise RuntimeError(f"mc cat failed: {(err or out).strip()[:300]}")
    return out


def _mc_get(remote_path, local_path):
    code, out, err = _run_local(
        _mc_cmd("cp", f"{MC_ALIAS}/{MC_BUCKET}/{remote_path}", local_path),
        timeout=300)
    if code != 0:
        raise RuntimeError(f"mc cp failed: {(err or out).strip()[:300]}")


def _ssh_base(target, key_path):
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if key_path:
        cmd += ["-i", key_path]
    cmd.append(f"{target['user']}@{target['host']}")
    return cmd


def _ssh_run(target, key_path, remote_cmd, timeout=120):
    """Run a command on the target over SSH. Returns (code, stdout, stderr)."""
    if target.get("password") and not key_path:
        full = ["sshpass", "-p", target["password"]] + _ssh_base(target, None) + [remote_cmd]
    else:
        full = _ssh_base(target, key_path) + [remote_cmd]
    p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _ssh_sudo(target, key_path, remote_cmd, timeout=300):
    """Run a command with sudo on the target.
    The whole command is wrapped in sudo sh -c so that every step in an
    && chain runs as root, not just the first one. Tries passwordless
    sudo first (standard on EC2 Ubuntu), then falls back to the password
    from the form when one was provided.
    """
    pw = target.get("password") or ""
    quoted = "'" + remote_cmd.replace("'", "'\\''") + "'"
    if pw:
        inner = (
            f"sudo -n sh -c {quoted} 2>/dev/null || "
            f"echo {json.dumps(pw)} | sudo -S sh -c {quoted}"
        )
    else:
        inner = f"sudo -n sh -c {quoted}"
    return _ssh_run(target, key_path, inner, timeout=timeout)


def _scp_to_target(target, key_path, local_path, remote_path, timeout=300):
    # local_path may be a single file or a list of files. A shell glob
    # like mysql-bin.* is not expanded when passed as one argument to
    # scp, so callers pass the explicit file list instead.
    if isinstance(local_path, str):
        local_paths = [local_path]
    else:
        local_paths = list(local_path)
    if target.get("password") and not key_path:
        base = ["sshpass", "-p", target["password"], "scp", "-o",
                "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
    else:
        base = ["scp", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10"]
        if key_path:
            base += ["-i", key_path]
    p = subprocess.run(base + local_paths + [f"{target['user']}@{target['host']}:{remote_path}"],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _restore_worker(sid, target, backup_id, target_binlog):
    sess = SESSIONS.get(sid)
    if not sess:
        return
    state = _restore_state(sess)
    key_path = None
    stage = tempfile.mkdtemp(prefix="dbadmin-restore-")
    try:
        log_file = os.path.join(
            tempfile.gettempdir(),
            f"dbadmin-restore-{sid[:8]}-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}.log")
        with RESTORE_LOCK:
            state.update(running=True, done=False, ok=False,
                         message="Starting restore...", log=[],
                         log_file=log_file)
        _restore_log(state, f"Target: {target['user']}@{target['host']}")
        _restore_log(state, f"Backup: {backup_id}, restore up to binlog: {target_binlog}")
        _restore_log(state, f"Log file on this server: {log_file}")
        # SSH key to a 0600 temp file when the user pasted one.
        if target.get("ssh_key"):
            fd, key_path = tempfile.mkstemp(prefix="dbadmin-key-")
            with os.fdopen(fd, "w") as f:
                f.write(target["ssh_key"])
            os.chmod(key_path, 0o600)
        # 1. Reach the target.
        code, out, err = _ssh_run(target, key_path, "echo ok", timeout=30)
        if code != 0 or "ok" not in out:
            # Key auth failed, fall back to password auth when a password
            # was provided. The Connect test uses the same path, so a
            # failure here after a successful Connect means the key file
            # written for the worker differs from the one used at Connect.
            if target.get("password") and key_path:
                _restore_log(state, "SSH with key failed, retrying with password...")
                code, out, err = _ssh_run(target, None, "echo ok", timeout=30)
            if code != 0 or "ok" not in out:
                raise RuntimeError(f"SSH to target failed: {(err or out).strip()[:300]}")
            if target.get("password") and key_path:
                _restore_log(state, "SSH with password OK, continuing with password auth.")
                key_path = None
        _restore_log(state, "SSH to target OK.")
        # 2. MySQL on the target, install it when missing.
        code, out, err = _ssh_run(target, key_path, "which mysqld mysql", timeout=30)
        if code != 0:
            _restore_log(state, "MySQL not found on target, installing Percona Server 8.4...")
            # A fresh target has no Percona repo, so set it up first.
            # The repo is ps-84-lts, not ps84. percona-release setup
            # ps84 fails with "Specified repository does not exist".
            code, out, err = _ssh_sudo(
                target, key_path,
                "DEBIAN_FRONTEND=noninteractive apt-get update -qq "
                "&& DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                "wget gnupg2 lsb-release curl "
                "&& wget -q https://repo.percona.com/apt/percona-release_latest.generic_all.deb "
                "-O /tmp/percona-release.deb "
                "&& DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/percona-release.deb "
                "&& percona-release setup -y ps-84-lts "
                "&& DEBIAN_FRONTEND=noninteractive apt-get update -qq "
                "&& DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                "percona-server-server",
                timeout=900)
            if code != 0:
                raise RuntimeError(f"MySQL install failed: {(err or out).strip()[:300]}")
            _restore_log(state, "MySQL installed on target.")
        code, out, err = _ssh_sudo(target, key_path,
                                   "systemctl is-active mysql || systemctl start mysql",
                                   timeout=60)
        _restore_log(state, "MySQL running on target.")
        # 3. Fetch the backup manifest for its GTID, then the dump.
        manifest = json.loads(_mc_cat(f"proxy1/{backup_id}/manifest.json"))
        gtid = manifest.get("gtid", "")
        _restore_log(state, f"Backup GTID in dump: {gtid}")
        dump_local = os.path.join(stage, "dump.sql.gz")
        _mc_get(f"proxy1/{backup_id}/dump.sql.gz", dump_local)
        _restore_log(state, f"Dump downloaded ({os.path.getsize(dump_local)} bytes).")
        code, out, err = _scp_to_target(target, key_path, dump_local, "/tmp/restore-dump.sql.gz")
        if code != 0:
            raise RuntimeError(f"Copy dump to target failed: {(err or out).strip()[:300]}")
        _restore_log(state, "Dump copied to target.")
        # 4. Load the dump on the target.
        code, out, err = _ssh_sudo(target, key_path,
                                   "gunzip -c /tmp/restore-dump.sql.gz | mysql",
                                   timeout=900)
        if code != 0:
            raise RuntimeError(f"Dump load failed: {(err or out).strip()[:300]}")
        _restore_log(state, "Full dump loaded on target.")
        # 5. Binlogs: download from MinIO, copy to target, replay up to the
        # selected binlog, skipping the GTIDs already in the dump.
        all_bins = _mc_ls("proxy1-binlog/")
        bins = [b for b in all_bins if b.startswith("mysql-bin.")]
        if target_binlog not in bins:
            raise RuntimeError(f"Selected binlog {target_binlog} not found in MinIO.")
        upto = bins.index(target_binlog) + 1
        replay = bins[:upto]
        _restore_log(state, f"Replaying {len(replay)} binlog files, up to {target_binlog}.")
        bin_dir = os.path.join(stage, "binlogs")
        os.makedirs(bin_dir, exist_ok=True)
        bin_files = []
        for name in replay:
            local = os.path.join(bin_dir, name)
            _mc_get(f"proxy1-binlog/{name}", local)
            bin_files.append(local)
        code, out, err = _scp_to_target(target, key_path, bin_files, "/tmp/")
        if code != 0:
            raise RuntimeError(f"Copy binlogs to target failed: {(err or out).strip()[:300]}")
        exclude = f"--exclude-gtids='{gtid}'" if gtid else ""
        code, out, err = _ssh_sudo(
            target, key_path,
            f"mysqlbinlog --skip-gtids {exclude} /tmp/mysql-bin.000* | mysql",
            timeout=900)
        if code != 0:
            raise RuntimeError(f"Binlog replay failed: {(err or out).strip()[:300]}")
        _restore_log(state, "Binlog replay done.")
        # 6. Verify on the target.
        code, out, err = _ssh_sudo(target, key_path,
                                   "mysql -N -e \"SHOW DATABASES;\"",
                                   timeout=60)
        dbs = [l.strip() for l in out.splitlines() if l.strip()]
        _restore_log(state, f"Databases on target: {', '.join(dbs)}")
        with RESTORE_LOCK:
            state.update(running=False, done=True, ok=True,
                         message=f"Restore done. Databases on target: {', '.join(dbs)}")
    except Exception as e:
        _restore_log(state, f"FAILED: {e}")
        with RESTORE_LOCK:
            state.update(running=False, done=True, ok=False, message=f"Restore failed: {e}")
    finally:
        try:
            if key_path and os.path.exists(key_path):
                os.remove(key_path)
        except Exception:
            pass
        shutil.rmtree(stage, ignore_errors=True)


@app.route("/api/restore/connect", methods=["POST"])
def restore_connect():
    sess = _require_admin()
    if not sess:
        return _err("Admin login required", 401)
    data = request.get_json(force=True, silent=True) or {}
    target = {
        "host": (data.get("host") or "").strip(),
        "user": (data.get("user") or "").strip(),
        "password": data.get("password") or "",
        "ssh_key": (data.get("ssh_key") or "").strip(),
    }
    if not target["host"] or not target["user"]:
        return _err("Target host and SSH user are required")
    if not target["password"] and not target["ssh_key"]:
        return _err("Give either the user password or an SSH key")
    key_path = None
    try:
        if target["ssh_key"]:
            fd, key_path = tempfile.mkstemp(prefix="dbadmin-key-")
            with os.fdopen(fd, "w") as f:
                f.write(target["ssh_key"])
            os.chmod(key_path, 0o600)
        code, out, err = _ssh_run(target, key_path, "echo ok", timeout=30)
        if code != 0 or "ok" not in out:
            return _err(f"SSH to target failed: {(err or out).strip()[:300]}")
    except FileNotFoundError as e:
        return _err(f"sshpass is not installed on this server, use an SSH key instead: {e}")
    except Exception as e:
        return _err(str(e))
    finally:
        try:
            if key_path and os.path.exists(key_path):
                os.remove(key_path)
        except Exception:
            pass
    sess["restore_target"] = target
    return jsonify({"ok": True, "message": f"Connected to {target['user']}@{target['host']}"})


@app.route("/api/restore/backups")
def restore_backups():
    sess = _require_admin()
    if not sess:
        return _err("Admin login required", 401)
    if not sess.get("restore_target"):
        return _err("Connect to a restore target first", 401)
    try:
        backup_ids = [b for b in _mc_ls("proxy1/") if b]
        backups = []
        for bid in backup_ids:
            try:
                manifest = json.loads(_mc_cat(f"proxy1/{bid}/manifest.json"))
            except Exception:
                manifest = {}
            backups.append({
                "id": bid,
                "created_utc": manifest.get("created_utc", ""),
                "gtid": manifest.get("gtid", ""),
                "source_host": manifest.get("source_host", ""),
                "source_role": manifest.get("source_role", ""),
                "binlogs": [],
            })
        # Binlogs are grouped under the backup they follow in time. A
        # binlog belongs to the most recent backup created before that
        # binlog was written. Binlogs written before the earliest backup
        # are already inside that backup's dump, so they are not shown
        # under any backup. This keeps each backup's list to just the
        # binlogs you would replay after loading that backup.
        # Each binlog is returned as {name, time} where time is the
        # MinIO modification time in IST, so the user can pick a binlog
        # by the time they want to restore up to, for example 4:00 pm.
        from datetime import timezone
        try:
            from zoneinfo import ZoneInfo
            ist = ZoneInfo("Asia/Kolkata")
        except Exception:
            ist = timezone.utc

        def _fmt_ist(dt):
            if dt is None:
                return ""
            try:
                return dt.astimezone(ist).strftime("%d %b %H:%M IST")
            except Exception:
                return ""

        bins_detailed = _mc_ls_detailed("proxy1-binlog/")
        bins = [(n, t) for (n, t) in bins_detailed if n.startswith("mysql-bin.")]
        bins.sort(key=lambda x: x[0])
        backups_sorted = sorted(
            backups, key=lambda b: _parse_utc(b.get("created_utc")) or _parse_utc("1970-01-01T00:00:00+00:00"))
        for name, mtime in bins:
            owner = None
            for b in backups_sorted:
                created = _parse_utc(b.get("created_utc"))
                if created is None or mtime is None:
                    continue
                if mtime >= created:
                    owner = b
                else:
                    break
            if owner is not None:
                owner["binlogs"].append({"name": name, "time": _fmt_ist(mtime)})
        # If a binlog had no usable timestamp, fall back to showing it
        # under the latest backup so it is not silently hidden.
        for name, mtime in bins:
            if mtime is None:
                if backups_sorted:
                    backups_sorted[-1]["binlogs"].append({"name": name, "time": ""})
        for b in backups:
            seen = set()
            uniq = []
            for item in sorted(b["binlogs"], key=lambda x: x["name"]):
                if item["name"] not in seen:
                    seen.add(item["name"])
                    uniq.append(item)
            b["binlogs"] = uniq
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True, "backups": backups})


@app.route("/api/restore/start", methods=["POST"])
def restore_start():
    sid, sess = _session()
    if not sess:
        return _err("Admin login required", 401)
    target = sess.get("restore_target")
    if not target:
        return _err("Connect to a restore target first", 401)
    data = request.get_json(force=True, silent=True) or {}
    backup_id = (data.get("backup_id") or "").strip()
    target_binlog = (data.get("binlog") or "").strip()
    if not backup_id or not target_binlog:
        return _err("Pick a backup and a binlog first")
    state = _restore_state(sess)
    with RESTORE_LOCK:
        if state["running"]:
            return _err("A restore is already running")
        state.update(running=True, done=False, ok=False,
                     message="Starting restore...", log=[])
    t = threading.Thread(target=_restore_worker,
                         args=(sid, dict(target), backup_id, target_binlog),
                         daemon=True)
    RESTORE_JOBS[sid] = t
    t.start()
    return jsonify({"ok": True, "message": "Restore started. Watch the log below."})


@app.route("/api/restore/status")
def restore_status():
    _, sess = _session()
    if not sess:
        return _err("Admin login required", 401)
    state = _restore_state(sess)
    with RESTORE_LOCK:
        out = dict(state)
    return jsonify({"ok": True, **out})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
