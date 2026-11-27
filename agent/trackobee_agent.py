"""Trackobee agent - Windows / Linux / macOS, Python 3.7+ standard library only.

Collects: hostname, LAN IP, OS username, lock/idle state, session events, the foreground application NAME (never window titles,
keystrokes, screenshots or content), a hardware/OS inventory (serial number, model, CPU, RAM, disks, MACs ...), battery and other
light telemetry, and an approximate (IP based, city level) location. Everything except the heartbeat basics is free-form JSON, so new
fields can be added here without touching the database or the installer.

The agent keeps itself up to date: every few hours it downloads the newest agent from the organization's site, tests it, swaps it in
and restarts itself. The installer therefore never needs to change.
"""
import ctypes, getpass, json, os, platform, re, shutil, signal, socket, subprocess, sys, threading, time, urllib.error, urllib.request
import itertools
# Imported up front so a frozen (PyInstaller) build bundles them: a self-updated agent may use any of these without a rebuilt binary.
import base64, collections, configparser, csv, datetime, functools, glob, hashlib, hmac, ipaddress, locale, plistlib, queue, random  # noqa: F401
import runpy, sqlite3, ssl, struct, tempfile, textwrap, traceback, uuid, xml.etree.ElementTree, zlib  # noqa: F401
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "4.1"

FROZEN = getattr(sys, "frozen", False)
OSN = platform.system()
HOME_DIR = os.path.join(os.path.expanduser("~"), ".trackobee")
SELF = None if FROZEN else os.path.abspath(__file__)
UPDATE_PATH = SELF if SELF else os.path.join(HOME_DIR, "agent_update.py")     # where a newer agent is written
UPDATE_EVERY, INVENTORY_EVERY, SLOW_TELEMETRY_EVERY = 6 * 3600, 3600, 300


