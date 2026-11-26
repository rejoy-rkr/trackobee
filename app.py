"""Trackobee - multi-tenant server: signup/login, roles, facilities, deploy tools, ingest API, live dashboard."""
import base64, csv, functools, hashlib, hmac, io, ipaddress, json, os, re, secrets, sqlite3, threading, time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, Response, abort, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))

def _load_env(path):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError: pass
_load_env(os.environ.get("TB_ENV", os.path.join(HERE, ".env")))

DATA = os.environ.get("TB_DATA", os.path.join(HERE, "data")); os.makedirs(DATA, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()      # Supabase/Postgres; empty => local SQLite file
PG = bool(DATABASE_URL)
if PG and ("YOUR_DB_PASSWORD" in DATABASE_URL or "PROJECTREF" in DATABASE_URL):
    raise SystemExit("DATABASE_URL in .env is still the placeholder. Paste your Supabase transaction-pooler connection string.")
PUBLIC_URL = os.environ.get("TB_PUBLIC_URL", "").rstrip("/")
TRUST_PROXY = os.environ.get("TB_TRUST_PROXY") == "1"
ALLOW_SIGNUP = os.environ.get("TB_ALLOW_SIGNUP", "1") == "1"       # workspace *requests*; owner must approve
RETENTION_DAYS = int(os.environ.get("TB_RETENTION_DAYS", "60"))
INSTALLERS = os.environ.get("TB_INSTALLERS", os.path.join(HERE, "installers"))
INTERVAL, IDLE_LIMIT, ONLINE_WINDOW, MAX_GAP = 15, 300, 50, 45
EVENT_TYPES = {"login", "lock", "unlock", "crm_login", "crm_logout"}
ROLES = ("admin", "sub_admin", "read_only")
BINARIES = {"windows": "trackobee-setup.exe", "mac": "trackobee-agent-macos", "linux": "trackobee-agent-linux"}

app = Flask(__name__)
_kf = os.path.join(DATA, "secret.key")
if not os.path.exists(_kf):
    with open(_kf, "w") as f: f.write(secrets.token_hex(32))
    os.chmod(_kf, 0o600)
app.secret_key = os.environ.get("TB_SECRET_KEY") or open(_kf).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("TB_SECURE_COOKIES") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

# ------------------------------------------------------------------ database (SQLite for dev, Postgres/Supabase for prod)
if PG:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    POOL = ConnectionPool(DATABASE_URL, min_size=1, max_size=int(os.environ.get("TB_DB_POOL", "10")), open=True,
                          kwargs={"row_factory": dict_row, "prepare_threshold": None})   # None: required by Supabase's transaction pooler
else:
    SQLITE_PATH = os.path.join(DATA, "trackobee.db")

class Res:
    def __init__(self, cur): self.cur = cur
    def fetchone(self): return self.cur.fetchone()
    def fetchall(self): return self.cur.fetchall()
    def __iter__(self): return iter(self.cur.fetchall())

class Conn:
    """Tiny DB wrapper: '?' placeholders, dict rows, commit on success / rollback on error."""
    def __enter__(self):
        if PG: self._cm = POOL.connection(); self.c = self._cm.__enter__()
        else:
            self.c = sqlite3.connect(SQLITE_PATH, timeout=15)
            self.c.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
            self.c.execute("PRAGMA journal_mode=WAL")
        return self
    def __exit__(self, et, ev, tb):
        if PG: return self._cm.__exit__(et, ev, tb)
        try: self.c.commit() if et is None else self.c.rollback()
        finally: self.c.close()
    def execute(self, sql, params=()):
        return Res(self.c.execute(sql.replace("?", "%s") if PG else sql, params))
    def insert(self, sql, params=()):
        if PG: return self.c.execute(sql.replace("?", "%s") + " RETURNING id", params).fetchone()["id"]
        return self.c.execute(sql, params).lastrowid

def db(): return Conn()

def init_db():
    pk = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY"
    big = "BIGINT" if PG else "INTEGER"
    stmts = [
     f"CREATE TABLE IF NOT EXISTS orgs(id {pk}, name TEXT, public_id TEXT UNIQUE, agent_token TEXT UNIQUE, crm_secret TEXT, allowed_origins TEXT DEFAULT '', active INTEGER DEFAULT 1, created {big})",
     f"CREATE TABLE IF NOT EXISTS users(id {pk}, org_id {big}, email TEXT UNIQUE, name TEXT, pw_hash TEXT, role TEXT, active INTEGER DEFAULT 1, created {big})",
     f"CREATE TABLE IF NOT EXISTS signup_requests(id {pk}, org TEXT, name TEXT, email TEXT UNIQUE, pw_hash TEXT, ip TEXT, created {big})",
     f"CREATE TABLE IF NOT EXISTS facilities(id {pk}, org_id {big}, name TEXT, cidr TEXT, UNIQUE(org_id, cidr))",
     f"CREATE TABLE IF NOT EXISTS hb(id {pk}, org_id {big}, ts {big}, host TEXT, ip TEXT, pub_ip TEXT, os_user TEXT, crm_user TEXT, facility TEXT, state TEXT, idle INTEGER)",
     f"CREATE TABLE IF NOT EXISTS ev(id {pk}, org_id {big}, ts {big}, host TEXT, pub_ip TEXT, os_user TEXT, crm_user TEXT, facility TEXT, type TEXT)",
     "CREATE INDEX IF NOT EXISTS hb_o ON hb(org_id, ts)", "CREATE INDEX IF NOT EXISTS hb_oh ON hb(org_id, host, id)",
     "CREATE INDEX IF NOT EXISTS ev_o ON ev(org_id, ts)"]
    with db() as c:
        for q in stmts: c.execute(q)
        if PG:   # Supabase exposes public tables through its REST API: RLS on + no policies => only this server (postgres role) can read them
            for t in ("orgs", "users", "signup_requests", "facilities", "hb", "ev"): c.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
init_db()

_CACHE = {}
def cached(key, ttl, fn):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time(): return hit[1]
    v = fn(); _CACHE[key] = (time.time() + ttl, v); return v
def uncache(): _CACHE.clear()

def purge_loop():
    while True:
        try:
            with db() as c:
                c.execute("DELETE FROM hb WHERE ts < ?", (int(time.time()) - RETENTION_DAYS * 86400,))
                c.execute("DELETE FROM ev WHERE ts < ?", (int(time.time()) - 365 * 86400,))
        except Exception: pass
        time.sleep(6 * 3600)
threading.Thread(target=purge_loop, daemon=True).start()

# ------------------------------------------------------------------ auth helpers
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FAILS = {}   # (ip,email) -> [timestamps]; basic login throttle

def me():
    uid = session.get("uid")
    if not uid: return None
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if u and u["role"] != "owner":
            o = c.execute("SELECT active FROM orgs WHERE id=?", (u["org_id"],)).fetchone()
            if not o or not o["active"]: return None        # suspended / deleted workspace
    return u

def need(*roles):
    def deco(fn):
        @functools.wraps(fn)
        def w(*a, **k):
            u = me()
            if not u: return jsonify(error="login required"), 401
            if roles and u["role"] not in roles: return jsonify(error="forbidden"), 403
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("X-Requested-With") != "tb":
                return jsonify(error="bad request"), 400          # CSRF guard
            request.user = u; return fn(*a, **k)
        return w
    return deco

ADMIN, MANAGE, VIEW, OWNER = ("admin",), ("admin", "sub_admin"), ROLES, ("owner",)

def ok_password(p): return isinstance(p, str) and len(p) >= 10

def client_ip():
    """Behind the Cloudflare tunnel (TB_TRUST_PROXY=1) the real client IP is in CF-Connecting-IP; the app only listens on 127.0.0.1."""
    if TRUST_PROXY:
        h = request.headers
        if h.get("CF-Connecting-IP"): return h["CF-Connecting-IP"].strip()
        if h.get("X-Forwarded-For"): return h["X-Forwarded-For"].split(",")[-1].strip()
    return request.remote_addr

def server_url(): return PUBLIC_URL or request.host_url.rstrip("/")

# ------------------------------------------------------------------ CRM-signed pairing token
# token = <org_public_id>.<base64url("user:expiry")>.<hmac_sha256(org_secret, "<public_id>.<payload>")>
def org_by_pid(pid):
    def q():
        with db() as c: return c.execute("SELECT * FROM orgs WHERE public_id=? AND active=1", (pid,)).fetchone()
    return cached(("pid", pid), 20, q)

def verify_token(tok):
    """-> (org_row, crm_user) or (None, None)"""
    try:
        pid, p, sig = (tok or "").split(".")
        org = org_by_pid(pid)
        if not org: return None, None
        good = hmac.new(org["crm_secret"].encode(), f"{pid}.{p}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good): return None, None
        user, exp = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode().rsplit(":", 1)
        return (org, user) if int(exp) > time.time() else (None, None)
    except Exception:
        return None, None

def facility_for(org_id, *ips):
    def q():
        with db() as c: return c.execute("SELECT name, cidr FROM facilities WHERE org_id=?", (org_id,)).fetchall()
    rows = cached(("fac", org_id), 20, q)
    for ip in ips:
        try: a = ipaddress.ip_address(ip)
        except Exception: continue
        best = None
        for r in rows:
            n = ipaddress.ip_network(r["cidr"], strict=False)
            if a.version == n.version and a in n and (best is None or n.prefixlen > best[0]): best = (n.prefixlen, r["name"])
        if best: return best[1]
    return None

# ------------------------------------------------------------------ pages
def page(name): return send_from_directory(os.path.join(HERE, "static"), name)
@app.get("/")
def home():
    u = me()
    return redirect("/login" if not u else "/owner" if u["role"] == "owner" else "/app")
@app.get("/login")
def login_page(): return page("auth.html")
@app.get("/app")
def app_page():
    u = me()
    return redirect("/login") if not u else redirect("/owner") if u["role"] == "owner" else page("app.html")
@app.get("/owner")
def owner_page():
    u = me()
    return page("owner.html") if u and u["role"] == "owner" else redirect("/login" if not u else "/app")
@app.get("/embed.js")
def embed(): return send_from_directory(os.path.join(HERE, "static"), "embed.js", mimetype="application/javascript")

# ------------------------------------------------------------------ account APIs
@app.get("/api/config")
def pub_config(): return jsonify(signup=ALLOW_SIGNUP)

@app.post("/api/auth/signup")
def signup():
    """Does NOT create a workspace: files a request that the platform owner must approve."""
    if not ALLOW_SIGNUP: return jsonify(error="workspace requests are closed"), 403
    d = request.get_json(force=True)
    org, name, email, pw = (d.get("org") or "").strip(), (d.get("name") or "").strip(), (d.get("email") or "").strip().lower(), d.get("password")
    if not (org and name and EMAIL.match(email)): return jsonify(error="organization, name and a valid email are required"), 400
    if not ok_password(pw): return jsonify(error="password must be at least 10 characters"), 400
    ip, now = client_ip(), time.time()
    k = ("signup", ip); FAILS[k] = [t for t in FAILS.get(k, []) if now - t < 3600]
    if len(FAILS[k]) >= 5: return jsonify(error="too many requests from this network, try later"), 429
    FAILS[k].append(now)
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone() or c.execute("SELECT 1 FROM signup_requests WHERE email=?", (email,)).fetchone():
            return jsonify(error="this email already has an account or a pending request"), 409
        c.execute("INSERT INTO signup_requests(org,name,email,pw_hash,ip,created) VALUES(?,?,?,?,?,?)",
                  (org[:80], name[:80], email, generate_password_hash(pw), ip, int(now)))
    return jsonify(ok=True, pending=True)

@app.post("/api/auth/login")
def login():
    d = request.get_json(force=True); email = (d.get("email") or "").strip().lower()
    k = (client_ip(), email); now = time.time()
    FAILS[k] = [t for t in FAILS.get(k, []) if now - t < 600]
    if len(FAILS[k]) >= 8: return jsonify(error="too many attempts, try again in 10 minutes"), 429
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=? AND active=1", (email,)).fetchone()
        pend = None if u else c.execute("SELECT pw_hash FROM signup_requests WHERE email=?", (email,)).fetchone()
        suspended = False
        if u and u["role"] != "owner":
            o = c.execute("SELECT active FROM orgs WHERE id=?", (u["org_id"],)).fetchone()
            suspended = not o or not o["active"]
    if pend and check_password_hash(pend["pw_hash"], d.get("password") or ""):
        return jsonify(error="Your workspace request is awaiting approval."), 403
    if not u or not check_password_hash(u["pw_hash"], d.get("password") or ""):
        FAILS[k].append(now); return jsonify(error="invalid email or password"), 401
    if suspended: return jsonify(error="This workspace is suspended. Contact the platform owner."), 403
    session.clear(); session.permanent = True; session["uid"] = u["id"]
    return jsonify(ok=True, redirect="/owner" if u["role"] == "owner" else "/app")

@app.post("/api/auth/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/me")
@need(*VIEW, *OWNER)
def whoami():
    u = request.user
    with db() as c: o = c.execute("SELECT name FROM orgs WHERE id=?", (u["org_id"],)).fetchone() if u["org_id"] else None
    return jsonify(id=u["id"], name=u["name"], email=u["email"], role=u["role"], org=o["name"] if o else "Platform owner")

@app.post("/api/me/password")
@need(*VIEW, *OWNER)
def change_pw():
    d = request.get_json(force=True)
    if not check_password_hash(request.user["pw_hash"], d.get("old") or ""): return jsonify(error="current password is wrong"), 400
    if not ok_password(d.get("new")): return jsonify(error="password must be at least 10 characters"), 400
    with db() as c: c.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(d["new"]), request.user["id"]))
    return jsonify(ok=True)

