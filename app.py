"""Trackobee server: embed script, agent installers, ingest API, live dashboard."""
import base64, csv, hashlib, hmac, io, ipaddress, os, sqlite3, time
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, Response, abort

DB = os.environ.get("TB_DB", "trackobee.db")
AGENT_TOKEN = os.environ.get("TB_AGENT_TOKEN", "change-me")
CRM_SECRET = os.environ.get("TB_CRM_SECRET", "change-me-secret").encode()
DASH_USER = os.environ.get("TB_DASH_USER", "admin")
DASH_PASS = os.environ.get("TB_DASH_PASS", "change-me")
PUBLIC_URL = os.environ.get("TB_PUBLIC_URL", "").rstrip("/")
ALLOWED_ORIGINS = os.environ.get("TB_ALLOWED_ORIGINS", "https://customercentral.co")  # comma list
TRUST_PROXY = os.environ.get("TB_TRUST_PROXY") == "1"
INTERVAL, IDLE_LIMIT, ONLINE_WINDOW, MAX_GAP = 15, 300, 50, 45
EVENT_TYPES = {"login", "lock", "unlock", "crm_login", "crm_logout"}

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS hb(id INTEGER PRIMARY KEY, ts INTEGER, host TEXT, ip TEXT, pub_ip TEXT,
        os_user TEXT, crm_user TEXT, facility TEXT, state TEXT, idle INTEGER);
    CREATE TABLE IF NOT EXISTS ev(id INTEGER PRIMARY KEY, ts INTEGER, host TEXT, pub_ip TEXT,
        os_user TEXT, crm_user TEXT, facility TEXT, type TEXT);
    CREATE TABLE IF NOT EXISTS facilities(id INTEGER PRIMARY KEY, name TEXT, cidr TEXT UNIQUE);
    CREATE INDEX IF NOT EXISTS hb_ts ON hb(ts); CREATE INDEX IF NOT EXISTS ev_ts ON ev(ts);""")

# ---- CRM-signed pairing token: base64url("user:expiry").hmac ----
def make_token(user, ttl=86400):
    p = base64.urlsafe_b64encode(f"{user}:{int(time.time()) + ttl}".encode()).decode().rstrip("=")
    return p + "." + hmac.new(CRM_SECRET, p.encode(), hashlib.sha256).hexdigest()

def verify_token(tok):
    try:
        p, sig = (tok or "").split(".")
        if not hmac.compare_digest(sig, hmac.new(CRM_SECRET, p.encode(), hashlib.sha256).hexdigest()):
            return None
        user, exp = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode().rsplit(":", 1)
        return user if int(exp) > time.time() else None
    except Exception:
        return None

def client_ip():
    if TRUST_PROXY and request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[0].strip()
    return request.remote_addr

def facility_for(*ips):
    with db() as c:
        rows = c.execute("SELECT name, cidr FROM facilities").fetchall()
    for ip in ips:                       # public IP first, then LAN IP
        try: a = ipaddress.ip_address(ip)
        except Exception: continue
        best = None
        for r in rows:
            n = ipaddress.ip_network(r["cidr"], strict=False)
            if a.version == n.version and a in n and (best is None or n.prefixlen > best[0]):
                best = (n.prefixlen, r["name"])
        if best: return best[1]
    return None

PUBLIC = ("/embed.js", "/agent/", "/download/", "/api/agent/", "/favicon.ico")
@app.before_request
def auth():
    p = request.path
    if p.startswith("/api/agent/"):
        if request.headers.get("X-Token") != AGENT_TOKEN: return jsonify(error="bad token"), 401
        return
    if any(p.startswith(x) for x in PUBLIC): return
    a = request.authorization
    if not a or a.username != DASH_USER or not hmac.compare_digest(a.password or "", DASH_PASS):
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Trackobee"'})

# ---------------- embed + agent distribution ----------------
@app.get("/embed.js")
def embed():
    return send_from_directory(os.path.join(HERE, "static"), "embed.js", mimetype="application/javascript")

@app.get("/agent/trackobee_agent.py")
def agent_file():
    return send_from_directory(os.path.join(HERE, "agent"), "trackobee_agent.py", mimetype="text/plain")

def server_url(): return PUBLIC_URL or request.host_url.rstrip("/")

def installer(osname):
    srv, tok, orig = server_url(), AGENT_TOKEN, ALLOWED_ORIGINS
    if osname == "windows":
        return "trackobee-setup.bat", f"""@echo off