def _config():
    """Per-organization settings (server, agent_token, allowed_origins). Search order: baked-in config.json,
    ~/.trackobee/org.json (written by the setup script), then a system-wide file (for MDM / GPO deployments)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(base, "config.json"), os.path.join(HOME_DIR, "org.json"),
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
INGEST = _B.get("ingest") or ""          # Supabase RPC URL (tb_ingest_heartbeat); empty => legacy self-hosted server
APIKEY = _B.get("apikey") or ""          # Supabase anon (public) key
ALLOWED = [o.strip() for o in (_B.get("allowed_origins") or os.environ.get("TB_ALLOWED_ORIGINS", "")).split(",") if o.strip()]
PORT, INTERVAL = int(os.environ.get("TB_AGENT_PORT", "47821")), int(os.environ.get("TB_AGENT_INTERVAL", "15"))
CFG = os.path.join(HOME_DIR, "config.json")
STATE_FILE = os.path.join(HOME_DIR, "state.json")
S = {"token": None, "user": None, "events": [], "sys": None, "sys_new": False, "loc": None, "loc_new": False,
     "kwin": False, "restart": False, "ssid": (0, None)}
L = threading.Lock()
SRV = None


# ------------------------------------------------------------------ small helpers
def _child_env():
    """A frozen (PyInstaller) binary points LD_LIBRARY_PATH at its own bundled libraries, which breaks system tools such as
    gdbus, journalctl and nmcli. Give child processes the original environment."""
    env = dict(os.environ)
    if FROZEN:
        for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LIBPATH"):
            if k + "_ORIG" in env: env[k] = env[k + "_ORIG"]
            else: env.pop(k, None)
    return env

def sh(cmd, timeout=8, shell=False):
    """Run a command, return stripped stdout ('' on any failure). Never raises, never pops a console window."""
    try:
        kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": timeout, "shell": shell,
              "encoding": "utf-8", "errors": "replace", "env": _child_env()}
        if OSN == "Windows": kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        return (subprocess.run(cmd, **kw).stdout or "").strip()
    except Exception:
        return ""

def _run(cmd): return sh(cmd, 3)

def _try(fn, default=None):
    try: return fn()
    except Exception: return default

def _read(path):
    try:
        with open(path) as f: return f.read().strip()
    except Exception: return ""

def _clean(d):
    """Drop None / empty values so a missing reading never overwrites a good one stored on the server."""
    return {k: v for k, v in (d or {}).items() if v is not None and v != "" and v != [] and v != {}}

def _vt(v): return tuple(int(x) for x in re.findall(r"\d+", v or "0"))

def http_get(url, timeout=10):
    rq = urllib.request.Request(url, headers={"User-Agent": "Trackobee-Agent/" + VERSION})
    return urllib.request.urlopen(rq, timeout=timeout).read()

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}

def save_state(**kw):
    try:
        d = load_state(); d.update(kw)
        os.makedirs(HOME_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f: json.dump(d, f)
    except Exception: pass

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
    try:
        p = tok.split(".")[1]; return base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)).decode().rsplit(":", 1)[0]
    except Exception: return None

def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except Exception: return None
    finally: s.close()

_FRIENDLY = {"winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook", "onenote": "OneNote", "chrome": "Chrome",
             "msedge": "Edge", "firefox": "Firefox", "brave": "Brave", "code": "VS Code", "explorer": "File Explorer", "teams": "Teams",
             "ms-teams": "Teams", "slack": "Slack", "zoom": "Zoom", "notepad": "Notepad", "cmd": "Command Prompt", "powershell": "PowerShell",
             "windowsterminal": "Windows Terminal", "acrobat": "Acrobat", "acrord32": "Acrobat Reader", "whatsapp": "WhatsApp"}
def _friendly(name):
    if not name: return None
    n = re.sub(r"\.exe$", "", name.strip(), flags=re.I)
    n = re.sub(r"^(org|com|io|net)\.([A-Za-z0-9_-]+\.)+", "", n) if " " not in n else n      # org.kde.konsole -> konsole
    if n.lower() in _FRIENDLY: return _FRIENDLY[n.lower()]
    return (n.capitalize() if n.islower() else n)[:100]


# ------------------------------------------------------------------ user-session environment (Linux services start without it)
def _import_user_env():
    if OSN != "Linux": return
    try:
        rd = "/run/user/%d" % os.getuid()
        if "XDG_RUNTIME_DIR" not in os.environ and os.path.isdir(rd): os.environ["XDG_RUNTIME_DIR"] = rd
        if "DBUS_SESSION_BUS_ADDRESS" not in os.environ and os.path.exists(rd + "/bus"):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + rd + "/bus"
        for line in sh(["systemctl", "--user", "show-environment"], 3).splitlines():
            k, _, v = line.partition("=")
            if k in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_SESSION_ID") and k not in os.environ:
                os.environ[k] = v
    except Exception: pass
_import_user_env()

def _is_wayland(): return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


# ------------------------------------------------------------------ lock / idle state
_IRQ = {"sum": None, "t": time.time()}
_IRQ_RE = re.compile(r"i8042|elan|synaptics|i2c[_-]hid|hid-over|touchpad|trackpoint|keyboard|mouse|atkbd|psmouse|xhci", re.I)

def _irq_idle():
    """Last-resort idle estimate for Wayland desktops that expose no idle API (e.g. KDE): input devices raise interrupts."""
    try:
        tot, found = 0, False
        for line in _read("/proc/interrupts").splitlines()[1:]:
            if _IRQ_RE.search(line):
                found = True
                tot += sum(int(x) for x in itertools.takewhile(str.isdigit, line.split()[1:]))
        if not found: return None
        now = time.time()
        if _IRQ["sum"] is None or tot != _IRQ["sum"]: _IRQ["t"] = now
        _IRQ["sum"] = tot
        return int(now - _IRQ["t"])
    except Exception:
        return None

def _linux_state():
    """Linux desktop: lock state + idle seconds. GNOME via D-Bus, X11 via xprintidle, KDE/other Wayland via input interrupts."""
    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY") and time.time() - S.get("env_t", 0) > 30:
        S["env_t"] = time.time(); _import_user_env()          # service started before the desktop session exported its environment
    for dest, path, meth in (("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver.GetActive"),
                             ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver.GetActive"),
                             ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver.GetActive")):
        if "true" in _run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path, "--method", meth]):
            return "locked", 0
    idle = None
    o = _run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
              "--object-path", "/org/gnome/Mutter/IdleMonitor/Core", "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"])
    if o:                                   # "(uint64 12345,)" in milliseconds
        digits = "".join(ch for ch in o.split("uint64")[-1] if ch.isdigit())
        if digits: idle = int(digits) // 1000
    if idle is None and not _is_wayland():
        x = _run(["xprintidle"])
        if x.isdigit(): idle = int(x) // 1000
    if idle is None:
        idle = _irq_idle()
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
            idle = (((ctypes.windll.kernel32.GetTickCount() - li.t) & 0xFFFFFFFF)) // 1000      # modular: survives the 49-day tick wrap
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


# ------------------------------------------------------------------ foreground application (name only)
_KWIN_JS = ('function rep(w){ print("TBAPP|" + (w ? (w.resourceClass || w.caption || "") : "")); }\n'
            'var sig = workspace.windowActivated || workspace.clientActivated;\nif (sig) sig.connect(rep);\n'
            'rep(workspace.activeWindow || workspace.activeClient);\n')

def kwin_setup():
    """KDE Wayland: a tiny KWin script reports the active window class to the user journal; we read the last line."""
    try:
        if OSN != "Linux" or not shutil.which("gdbus") or not shutil.which("journalctl"): return False
        if "true" not in sh(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
                             "--method", "org.freedesktop.DBus.NameHasOwner", "org.kde.KWin"], 3): return False
        os.makedirs(HOME_DIR, exist_ok=True)
        path = os.path.join(HOME_DIR, "kwin_app.js")
        with open(path, "w") as f: f.write(_KWIN_JS)
        sh(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/Scripting",
            "--method", "org.kde.kwin.Scripting.unloadScript", "trackobee"], 3)
        m = re.search(r"(\d+)", sh(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/Scripting",
                                    "--method", "org.kde.kwin.Scripting.loadScript", path, "trackobee"], 3))
        if not m: return False
        for obj in ("/Scripting/Script" + m.group(1), "/" + m.group(1)):
            if sh(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", obj, "--method", "org.kde.kwin.Script.run"], 3) == "()":
                S["kwin"] = True; return True
    except Exception: pass
    return False

def _kwin_app():
    if not S["kwin"]: return None
    o = sh(["journalctl", "--user", "-o", "cat", "--no-pager", "-g", "TBAPP[|]", "-n", "1"], 3)
    return o.split("TBAPP|", 1)[1].strip() if "TBAPP|" in o else None

def _x11_app():
    o = sh(["xprop", "-root", "_NET_ACTIVE_WINDOW"], 2)
    m = re.search(r"(0x[0-9a-fA-F]+)", o)
    if not m or int(m.group(1), 16) == 0: return None
    o2 = sh(["xprop", "-id", m.group(1), "_NET_WM_PID", "WM_CLASS"], 2)
    pid = re.search(r"_NET_WM_PID\(CARDINAL\) = (\d+)", o2)
    name = _read("/proc/%s/comm" % pid.group(1)) if pid else ""
    if not name:
        cls = re.findall(r'"([^"]+)"', o2); name = cls[-1] if cls else ""
    return name or None

def _win_app():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    hwnd = u.GetForegroundWindow()
    if not hwnd: return None
    pid = ctypes.c_ulong(); u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h: return None
    try:
        buf = ctypes.create_unicode_buffer(1024); n = ctypes.c_ulong(1024)
        return os.path.basename(buf.value) if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally: k.CloseHandle(h)

def _mac_app():
    asn = sh(["lsappinfo", "front"], 3)
    m = re.search(r'="([^"]+)"', sh(["lsappinfo", "info", "-only", "name", asn], 3)) if asn else None
    return m.group(1) if m else None

def foreground_app():
    try:
        n = _win_app() if OSN == "Windows" else _mac_app() if OSN == "Darwin" else (_kwin_app() or _x11_app())
        return _friendly(n)
    except Exception:
        return None


# ------------------------------------------------------------------ telemetry (light, every heartbeat) and inventory (hourly)
def _ssid():
    t, v = S["ssid"]
    if time.time() - t < SLOW_TELEMETRY_EVERY: return v
    v = None
    try:
        if OSN == "Linux":
            for line in sh(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], 4).splitlines():
                if line.startswith("yes:"): v = line[4:].replace("\\:", ":"); break
            v = v or sh(["iwgetid", "-r"], 3) or None
        elif OSN == "Windows":
            m = re.search(r"^\s*SSID\s*:\s*(.+)$", sh(["netsh", "wlan", "show", "interfaces"], 5), re.M)
            v = m.group(1).strip() if m else None
        elif OSN == "Darwin":
            for dev in ("en0", "en1"):
                m = re.search(r"Network:\s*(.+)$", sh(["networksetup", "-getairportnetwork", dev], 3))
                if m: v = m.group(1).strip(); break
    except Exception: pass
    S["ssid"] = (time.time(), v); return v

def _battery():
    if OSN == "Linux":
        bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        ac = [_read(p) for p in glob.glob("/sys/class/power_supply/A*/online")]
        if not bats: return {"has_battery": False, "ac_power": (("1" in ac) if ac else None)}
        cap, status = _read(bats[0] + "/capacity"), _read(bats[0] + "/status")
        return {"has_battery": True, "battery_percent": int(cap) if cap.isdigit() else None, "battery_status": status,
                "charging": status == "Charging", "ac_power": (("1" in ac) if ac else None)}
    if OSN == "Windows":
        class SPS(ctypes.Structure):
            _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte), ("sys", ctypes.c_ubyte),
                        ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
        s = SPS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)): return {}
        if s.flag == 128: return {"has_battery": False, "ac_power": s.ac == 1}
        return {"has_battery": True, "battery_percent": s.pct if s.pct <= 100 else None, "charging": bool(s.flag & 8),
                "ac_power": s.ac == 1, "battery_minutes_left": (s.life // 60) if s.life != 0xFFFFFFFF else None}
    if OSN == "Darwin":
        o = sh(["pmset", "-g", "batt"], 3)
        m = re.search(r"(\d+)%;\s*([A-Za-z ]+)", o)
        if not m: return {"has_battery": False}
        return {"has_battery": True, "battery_percent": int(m.group(1)), "battery_status": m.group(2).strip(),
                "charging": m.group(2).strip().lower().startswith("charging"), "ac_power": "AC Power" in o}
    return {}

def _uptime():
    if OSN == "Linux": return int(float(_read("/proc/uptime").split()[0]))
    if OSN == "Windows":
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return int(ctypes.windll.kernel32.GetTickCount64() // 1000)
    m = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"], 3))
    return int(time.time() - int(m.group(1))) if m else None

def _mem_used_percent():
    if OSN == "Linux":
        mi = dict(re.findall(r"(\w+):\s+(\d+)", _read("/proc/meminfo")))
        return round(100 - 100 * int(mi["MemAvailable"]) / int(mi["MemTotal"]))
    if OSN == "Windows":
        class MS(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                        ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                        ("ae", ctypes.c_ulonglong)]
        m = MS(); m.len = ctypes.sizeof(m); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)); return int(m.load)
    return None

def telemetry():
    root = "C:\\" if OSN == "Windows" else "/"
    free = _try(lambda: round(shutil.disk_usage(root).free / 1e9, 1))
    return _clean(dict(_try(_battery, {}), uptime_s=_try(_uptime), mem_used_percent=_try(_mem_used_percent), disk_free_gb=free,
                       wifi_ssid=_try(_ssid), agent_version=VERSION, idle_method=("irq" if OSN == "Linux" and _IRQ["sum"] is not None else None)))

_PLACEHOLDERS = {"none", "to be filled by o.e.m.", "default string", "not specified", "system serial number", "not applicable", "unknown", "0", "no asset information"}

def _linux_sys():
    d, dmi = {}, "/sys/class/dmi/id/"
    for key, f in (("manufacturer", "sys_vendor"), ("model", "product_name"), ("model_version", "product_version"), ("serial_number", "product_serial"),
                   ("board_serial", "board_serial"), ("chassis_serial", "chassis_serial"), ("uuid", "product_uuid"), ("bios_vendor", "bios_vendor"),
                   ("bios_version", "bios_version"), ("bios_date", "bios_date"), ("chassis_type", "chassis_type"), ("asset_tag", "chassis_asset_tag")):
        v = _read(dmi + f)
        if v and v.lower() not in _PLACEHOLDERS and not (key == "asset_tag" and v.lower().startswith("no asset")): d[key] = v
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    d["os"], d["os_version"], d["kernel"] = osr.get("PRETTY_NAME"), osr.get("VERSION_ID"), platform.release()
    cpu = _read("/proc/cpuinfo")
    m = re.search(r"model name\s*:\s*(.+)", cpu); d["cpu"] = m.group(1).strip() if m else platform.processor()
    mi = dict(re.findall(r"(\w+):\s+(\d+)", _read("/proc/meminfo")))
    if "MemTotal" in mi: d["ram_gb"] = round(int(mi["MemTotal"]) / 1048576, 1)
    d["machine_id"] = _read("/etc/machine-id")
    d["disks"] = _try(lambda: [x for x in json.loads(sh(["lsblk", "-dJ", "-o", "NAME,SIZE,MODEL,SERIAL,TYPE,ROTA"], 5)).get("blockdevices", []) if not re.match(r"(zram|loop|ram)", x.get("name", ""))])
    d["macs"] = sorted({_read(p) for p in glob.glob("/sys/class/net/*/address") if os.path.exists(os.path.dirname(p) + "/device") and _read(p) != "00:00:00:00:00:00"})
    d["desktop"], d["session_type"] = os.environ.get("XDG_CURRENT_DESKTOP"), os.environ.get("XDG_SESSION_TYPE")
    d["gpu"] = [l.split(": ", 1)[-1] for l in sh(["lspci"], 4).splitlines() if re.search(r"VGA|3D|Display", l)] if shutil.which("lspci") else None
    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if bats:
        b = bats[0]; full, design = _read(b + "/energy_full") or _read(b + "/charge_full"), _read(b + "/energy_full_design") or _read(b + "/charge_full_design")
        d["battery_model"], d["battery_serial"] = _read(b + "/model_name"), _read(b + "/serial_number")
        d["battery_technology"], d["battery_cycles"] = _read(b + "/technology"), _read(b + "/cycle_count")
        if full.isdigit() and design.isdigit() and int(design): d["battery_health_percent"] = round(100 * int(full) / int(design))
    return d

def _windows_sys():
    ps = ("$ErrorActionPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;$o=[ordered]@{};"
          "$b=Get-CimInstance Win32_BIOS;$cs=Get-CimInstance Win32_ComputerSystem;$os=Get-CimInstance Win32_OperatingSystem;"
          "$cpu=@(Get-CimInstance Win32_Processor)[0];$csp=Get-CimInstance Win32_ComputerSystemProduct;$bb=Get-CimInstance Win32_BaseBoard;"
          "$o.serial_number=$b.SerialNumber;$o.bios_version=$b.SMBIOSBIOSVersion;$o.bios_date=[string]$b.ReleaseDate;"
          "$o.manufacturer=$cs.Manufacturer;$o.model=$cs.Model;$o.domain=$cs.Domain;$o.part_of_domain=$cs.PartOfDomain;"
          "$o.ram_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,1);$o.uuid=$csp.UUID;$o.board_serial=$bb.SerialNumber;"
          "$o.os=$os.Caption;$o.os_version=$os.Version;$o.os_build=$os.BuildNumber;$o.os_install_date=[string]$os.InstallDate;$o.last_boot=[string]$os.LastBootUpTime;"
          "$o.cpu=$cpu.Name;$o.cpu_cores=$cpu.NumberOfCores;$o.cpu_threads=$cpu.NumberOfLogicalProcessors;"
          "$o.gpu=@(Get-CimInstance Win32_VideoController|%{$_.Name});"
          "$o.disks=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'|%{@{drive=$_.DeviceID;size_gb=[math]::Round($_.Size/1GB);free_gb=[math]::Round($_.FreeSpace/1GB)}});"
          "$o.physical_disks=@(Get-CimInstance Win32_DiskDrive|%{@{model=$_.Model;serial=([string]$_.SerialNumber).Trim();size_gb=[math]::Round($_.Size/1GB)}});"
          "$o.macs=@(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=true'|%{$_.MACAddress});"
          "$o.monitors=@(Get-CimInstance Win32_DesktopMonitor|%{$_.Name});"
          "$bt=Get-CimInstance Win32_Battery;if($bt){$o.battery_name=$bt.Name;$o.battery_design_mv=$bt.DesignVoltage};"
          "$o.antivirus=@(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct|%{$_.displayName});"
          "$o|ConvertTo-Json -Compress -Depth 5")
    out = sh(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps], 60)
    d = json.loads(out[out.index("{"):]) if "{" in out else {}
    for k in ("serial_number", "board_serial", "uuid"):
        if str(d.get(k, "")).strip().lower() in _PLACEHOLDERS: d.pop(k, None)
    return d

def _mac_sys():
    d = {"manufacturer": "Apple"}
    hw = (json.loads(sh(["system_profiler", "SPHardwareDataType", "-json"], 40)).get("SPHardwareDataType") or [{}])[0]
    d.update(model=hw.get("machine_name"), model_identifier=hw.get("machine_model"), serial_number=hw.get("serial_number"), uuid=hw.get("platform_UUID"),
             cpu=hw.get("chip_type") or hw.get("cpu_type"), ram=hw.get("physical_memory"), cores=hw.get("number_processors"))
    d["os"] = "%s %s" % (sh(["sw_vers", "-productName"], 3), sh(["sw_vers", "-productVersion"], 3))
    d["os_build"] = sh(["sw_vers", "-buildVersion"], 3)
    d["disk_total_gb"] = _try(lambda: round(shutil.disk_usage("/").total / 1e9))
    return d

def collect_sys():
    """Hardware / OS inventory. Every collector is isolated, so one failing probe never loses the rest."""
    d = {"agent_version": VERSION, "collected_at": int(time.time()), "python": platform.python_version(), "platform": platform.platform(),
         "hostname": socket.gethostname(), "fqdn": _try(socket.getfqdn), "os_user": getpass.getuser(), "arch": platform.machine(),
         "cpu_logical": os.cpu_count(), "utc_offset_min": -time.timezone // 60, "timezone": time.strftime("%Z"),
         "lan_ip": _try(local_ip), "mac": _try(lambda: ":".join("%02x" % ((uuid.getnode() >> s) & 0xff) for s in range(40, -1, -8))),
         "disk_total_gb": _try(lambda: round(shutil.disk_usage("C:\\" if OSN == "Windows" else "/").total / 1e9))}
    d.update(_clean(_try({"Linux": _linux_sys, "Windows": _windows_sys, "Darwin": _mac_sys}.get(OSN, lambda: {}), {})))
    for extra in ("hw.json", "extra.json"):         # optional files dropped by the installer / admins; merged as-is
        try:
            with open(os.path.join(HOME_DIR, extra)) as f:
                d.update({k: v for k, v in _clean(json.load(f)).items() if str(v).strip().lower() not in _PLACEHOLDERS})
        except Exception: pass
    return _clean(d)

def fetch_location():
    """Approximate (city level) location from the public IP. The only outside service the agent calls besides your own backend."""
    try:
        j = json.loads(http_get("https://ipwho.is/", 10))
        if j.get("success"):
            return _clean({"ip": j.get("ip"), "city": j.get("city"), "region": j.get("region"), "country": j.get("country"),
                           "lat": j.get("latitude"), "lon": j.get("longitude"), "isp": (j.get("connection") or {}).get("isp"),
                           "timezone": (j.get("timezone") or {}).get("id"), "source": "ip", "accuracy": "city", "ts": int(time.time())})
    except Exception: pass
    try:
        j = json.loads(http_get("https://ipapi.co/json/", 10))
        if j.get("ip") and not j.get("error"):
            return _clean({"ip": j.get("ip"), "city": j.get("city"), "region": j.get("region"), "country": j.get("country_name"),
                           "lat": j.get("latitude"), "lon": j.get("longitude"), "isp": j.get("org"), "timezone": j.get("timezone"),
                           "source": "ip", "accuracy": "city", "ts": int(time.time())})
    except Exception: pass
    return None


# ------------------------------------------------------------------ events + heartbeat
def event(t, user=None):
    with L: S["events"].append({"type": t, "ts": int(time.time()), **({"user": user} if user else {})})

def send_beat(host, osu, st, idle, evs, tok, extra=None):
    """One heartbeat to the backend. True when it was accepted (so queued events can be dropped)."""
    extra = extra or {}
    try:
        if INGEST:     # Supabase: call the tb_ingest_heartbeat database function directly
            base = dict(p_agent_token=AGENT_TOKEN, p_host=host, p_ip=local_ip(), p_os_user=osu, p_state=st, p_idle=int(idle), p_token=tok, p_events=evs)
            full = dict(base, p_app=extra.get("app"), p_telemetry=extra.get("telemetry"), p_sys=extra.get("sys"), p_location=extra.get("location"))
            for payload in (full, base):
                rq = urllib.request.Request(INGEST, json.dumps(payload).encode(), {"Content-Type": "application/json", "apikey": APIKEY, "Authorization": "Bearer " + APIKEY})
                try:
                    return json.loads(urllib.request.urlopen(rq, timeout=15).read() or b"{}").get("ok") is True
                except urllib.error.HTTPError as e:
                    if payload is full and e.code in (400, 404): continue      # backend not upgraded yet: fall back to the basic heartbeat
                    return False
            return False
        body = json.dumps(dict(host=host, ip=local_ip(), os_user=osu, state=st, idle=idle, token=tok, events=evs)).encode()   # legacy Flask server
        rq = urllib.request.Request(SERVER + "/api/agent/heartbeat", body, {"Content-Type": "application/json", "X-Token": AGENT_TOKEN})
        urllib.request.urlopen(rq, timeout=10).read(); return True
    except Exception:
        return False

HOST, OSU = socket.gethostname(), getpass.getuser()

def _session_ending():
    """True when the OS session / machine is going down (a real logoff), False for a plain service restart."""
    if OSN != "Linux" or not shutil.which("systemctl"): return True
    if "stopping" in (sh(["systemctl", "is-system-running"], 2), sh(["systemctl", "--user", "is-system-running"], 2)): return True
    return sh(["systemctl", "--user", "is-system-running"], 2) not in ("running", "degraded", "starting", "initializing", "maintenance")

def goodbye(*_):
    """Graceful stop (user logs off, machine shuts down, Ctrl+C): tell the server, so history shows a clean logoff."""
    try:
        if _session_ending():
            st, idle = state_idle()
            with L: evs, tok = list(S["events"]) + [{"type": "logoff", "ts": int(time.time())}], S["token"]
            send_beat(HOST, OSU, st, idle, evs, tok)
    finally:
        os._exit(0)

def beat_loop():
    host, osu, last = HOST, OSU, None
    if time.time() - load_state().get("last_beat", 0) > 180: event("login")      # a restart / self-update is not a new sign-in
    while True:
        st, idle = state_idle()
        if last and st != last: event("lock" if st == "locked" else "unlock")
        last = st
        extra = {"app": foreground_app() if st == "active" else None, "telemetry": telemetry()}
        with L:
            evs, tok = list(S["events"]), S["token"]
            if S["sys_new"]: extra["sys"] = S["sys"]
            if S["loc_new"]: extra["location"] = S["loc"]
        if send_beat(host, osu, st, idle, evs, tok, extra):
            with L:
                del S["events"][:len(evs)]
                if "sys" in extra: S["sys_new"] = False
                if "location" in extra: S["loc_new"] = False
            save_state(last_beat=int(time.time()))
        else:
            with L: del S["events"][:-200]       # keep memory bounded while the server is unreachable
        time.sleep(INTERVAL)


# ------------------------------------------------------------------ inventory refresh + self-update
def check_update():
    """Download the newest agent from the organization's site; install it only if it is newer, compiles and passes its own self-test."""
    if not SERVER.startswith("http") or "localhost" in SERVER or "127.0.0.1" in SERVER: return False
    try:
        raw = http_get(SERVER + "/agent/trackobee_agent.py", 30)
        txt = raw.decode("utf-8")
        m = re.search(r'^VERSION = "([0-9.]+)"', txt, re.M)
        if not m or _vt(m.group(1)) <= _vt(VERSION) or "def run(" not in txt: return False
        compile(txt, "trackobee_agent_update", "exec")
        new = UPDATE_PATH + ".new"
        with open(new, "wb") as f: f.write(raw)
        if not FROZEN:
            r = subprocess.run([sys.executable, new, "--selftest"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
            if r.returncode != 0 or b"ok" not in r.stdout: os.remove(new); return False
        os.replace(new, UPDATE_PATH)
        return True
    except Exception:
        return False

def maint_loop():
    """Inventory + location now and hourly; update check now (after a minute) and every few hours."""
    next_update = time.time() + 60
    while True:
        try:
            _import_user_env(); kwin_setup()
            inv = collect_sys(); loc = fetch_location()
            with L:
                S["sys"], S["sys_new"] = inv, True
                if loc: S["loc"], S["loc_new"] = loc, True
        except Exception: pass
        end = time.time() + INVENTORY_EVERY
        while time.time() < end:
            time.sleep(30)
            if time.time() >= next_update:
                next_update = time.time() + UPDATE_EVERY
                if check_update():
                    S["restart"] = True
                    try: SRV.shutdown()
                    except Exception: pass
                    return


# ------------------------------------------------------------------ local pairing server (website <-> this PC)
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
        if self.path == "/status": self._send(200, dict(agent="trackobee", version=VERSION, paired=bool(S["token"]), last_beat=load_state().get("last_beat")))
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


# ------------------------------------------------------------------ Windows self-install (the downloaded trackobee-setup.exe installs itself, no admin needed)
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
    subprocess.Popen([WIN_EXE, "--run"], creationflags=0x00000008 | 0x00000200 | 0x08000000, close_fds=True)  # DETACHED | NEW_GROUP | NO_WINDOW
    msgbox("Trackobee is installed and running.")

def win_uninstall():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k: winreg.DeleteValue(k, "Trackobee")
    except OSError: pass
    subprocess.run(["taskkill", "/F", "/IM", "trackobee-agent.exe"], capture_output=True)
    shutil.rmtree(HOME_DIR, ignore_errors=True)
    msgbox("Trackobee was removed. Delete %s to finish." % WIN_DIR)


# ------------------------------------------------------------------ run / restart
def _restart():
    """Re-launch ourselves (after a self-update). The new process retries the port while this one finishes exiting."""
    env = dict(os.environ, TB_RESTARTED="1")
    args = [sys.executable] + ([] if FROZEN else [SELF]) + sys.argv[1:]
    if OSN == "Windows":
        subprocess.Popen(args, env=env, creationflags=0x00000008 | 0x00000200 | 0x08000000, close_fds=True); os._exit(0)
    os.execve(sys.executable, args, env)

def run():
    global SRV
    srv = None
    for _ in range(12 if os.environ.get("TB_RESTARTED") else 1):
        try: srv = ThreadingHTTPServer(("127.0.0.1", PORT), H); break
        except OSError: time.sleep(1)
    if srv is None: return          # already running for this machine
    SRV = srv
    load(); S["user"] = token_user(S["token"]) if S["token"] else None
    threading.Thread(target=beat_loop, daemon=True).start()
    threading.Thread(target=maint_loop, daemon=True).start()
    for sig in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):      # handlers must be installed from the main thread
        try: signal.signal(getattr(signal, sig), goodbye)
        except Exception: pass
    srv.serve_forever()
    srv.server_close()
    if S["restart"]: _restart()

def _frozen_loader():
    """Frozen binary: if a newer agent_update.py was self-downloaded, run that code instead of the bundled copy."""
    if not FROZEN or os.environ.get("TB_LOADED"): return False
    try:
        with open(UPDATE_PATH, encoding="utf-8") as f: txt = f.read()
        m = re.search(r'^VERSION = "([0-9.]+)"', txt, re.M)
        if not m or _vt(m.group(1)) <= _vt(VERSION): return False
        code = compile(txt, UPDATE_PATH, "exec")
    except Exception:
        return False
    os.environ["TB_LOADED"] = "1"
    exec(code, {"__name__": "__main__", "__file__": UPDATE_PATH})
    return True

if __name__ == "__main__":
    a = sys.argv[1:]
    if "--selftest" in a: print("ok"); sys.exit(0)
    if _frozen_loader(): sys.exit(0)
    if OSN == "Windows" and FROZEN:
        if "--uninstall" in a: win_uninstall()
        elif "--run" in a or os.environ.get("TB_LOADED") or os.path.normcase(os.path.dirname(sys.executable)) == os.path.normcase(WIN_DIR): run()
        else: win_install()
    else:
        run()