# ------------------------------------------------------------------ user management (admin)
def user_rows(org_id):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,email,name,role,active,created FROM users WHERE org_id=? ORDER BY created", (org_id,))]

def admins_left(c, org_id, excluding):
    return c.execute("SELECT COUNT(*) AS n FROM users WHERE org_id=? AND role='admin' AND active=1 AND id!=?", (org_id, excluding)).fetchone()["n"]

@app.get("/api/users")
@need(*ADMIN)
def users_list(): return jsonify(user_rows(request.user["org_id"]))

@app.post("/api/users")
@need(*ADMIN)
def users_add():
    d = request.get_json(force=True); email = (d.get("email") or "").strip().lower()
    if d.get("role") not in ROLES or not EMAIL.match(email) or not (d.get("name") or "").strip(): return jsonify(error="name, valid email and role required"), 400
    if not ok_password(d.get("password")): return jsonify(error="password must be at least 10 characters"), 400
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): return jsonify(error="email already registered"), 409
        c.execute("INSERT INTO users(org_id,email,name,pw_hash,role,created) VALUES(?,?,?,?,?,?)",
                  (request.user["org_id"], email, d["name"].strip()[:80], generate_password_hash(d["password"]), d["role"], int(time.time())))
    return jsonify(user_rows(request.user["org_id"]))

