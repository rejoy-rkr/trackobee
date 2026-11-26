"""Trackobee agent - Windows / Linux / macOS, Python 3 standard library only.
Collects ONLY: hostname, LAN IP, OS username, lock/idle state, session events, and the
CRM user identity handed over by the Trackobee page (signed token). No screenshots/keystrokes/content."""
import ctypes, getpass, json, os, platform, shutil, socket, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.1"

def _config():
    """Per-organization settings (server, agent_token, allowed_origins). Search order: baked-in config.json,
    ~/.trackobee/org.json (written by the setup script), then a system-wide file (for MDM / GPO deployments)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(base, "config.json"), os.path.join(os.path.expanduser("~"), ".trackobee", "org.json"),
             "/Library/Trackobee/org.json", "/etc/trackobee/org.json",
             os.path.join(os.environ.get("PROGRAMDATA", ""), "Trackobee", "org.json")]
    for p in paths:
        try:
            with open(p) as f: return json.load(f)
        except Exception: continue
    return {}
_B = _config()
SERVER = (_B.get("server") or os.environ.get("TB_SERVER", "http://localhost:8090")).rstrip("/")
AGENT_TOKEN = _B.get("agent_token") or os.environ.get("TB_AGENT_TOKEN", "")
ALLOWED = [o.strip() for o in (_B.get("allowed_origins") or os.environ.get("TB_ALLOWED_ORIGINS", "")).split(",") if o.strip()]
PORT, INTERVAL, OSN = 47821, 15, platform.system()
CFG = os.path.join(os.path.expanduser("~"), ".trackobee", "config.json")
S = {"token": None, "user": None, "events": []}
L = threading.Lock()

def load():
    try:
        with open(CFG) as f: S["token"] = json.load(f).get("token")
    except Exception: pass

def save():
    try:
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        with open(CFG, "w") as f: json.dump({"token": S["token"]}, f)
        os.chmod(CFG, 0o600)
    except Exception: pass

def token_user(tok):  # display only; the server does the real signature check
    import base64
    try:
        p = tok.split(".")[1]; return base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode().rsplit(":", 1)[0]
    except Exception: return None

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except Exception: return None
    finally: s.close()

def _run(cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception: return ""

def _linux_state():
    """Linux desktop: lock state + idle seconds. GNOME/Ubuntu via D-Bus, X11 via xprintidle, else systemd-logind."""
    for dest, path, meth in (("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver.GetActive"),
                             ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver.GetActive")):
        if "true" in _run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path, "--method", meth]):
            return "locked", 0
    idle = None
    o = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
              "--object-path", "/org/gnome/Mutter/IdleMonitor/Core", "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
    if o:                                   # "(uint64 12345,)" in milliseconds
        digits = "".join(ch for ch in o.split("uint64")[-1] if ch.isdigit())
        if digits: idle = int(digits) // 1000
    if idle is None:
        x = _run(["xprintidle"])
        if x.isdigit(): idle = int(x) // 1000
    sid = os.environ.get("XDG_SESSION_ID") or _run(["loginctl", "show-user", getpass.getuser(), "-p", "Display", "--value"]) or "auto"
    kv = dict(l.split("=", 1) for l in _run(["loginctl", "show-session", sid, "-p", "LockedHint", "-p", "IdleHint", "-p", "IdleSinceHint"]).splitlines() if "=" in l)
    if kv.get("LockedHint") == "yes": return "locked", 0
    if idle is None:
        idle = 0
        if kv.get("IdleHint") == "yes" and kv.get("IdleSinceHint", "0") not in ("", "0"):
            idle = int(time.time() - int(kv["IdleSinceHint"]) / 10**6)
    return "active", max(idle, 0)

def state_idle():
    """-> ('active'|'locked', idle_seconds)"""
    try:
        if OSN == "Windows":
            class LII(ctypes.Structure): _fields_ = [("cb", ctypes.c_uint), ("t", ctypes.c_uint)]
            li = LII(); li.cb = ctypes.sizeof(li); ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li))
            idle = (ctypes.windll.kernel32.GetTickCount() - li.t) // 1000
            h = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
            if not h: return "locked", idle
            ctypes.windll.user32.CloseDesktop(h); return "active", idle
        if OSN == "Darwin":
            o = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True).stdout
            idle = next(int(l.split("=")[-1]) // 10**9 for l in o.splitlines() if "HIDIdleTime" in l)
            q = subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True, text=True).stdout
            return ("locked" if "CGSSessionScreenIsLocked" in q else "active"), idle
        return _linux_state()
    except Exception:
        return "active", 0

def event(t, user=None):
    with L: S["events"].append({"type": t, "ts": int(time.time()), **({"user": user} if user else {})})

def beat_loop():
    host, osu, last = socket.gethostname(), getpass.getuser(), None
    event("login")
    while True:
        st, idle = state_idle()
        if last and st != last: event("lock" if st == "locked" else "unlock")
        last = st
        with L: evs, tok = list(S["events"]), S["token"]
        body = json.dumps(dict(host=host, ip=local_ip(), os_user=osu, state=st, idle=idle, token=tok, events=evs)).encode()
        try:
            rq = urllib.request.Request(SERVER + "/api/agent/heartbeat", body,
                                        {"Content-Type": "application/json", "X-Token": AGENT_TOKEN})
            urllib.request.urlopen(rq, timeout=10).read()
            with L: del S["events"][:len(evs)]
        except Exception: pass
        time.sleep(INTERVAL)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _host_ok(self): return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")  # anti DNS-rebinding
    def _origin(self): o = self.headers.get("Origin", ""); return o if o in ALLOWED else None
    def _send(self, code, obj=None):
        self.send_response(code)
        o = self._origin()
        if o:
            self.send_header("Access-Control-Allow-Origin", o); self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        data = json.dumps(obj or {}).encode()
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def do_OPTIONS(self): self._send(204 if self._host_ok() and self._origin() else 403)
    def do_GET(self):
        if not self._host_ok(): return self._send(403)
        if self.path == "/status": self._send(200, dict(agent="trackobee", version=VERSION, paired=bool(S["token"])))
        else: self._send(404)
    def do_POST(self):
        if not (self._host_ok() and self._origin()): return self._send(403)
        try: d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception: return self._send(400)
        if self.path == "/pair" and d.get("token"):
            u = token_user(d["token"])
            if u != S["user"]:
                if S["user"]: event("crm_logout", S["user"])
                event("crm_login", u)
            with L: S["token"], S["user"] = d["token"], u
            save(); self._send(200, dict(ok=True))
        elif self.path == "/unpair":
            if S["user"]: event("crm_logout", S["user"])
            with L: S["token"], S["user"] = None, None
            save(); self._send(200, dict(ok=True))
        else: self._send(404)

# ---- Windows self-install (the downloaded trackobee-setup.exe installs itself, no admin needed) ----
WIN_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Trackobee")
WIN_EXE = os.path.join(WIN_DIR, "trackobee-agent.exe")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def msgbox(text):
    try: ctypes.windll.user32.MessageBoxW(0, text, "Trackobee", 0x40)
    except Exception: pass

def win_install():
    import winreg
    os.makedirs(WIN_DIR, exist_ok=True)
    subprocess.run(["taskkill", "/F", "/IM", "trackobee-agent.exe"], capture_output=True)
    time.sleep(1); shutil.copy2(sys.executable, WIN_EXE)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, "Trackobee", 0, winreg.REG_SZ, '"%s" --run' % WIN_EXE)
    subprocess.Popen([WIN_EXE, "--run"], creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED | NEW_GROUP
    msgbox("Trackobee is installed. Please refresh the CRM page.")

def win_uninstall():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k: winreg.DeleteValue(k, "Trackobee")
    except OSError: pass
    subprocess.run(["taskkill", "/F", "/IM", "trackobee-agent.exe"], capture_output=True)
    shutil.rmtree(os.path.join(os.path.expanduser("~"), ".trackobee"), ignore_errors=True)
    msgbox("Trackobee was removed. Delete %s to finish." % WIN_DIR)

def run():
    try: srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError: return          # already running for this user
    load(); S["user"] = token_user(S["token"]) if S["token"] else None
    threading.Thread(target=beat_loop, daemon=True).start()
    srv.serve_forever()

if __name__ == "__main__":
    a = sys.argv[1:]
    if OSN == "Windows" and getattr(sys, "frozen", False):
        if "--uninstall" in a: win_uninstall()
        elif "--run" in a or os.path.normcase(os.path.dirname(sys.executable)) == os.path.normcase(WIN_DIR): run()
        else: win_install()
    else:
        run()