setlocal
set D=%LOCALAPPDATA%\\Trackobee
mkdir "%D%" 2>nul
where python >nul 2>&1 || (echo Python 3 is required: https://python.org & pause & exit /b 1)
powershell -NoProfile -Command "Invoke-WebRequest '{srv}/agent/trackobee_agent.py' -OutFile '%D%\\agent.py'"
> "%D%\\run.bat" echo @echo off
>> "%D%\\run.bat" echo set TB_SERVER={srv}
>> "%D%\\run.bat" echo set TB_AGENT_TOKEN={tok}
>> "%D%\\run.bat" echo set TB_ALLOWED_ORIGINS={orig}
>> "%D%\\run.bat" echo start "" pythonw "%D%\\agent.py"
schtasks /Create /TN Trackobee /TR "\\"%D%\\run.bat\\"" /SC ONLOGON /F >nul
call "%D%\\run.bat"
echo Trackobee installed. You can close this window and refresh the CRM page.
pause
"""
    env = f'TB_SERVER="{srv}" TB_AGENT_TOKEN="{tok}" TB_ALLOWED_ORIGINS="{orig}"'
    if osname == "linux":
        return "trackobee-setup.sh", f"""#!/bin/sh
set -e
D="$HOME/.trackobee"; mkdir -p "$D" "$HOME/.config/systemd/user"
curl -fsSL "{srv}/agent/trackobee_agent.py" -o "$D/agent.py"
cat > "$HOME/.config/systemd/user/trackobee.service" <<UNIT
[Unit]
Description=Trackobee agent
[Service]
Environment=TB_SERVER={srv} TB_AGENT_TOKEN={tok} TB_ALLOWED_ORIGINS={orig}
ExecStart=/usr/bin/env python3 $D/agent.py
Restart=always
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload && systemctl --user enable --now trackobee.service
echo "Trackobee installed. Refresh the CRM page."
"""
    return "trackobee-setup.command", f"""#!/bin/sh
set -e
D="$HOME/.trackobee"; mkdir -p "$D"
curl -fsSL "{srv}/agent/trackobee_agent.py" -o "$D/agent.py"
P="$HOME/Library/LaunchAgents/global.trackobee.agent.plist"
cat > "$P" <<PLIST
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>Label</key><string>global.trackobee.agent</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$D/agent.py</string></array>
<key>EnvironmentVariables</key><dict><key>TB_SERVER</key><string>{srv}</string>
<key>TB_AGENT_TOKEN</key><string>{tok}</string><key>TB_ALLOWED_ORIGINS</key><string>{orig}</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>
PLIST
launchctl unload "$P" 2>/dev/null || true; launchctl load "$P"
echo "Trackobee installed. Refresh the CRM page."
"""

INSTALLERS = os.environ.get("TB_INSTALLERS", os.path.join(HERE, "installers"))
BUILT = {"windows": "trackobee-setup.exe", "mac": "trackobee.pkg"}   # drop CI artifacts in ./installers

@app.get("/download/<osname>")
def download(osname):
    if osname not in ("windows", "linux", "mac") or not verify_token(request.args.get("token")):
        abort(403)      # only logged-in CRM users (valid signed token) can fetch an installer
    if osname in BUILT and os.path.isfile(os.path.join(INSTALLERS, BUILT[osname])):
        return send_from_directory(INSTALLERS, BUILT[osname], as_attachment=True)   # packaged build
    name, body = installer(osname)      # fallback: script installer (needs Python on the PC)
    return Response(body, mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={name}"})

# ---------------- ingest ----------------
@app.post("/api/agent/heartbeat")
def heartbeat():
    d = request.get_json(force=True)
    now, pub = int(time.time()), client_ip()
    crm = verify_token(d.get("token"))
    fac = facility_for(pub, d.get("ip"))
    with db() as c:
        c.execute("INSERT INTO hb(ts,host,ip,pub_ip,os_user,crm_user,facility,state,idle) VALUES(?,?,?,?,?,?,?,?,?)",
                  (now, d["host"], d.get("ip"), pub, d["os_user"], crm, fac, d["state"], int(d.get("idle", 0))))
        for e in d.get("events", [])[:50]:
            if e.get("type") in EVENT_TYPES:
                c.execute("INSERT INTO ev(ts,host,pub_ip,os_user,crm_user,facility,type) VALUES(?,?,?,?,?,?,?)",
                          (int(e.get("ts", now)), d["host"], pub, d["os_user"], e.get("user") or crm, fac, e["type"]))
    return jsonify(ok=True, interval=INTERVAL)

# ---------------- dashboard APIs ----------------
@app.get("/api/live")
def live():
    now = int(time.time())
    with db() as c:
        rows = c.execute("SELECT h.* FROM hb h JOIN (SELECT host, MAX(id) m FROM hb GROUP BY host) x ON h.id=x.m").fetchall()
    out = []
    for r in rows:
        online = now - r["ts"] <= ONLINE_WINDOW
        if not online: st = "offline"
        elif r["state"] != "active": st = "locked"
        elif r["idle"] > IDLE_LIMIT: st = "away"
        else: st = "available"
        out.append(dict(host=r["host"], ip=r["ip"], pub_ip=r["pub_ip"], os_user=r["os_user"], crm_user=r["crm_user"],
                        facility=r["facility"] or "Unmapped", state=st, idle=r["idle"], last_seen=r["ts"],
                        mismatch=bool(r["crm_user"] and r["crm_user"].lower() != r["os_user"].lower())))
    return jsonify(out)

def bounds(s):
    d = datetime.strptime(s, "%Y-%m-%d"); return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())

def build_report(s, e):
    with db() as c:
        hbs = c.execute("SELECT * FROM hb WHERE ts>=? AND ts<? ORDER BY host, ts", (s, e)).fetchall()
    acc, prev = {}, {}
    for r in hbs:
        user = r["crm_user"] or "(unpaired) " + r["os_user"]
        k = (user, r["host"])
        a = acc.setdefault(k, dict(user=user, host=r["host"], ips=set(), facility=None, first=r["ts"], last=r["ts"],
                                   screen_on=0, idle=0, locked=0))
        a["ips"].add(r["pub_ip"] or ""); a["last"] = r["ts"]; a["facility"] = r["facility"] or a["facility"]
        gap = min(r["ts"] - prev.get(k, r["ts"] - INTERVAL), MAX_GAP); prev[k] = r["ts"]
        if r["state"] != "active": a["locked"] += gap
        elif r["idle"] > IDLE_LIMIT: a["idle"] += gap
        else: a["screen_on"] += gap
    res = []
    for a in acc.values():
        a["ips"] = sorted(i for i in a["ips"] if i); res.append(a)
    return sorted(res, key=lambda x: (x["user"], x["host"]))

@app.get("/api/report")
def report():
    return jsonify(build_report(*bounds(request.args.get("date", datetime.now().strftime("%Y-%m-%d")))))

@app.get("/api/events")
def events():
    s, e = bounds(request.args.get("date", datetime.now().strftime("%Y-%m-%d")))
    with db() as c:
        return jsonify([dict(r) for r in c.execute(
            "SELECT * FROM ev WHERE ts>=? AND ts<? ORDER BY ts DESC LIMIT 500", (s, e))])

@app.get("/api/export.csv")
def export():
    s, e = bounds(request.args.get("date", datetime.now().strftime("%Y-%m-%d")))
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["user", "host", "facility", "ips", "first_seen", "last_seen", "screen_on_min", "idle_min", "locked_min"])
    f = lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S")
    for a in build_report(s, e):
        w.writerow([a["user"], a["host"], a["facility"], ";".join(a["ips"]), f(a["first"]), f(a["last"]),
                    a["screen_on"] // 60, a["idle"] // 60, a["locked"] // 60])
    return Response(buf.getvalue(), mimetype="text/csv")

@app.route("/api/facilities", methods=["GET", "POST", "DELETE"])
def facilities():
    with db() as c:
        if request.method == "POST":
            d = request.get_json(force=True)
            try: ipaddress.ip_network(d["cidr"], strict=False)
            except Exception: return jsonify(error="invalid CIDR"), 400
            c.execute("INSERT OR REPLACE INTO facilities(name,cidr) VALUES(?,?)", (d["name"].strip()[:80], d["cidr"].strip()))
        if request.method == "DELETE":
            c.execute("DELETE FROM facilities WHERE id=?", (request.args.get("id"),))
        return jsonify([dict(r) for r in c.execute("SELECT * FROM facilities ORDER BY name")])

@app.get("/")
def index(): return send_from_directory(os.path.join(HERE, "static"), "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8090)))