@app.patch("/api/users/<int:uid>")
@need(*ADMIN)
def users_edit(uid):
    d, o = request.get_json(force=True), request.user["org_id"]
    with db() as c:
        t = c.execute("SELECT * FROM users WHERE id=? AND org_id=?", (uid, o)).fetchone()
        if not t: abort(404)
        role, active = d.get("role", t["role"]), int(d.get("active", t["active"]))
        if role not in ROLES: return jsonify(error="bad role"), 400
        if t["role"] == "admin" and (role != "admin" or not active) and admins_left(c, o, uid) == 0:
            return jsonify(error="the organization needs at least one active admin"), 400
        if d.get("password"):
            if not ok_password(d["password"]): return jsonify(error="password must be at least 10 characters"), 400
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(d["password"]), uid))
        c.execute("UPDATE users SET role=?, active=? WHERE id=?", (role, active, uid))
    return jsonify(user_rows(o))

@app.delete("/api/users/<int:uid>")
@need(*ADMIN)
def users_del(uid):
    o = request.user["org_id"]
    with db() as c:
        t = c.execute("SELECT * FROM users WHERE id=? AND org_id=?", (uid, o)).fetchone()
        if not t: abort(404)
        if t["id"] == request.user["id"]: return jsonify(error="you cannot delete yourself"), 400
        if t["role"] == "admin" and admins_left(c, o, uid) == 0: return jsonify(error="the organization needs at least one active admin"), 400
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    return jsonify(user_rows(o))

