// Builds the per-organization endpoint installer scripts in the browser (no server involved).
// cfg = { siteUrl, ingestUrl, anonKey, agentToken, allowedOrigins };  packaged = { windows, mac, linux } booleans (prebuilt binaries available)
export function makeInstaller(os, cfg, packaged = {}) {
  const site = cfg.siteUrl.replace(/\/$/, "");
  const org = JSON.stringify({ server: site, ingest: cfg.ingestUrl, apikey: cfg.anonKey, agent_token: cfg.agentToken, allowed_origins: cfg.allowedOrigins || "" });
  if (os === "windows") {
    const body = packaged.windows
      ? String.raw`powershell -NoProfile -Command "Invoke-WebRequest '${site}/installers/trackobee-setup.exe' -OutFile '%TEMP%\trackobee-setup.exe'" || (echo Download failed & pause & exit /b 1)
"%TEMP%\trackobee-setup.exe"
`
      : String.raw`where python >nul 2>&1 || (echo Python 3 is required: https://python.org & pause & exit /b 1)
set A=%LOCALAPPDATA%\Trackobee
mkdir "%A%" 2>nul
powershell -NoProfile -Command "Invoke-WebRequest '${site}/agent/trackobee_agent.py' -OutFile '%A%\agent.py'" || (echo Download failed & pause & exit /b 1)
> "%A%\run.bat" echo @echo off
>> "%A%\run.bat" echo start "" pythonw "%A%\agent.py"
schtasks /Create /TN Trackobee /TR "\"%A%\run.bat\"" /SC ONLOGON /F >nul
call "%A%\run.bat"
echo Trackobee installed. Refresh the website.
pause
`;
    return { name: "trackobee-setup.bat", body: "@echo off\r\nsetlocal\r\nset D=%USERPROFILE%\\.trackobee\r\nmkdir \"%D%\" 2>nul\r\n> \"%D%\\org.json\" echo " + org + "\r\n" + body.replace(/\r?\n/g, "\r\n") };
  }
  if (os === "linux") {
    const bin = !!packaged.linux;
    const get = bin ? `GET "${site}/installers/trackobee-agent-linux" "$D/agent" && chmod +x "$D/agent"` : `GET "${site}/agent/trackobee_agent.py" "$D/agent.py"`;
    const ex = bin ? `"$D/agent"` : `"$(command -v python3) $D/agent.py"`;
    const needPy = bin ? "" : `command -v python3 >/dev/null 2>&1 || fail "python3 is required:  sudo apt install python3"`;
    return { name: "trackobee-setup.sh", body: String.raw`#!/bin/sh
# Trackobee agent installer (Linux). Run:  sh trackobee-setup.sh
SERVER="${site}"
say() { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
D="$HOME/.trackobee"; mkdir -p "$D" || fail "cannot create $D"
umask 077
cat > "$D/org.json" <<'CFG'
${org}
CFG
say "[1/4] checking requirements"
${needPy}
command -v python3 >/dev/null 2>&1 || fail "python3 is required (used to verify the install):  sudo apt install python3"
if command -v curl >/dev/null 2>&1; then GET() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then GET() { wget -q "$1" -O "$2"; }
else fail "curl or wget is required:  sudo apt install curl"; fi
say "[2/4] downloading the agent from $SERVER"
${get} || fail "cannot download from $SERVER . This computer must be able to open that address in a browser."
say "[3/4] starting the agent (and at every login)"
EXEC=${ex}
pkill -f "$D/agent" 2>/dev/null; sleep 1
MODE=""
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/trackobee.service" <<UNIT
[Unit]
Description=Trackobee agent
[Service]
Environment=PYTHONUNBUFFERED=1
ExecStart=$EXEC
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload && systemctl --user enable trackobee.service >/dev/null 2>&1 && systemctl --user restart trackobee.service && MODE="systemd service"
fi
if [ -z "$MODE" ]; then      # no systemd user session (minimal or remote login): desktop autostart + start now
  mkdir -p "$HOME/.config/autostart"
  printf '[Desktop Entry]\nType=Application\nName=Trackobee\nExec=%s\nX-GNOME-Autostart-enabled=true\n' "$EXEC" > "$HOME/.config/autostart/trackobee.desktop"
  nohup $EXEC >"$D/agent.log" 2>&1 &
  MODE="desktop autostart"
fi
say "[4/4] verifying"
OK=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  if python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:47821/status',timeout=2)" 2>/dev/null; then OK=1; break; fi
  sleep 1
done
if [ -n "$OK" ]; then say "OK: Trackobee agent is running ($MODE). Now refresh the website."
else say "The agent did not start. Details:"; journalctl --user -u trackobee -n 15 --no-pager 2>/dev/null; tail -n 15 "$D/agent.log" 2>/dev/null; exit 1; fi
` };
  }
  // macOS
  const bin = !!packaged.mac;
  const get = bin ? `curl -fsSL "${site}/installers/trackobee-agent-macos" -o "$D/agent" && chmod +x "$D/agent"` : `curl -fsSL "${site}/agent/trackobee_agent.py" -o "$D/agent.py"`;
  const args = bin ? "<string>$D/agent</string>" : "<string>/usr/bin/python3</string><string>$D/agent.py</string>";
  return { name: "trackobee-setup.command", body: String.raw`#!/bin/sh
set -e
D="$HOME/.trackobee"; mkdir -p "$D"; umask 077
cat > "$D/org.json" <<'CFG'
${org}
CFG
${get}
P="$HOME/Library/LaunchAgents/global.trackobee.agent.plist"; mkdir -p "$HOME/Library/LaunchAgents"
cat > "$P" <<PLIST
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>Label</key><string>global.trackobee.agent</string>
<key>ProgramArguments</key><array>${args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>
PLIST
launchctl unload "$P" 2>/dev/null || true; launchctl load "$P"
echo "Trackobee installed. Refresh the website."
` };
}

export function saveFile(name, body) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([body], { type: "application/octet-stream" }));
  a.download = name; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