# ------------------------------------------------------------------ org settings + facilities
def org_of(u):
    with db() as c: return c.execute("SELECT * FROM orgs WHERE id=?", (u["org_id"],)).fetchone()

@app.get("/api/org")
@need(*MANAGE)
def org_get():
    o, admin = org_of(request.user), request.user["role"] == "admin"
    return jsonify(name=o["name"], public_id=o["public_id"], allowed_origins=o["allowed_origins"],
                   agent_token=o["agent_token"] if admin else None, crm_secret=o["crm_secret"] if admin else None)

@app.post("/api/org")
@need(*MANAGE)
def org_set():
    d = request.get_json(force=True); origins = []
    for x in (d.get("allowed_origins") or "").replace("\n", ",").split(","):
        x = x.strip().rstrip("/")
        if x:
            if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$", x): return jsonify(error=f"invalid origin: {x} (use https://host[:port])"), 400
            origins.append(x)
    with db() as c:
        c.execute("UPDATE orgs SET allowed_origins=? WHERE id=?", (",".join(origins), request.user["org_id"])); uncache()
        if request.user["role"] == "admin" and (d.get("name") or "").strip():
            c.execute("UPDATE orgs SET name=? WHERE id=?", (d["name"].strip()[:80], request.user["org_id"]))
    return jsonify(ok=True)

@app.post("/api/org/rotate/<what>")
@need(*ADMIN)
def org_rotate(what):
    col = {"agent": "agent_token", "crm": "crm_secret"}.get(what) or abort(404)
    with db() as c: c.execute(f"UPDATE orgs SET {col}=? WHERE id=?", (secrets.token_urlsafe(32), request.user["org_id"]))
    uncache(); return jsonify(ok=True)

@app.route("/api/facilities", methods=["GET", "POST", "DELETE"])
@need(*VIEW)
def facilities():
    o, role = request.user["org_id"], request.user["role"]
    with db() as c:
        if request.method != "GET":
            if role not in MANAGE: return jsonify(error="forbidden"), 403
            if request.method == "POST":
                d = request.get_json(force=True)
                try: net = str(ipaddress.ip_network(d["cidr"].strip(), strict=False))
                except Exception: return jsonify(error="invalid IP or CIDR"), 400
                if not (d.get("name") or "").strip(): return jsonify(error="name required"), 400
                c.execute("INSERT INTO facilities(org_id,name,cidr) VALUES(?,?,?) ON CONFLICT(org_id,cidr) DO UPDATE SET name=excluded.name", (o, d["name"].strip()[:80], net)); uncache()
            else:
                c.execute("DELETE FROM facilities WHERE id=? AND org_id=?", (request.args.get("id", type=int), o)); uncache()
        return jsonify([dict(r) for r in c.execute("SELECT id,name,cidr FROM facilities WHERE org_id=? ORDER BY name", (o,))])

# ------------------------------------------------------------------ deploy: embed snippet + endpoint installers
def make_token(org, user, ttl=86400):
    p = base64.urlsafe_b64encode(f"{user}:{int(time.time()) + ttl}".encode()).decode().rstrip("=")
    return f"{org['public_id']}.{p}." + hmac.new(org["crm_secret"].encode(), f"{org['public_id']}.{p}".encode(), hashlib.sha256).hexdigest()

def have(osname): return os.path.isfile(os.path.join(INSTALLERS, BINARIES[osname]))

@app.get("/api/deploy/info")
@need(*MANAGE)
def deploy_info():
    o, admin = org_of(request.user), request.user["role"] == "admin"
    return jsonify(server=server_url(), public_id=o["public_id"], crm_secret=o["crm_secret"] if admin else None,
                   origins=o["allowed_origins"], packaged={k: have(k) for k in BINARIES})

@app.get("/api/deploy/installer/<osname>")
@need(*MANAGE)
def deploy_installer(osname):
    if osname not in BINARIES: abort(404)
    name, body = installer(org_of(request.user), osname)
    return Response(body, mimetype="application/octet-stream", headers={"Content-Disposition": f"attachment; filename={name}"})

@app.get("/download/<osname>")        # used by the embedded banner; needs a valid CRM-signed token
def download(osname):
    org, user = verify_token(request.args.get("token"))
    if osname not in BINARIES or not org: abort(403)
    name, body = installer(org, osname)
    return Response(body, mimetype="application/octet-stream", headers={"Content-Disposition": f"attachment; filename={name}"})

@app.get("/agent/trackobee_agent.py")
def agent_py(): return send_from_directory(os.path.join(HERE, "agent"), "trackobee_agent.py", mimetype="text/plain")

@app.get("/agent/bin/<osname>")       # generic (org-less) packaged agent binaries, if built
def agent_bin(osname):
    if osname not in BINARIES or not have(osname): abort(404)
    return send_from_directory(INSTALLERS, BINARIES[osname], as_attachment=True)

def installer(org, osname):
    """Per-organization endpoint installer script. Writes org.json (server, token, allowed origins) then installs the agent."""
    srv = server_url()
    cfg = json.dumps({"server": srv, "agent_token": org["agent_token"], "allowed_origins": org["allowed_origins"]}, separators=(",", ":"))
    if osname == "windows":
        if have("windows"):
            fetch = f"""powershell -NoProfile -Command "Invoke-WebRequest '{srv}/agent/bin/windows' -OutFile '%TEMP%\\trackobee-setup.exe'" || (echo Download failed & pause & exit /b 1)
"%TEMP%\\trackobee-setup.exe"
"""
        else:
            fetch = f"""where python >nul 2>&1 || (echo Python 3 is required: https://python.org & pause & exit /b 1)
set A=%LOCALAPPDATA%\\Trackobee
mkdir "%A%" 2>nul
powershell -NoProfile -Command "Invoke-WebRequest '{srv}/agent/trackobee_agent.py' -OutFile '%A%\\agent.py'"
> "%A%\\run.bat" echo @echo off
>> "%A%\\run.bat" echo start "" pythonw "%A%\\agent.py"
schtasks /Create /TN Trackobee /TR "\\"%A%\\run.bat\\"" /SC ONLOGON /F >nul
call "%A%\\run.bat"
echo Trackobee installed. Refresh the website.
pause
"""
        return "trackobee-setup.bat", f"""@echo off
setlocal
set D=%USERPROFILE%\\.trackobee
mkdir "%D%" 2>nul
> "%D%\\org.json" echo {cfg}
{fetch}"""
    pre = f"""#!/bin/sh
set -e
D="$HOME/.trackobee"; mkdir -p "$D"; umask 077
cat > "$D/org.json" <<'CFG'
{cfg}
CFG
"""
    if osname == "linux":
        exe = f'curl -fsSL "{srv}/agent/bin/linux" -o "$D/agent" && chmod +x "$D/agent"; CMD="$D/agent"' if have("linux") else \
              f'curl -fsSL "{srv}/agent/trackobee_agent.py" -o "$D/agent.py"; CMD="/usr/bin/env python3 $D/agent.py"'
        return "trackobee-setup.sh", pre + f"""{exe}
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/trackobee.service" <<UNIT
[Unit]
Description=Trackobee agent
[Service]
ExecStart=$CMD
Restart=always
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload && systemctl --user enable --now trackobee.service
echo "Trackobee installed. Refresh the website."
"""
    args = '<string>$D/agent</string>' if have("mac") else '<string>/usr/bin/python3</string><string>$D/agent.py</string>'
    exe = f'curl -fsSL "{srv}/agent/bin/mac" -o "$D/agent" && chmod +x "$D/agent"' if have("mac") else \
          f'curl -fsSL "{srv}/agent/trackobee_agent.py" -o "$D/agent.py"'
    return "trackobee-setup.command", pre + f"""{exe}
P="$HOME/Library/LaunchAgents/global.trackobee.agent.plist"; mkdir -p "$HOME/Library/LaunchAgents"
cat > "$P" <<PLIST
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>Label</key><string>global.trackobee.agent</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>
PLIST
launchctl unload "$P" 2>/dev/null || true; launchctl load "$P"
echo "Trackobee installed. Refresh the website."
"""

# ------------------------------------------------------------------ agent ingest (per-org agent token)
@app.post("/api/agent/heartbeat")
def heartbeat():
    tok = request.headers.get("X-Token", "")
    def q():
        with db() as c: return c.execute("SELECT * FROM orgs WHERE agent_token=? AND active=1", (tok,)).fetchone()
    org = cached(("agt", tok), 20, q) if tok else None
    if not org: return jsonify(error="bad token"), 401
    d = request.get_json(force=True)
    now, pub = int(time.time()), client_ip()
    t_org, crm = verify_token(d.get("token"))
    if not t_org or t_org["id"] != org["id"]: crm = None     # token must belong to the same org
    fac = facility_for(org["id"], pub, d.get("ip"))
    with db() as c:
        c.execute("INSERT INTO hb(org_id,ts,host,ip,pub_ip,os_user,crm_user,facility,state,idle) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (org["id"], now, str(d["host"])[:100], d.get("ip"), pub, str(d["os_user"])[:100], crm, fac, d["state"], int(d.get("idle", 0))))
        for e in d.get("events", [])[:50]:
            if e.get("type") in EVENT_TYPES:
                c.execute("INSERT INTO ev(org_id,ts,host,pub_ip,os_user,crm_user,facility,type) VALUES(?,?,?,?,?,?,?,?)",
                          (org["id"], int(e.get("ts", now)), str(d["host"])[:100], pub, str(d["os_user"])[:100], str(e.get("user") or crm or "")[:100] or None, fac, e["type"]))
    return jsonify(ok=True, interval=INTERVAL)

# ------------------------------------------------------------------ dashboard data (org-scoped, all roles)
@app.get("/api/live")
@need(*VIEW)
def live():
    now = int(time.time())
    with db() as c:
        rows = c.execute("SELECT h.* FROM hb h JOIN (SELECT host, MAX(id) m FROM hb WHERE org_id=? GROUP BY host) x ON h.id=x.m",
                         (request.user["org_id"],)).fetchall()
    out = []
    for r in rows:
        if now - r["ts"] > ONLINE_WINDOW: st = "offline"
        elif r["state"] != "active": st = "locked"
        elif r["idle"] > IDLE_LIMIT: st = "away"
        else: st = "available"
        out.append(dict(host=r["host"], ip=r["ip"], pub_ip=r["pub_ip"], os_user=r["os_user"], crm_user=r["crm_user"],
                        facility=r["facility"] or "Unmapped", state=st, idle=r["idle"], last_seen=r["ts"],
                        mismatch=bool(r["crm_user"] and r["crm_user"].lower() != r["os_user"].lower())))
    return jsonify(out)

def bounds(s):
    d = datetime.strptime(s, "%Y-%m-%d"); return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())

def build_report(org_id, s, e):
    with db() as c:
        hbs = c.execute("SELECT * FROM hb WHERE org_id=? AND ts>=? AND ts<? ORDER BY host, ts", (org_id, s, e)).fetchall()
    acc, prev = {}, {}
    for r in hbs:
        user = r["crm_user"] or "(unpaired) " + r["os_user"]; k = (user, r["host"])
        a = acc.setdefault(k, dict(user=user, host=r["host"], ips=set(), facility=None, first=r["ts"], last=r["ts"], screen_on=0, idle=0, locked=0))
        a["ips"].add(r["pub_ip"] or ""); a["last"] = r["ts"]; a["facility"] = r["facility"] or a["facility"]
        gap = min(r["ts"] - prev.get(k, r["ts"] - INTERVAL), MAX_GAP); prev[k] = r["ts"]
        if r["state"] != "active": a["locked"] += gap
        elif r["idle"] > IDLE_LIMIT: a["idle"] += gap
        else: a["screen_on"] += gap
    res = []
    for a in acc.values(): a["ips"] = sorted(i for i in a["ips"] if i); res.append(a)
    return sorted(res, key=lambda x: (x["user"], x["host"]))

def qdate():
    try: return bounds(request.args.get("date") or datetime.now().strftime("%Y-%m-%d"))
    except ValueError: abort(400)

@app.get("/api/report")
@need(*VIEW)
def report(): return jsonify(build_report(request.user["org_id"], *qdate()))

@app.get("/api/events")
@need(*VIEW)
def events():
    s, e = qdate()
    with db() as c:
        return jsonify([dict(r) for r in c.execute("SELECT * FROM ev WHERE org_id=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 500", (request.user["org_id"], s, e))])

@app.get("/api/export.csv")
@need(*VIEW)
def export():
    s, e = qdate(); buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["user", "host", "facility", "ips", "first_seen", "last_seen", "screen_on_min", "idle_min", "locked_min"])
    f = lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S")
    for a in build_report(request.user["org_id"], s, e):
        w.writerow([a["user"], a["host"], a["facility"], ";".join(a["ips"]), f(a["first"]), f(a["last"]), a["screen_on"] // 60, a["idle"] // 60, a["locked"] // 60])
    return Response(buf.getvalue(), mimetype="text/csv")

# ------------------------------------------------------------------ platform owner console (approve / create / suspend workspaces)
def create_workspace(c, org, name, email, pw_hash):
    oid = c.insert("INSERT INTO orgs(name,public_id,agent_token,crm_secret,created) VALUES(?,?,?,?,?)",
                   (org[:80], secrets.token_urlsafe(6), secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())))
    c.execute("INSERT INTO users(org_id,email,name,pw_hash,role,created) VALUES(?,?,?,?,?,?)", (oid, email, name[:80], pw_hash, "admin", int(time.time())))
    return oid

def owner_summary():
    since = int(time.time()) - 86400
    with db() as c:
        reqs = [dict(r) for r in c.execute("SELECT id,org,name,email,ip,created FROM signup_requests ORDER BY created")]
        orgs = [dict(r) for r in c.execute("SELECT id,name,public_id,active,created FROM orgs ORDER BY created")]
        for o in orgs:
            o["users"] = c.execute("SELECT COUNT(*) AS n FROM users WHERE org_id=?", (o["id"],)).fetchone()["n"]
            o["machines_24h"] = c.execute("SELECT COUNT(DISTINCT host) AS n FROM hb WHERE org_id=? AND ts>=?", (o["id"], since)).fetchone()["n"]
            a = c.execute("SELECT email FROM users WHERE org_id=? AND role='admin' ORDER BY id LIMIT 1", (o["id"],)).fetchone()
            o["admin"] = a["email"] if a else ""
    return dict(requests=reqs, orgs=orgs)

@app.get("/api/owner/summary")
@need(*OWNER)
def owner_get(): return jsonify(owner_summary())

@app.post("/api/owner/requests/<int:rid>/<action>")
@need(*OWNER)
def owner_request(rid, action):
    if action not in ("approve", "reject"): abort(404)
    with db() as c:
        r = c.execute("SELECT * FROM signup_requests WHERE id=?", (rid,)).fetchone()
        if not r: abort(404)
        if action == "approve":
            if c.execute("SELECT 1 FROM users WHERE email=?", (r["email"],)).fetchone(): return jsonify(error="email already has an account"), 409
            create_workspace(c, r["org"], r["name"], r["email"], r["pw_hash"])
        c.execute("DELETE FROM signup_requests WHERE id=?", (rid,))
    return jsonify(owner_summary())

@app.post("/api/owner/orgs")
@need(*OWNER)
def owner_create():
    d = request.get_json(force=True); email = (d.get("email") or "").strip().lower()
    if not ((d.get("org") or "").strip() and (d.get("name") or "").strip() and EMAIL.match(email)): return jsonify(error="organization, admin name and valid email required"), 400
    if not ok_password(d.get("password")): return jsonify(error="password must be at least 10 characters"), 400
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone(): return jsonify(error="email already has an account"), 409
        c.execute("DELETE FROM signup_requests WHERE email=?", (email,))
        create_workspace(c, d["org"].strip(), d["name"].strip(), email, generate_password_hash(d["password"]))
    return jsonify(owner_summary())

@app.patch("/api/owner/orgs/<int:oid>")
@need(*OWNER)
def owner_org_edit(oid):
    d = request.get_json(force=True)
    with db() as c: c.execute("UPDATE orgs SET active=? WHERE id=?", (1 if d.get("active") else 0, oid))
    uncache(); return jsonify(owner_summary())

@app.delete("/api/owner/orgs/<int:oid>")
@need(*OWNER)
def owner_org_del(oid):
    with db() as c:                       # destructive: removes the workspace and ALL its data
        for t in ("hb", "ev", "facilities", "users"): c.execute(f"DELETE FROM {t} WHERE org_id=?", (oid,))
        c.execute("DELETE FROM orgs WHERE id=?", (oid,))
    uncache(); return jsonify(owner_summary())

@app.after_request
def headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    if request.path in ("/login", "/app", "/owner"):
        r.headers["X-Frame-Options"] = "DENY"; r.headers["Cache-Control"] = "no-store"
    return r

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8090)))
