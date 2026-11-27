// Builds the per-organization endpoint installer scripts in the browser (no server involved).
// cfg = { siteUrl, ingestUrl, anonKey, agentToken, allowedOrigins };  packaged = { windows, mac, linux } booleans (prebuilt binaries available)
//
// These scripts are deliberately thin and stable: they only (1) write the organization's org.json, (2) download the agent,
// (3) make it start at every login and (4) prove the first report reached the server. Everything else (new data fields, bug fixes,
// features) ships through the agent itself, which updates itself from the organization's site, so installed PCs never need a new installer.
export function makeInstaller(os, cfg, packaged = {}, opts = null) {
  const site = cfg.siteUrl.replace(/\/$/, "");
  const org = JSON.stringify({ server: site, ingest: cfg.ingestUrl, apikey: cfg.anonKey, agent_token: cfg.agentToken, allowed_origins: cfg.allowedOrigins || "", ...(opts ? { browser_ext: true } : {}) });
  const X = (opts && opts.ext) || {};                                   // browser extension build: { id, update_url } from <site>/extension/info.json
  const xid = X.id || "", xurl = X.update_url || (site + "/extension/update.xml");
  const sfx = opts ? "-with-extension" : "";
  if (os === "windows") {
    const body = packaged.windows
      ? String.raw`set U=${site}/installers/trackobee-setup.exe
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; for($i=0;$i -lt 4;$i++){ try{ Invoke-WebRequest -UseBasicParsing '%U%' -OutFile '%TEMP%\trackobee-setup.exe'; exit 0 }catch{ Start-Sleep 3 } }; exit 1" || (echo Download failed. This computer must be able to open %U% in a browser. & pause & exit /b 1)
"%TEMP%\trackobee-setup.exe"
`
      : String.raw`where python >nul 2>&1 || (echo Python 3 is required: https://python.org & pause & exit /b 1)
set A=%LOCALAPPDATA%\Trackobee
mkdir "%A%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; for($i=0;$i -lt 4;$i++){ try{ Invoke-WebRequest -UseBasicParsing '${site}/agent/trackobee_agent.py' -OutFile '%A%\agent.py'; exit 0 }catch{ Start-Sleep 3 } }; exit 1" || (echo Download failed & pause & exit /b 1)
python -m py_compile "%A%\agent.py" || (echo The downloaded agent is damaged & pause & exit /b 1)
taskkill /F /IM pythonw.exe >nul 2>&1
> "%A%\run.bat" echo @echo off
>> "%A%\run.bat" echo start "" pythonw "%A%\agent.py"
schtasks /Create /TN Trackobee /TR "\"%A%\run.bat\"" /SC ONLOGON /F >nul
call "%A%\run.bat"
`;
    const verify = String.raw`echo Waiting for the first report to reach the server...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path $env:USERPROFILE '.trackobee\state.json'; for($i=0;$i -lt 45;$i++){ if(Test-Path $p){ try{ $j=Get-Content $p -Raw | ConvertFrom-Json; if($j.last_beat -gt 0){ exit 0 } }catch{} }; Start-Sleep 2 }; exit 1" && (echo OK: Trackobee is installed and reporting.) || (echo WARNING: installed, but no report reached the server yet. Check this computer's internet access, then run this file again.)
pause
`;
    const wext = !opts ? "" : String.raw`
echo Installing the browser extension (real website domains)...
set X=%USERPROFILE%\.trackobee\extension
mkdir "%X%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; foreach($f in 'manifest.json','background.js','icon16.png','icon48.png','icon128.png'){ try{ Invoke-WebRequest -UseBasicParsing ('${site}/extension/'+$f) -OutFile (Join-Path $env:USERPROFILE ('.trackobee\extension'+$f)) }catch{} }"
if not "${xid}"=="" (
  reg add "HKCU\Software\Policies\Google\Chrome\ExtensionInstallForcelist" /v 77 /t REG_SZ /d "${xid};${xurl}" /f >nul
  reg add "HKCU\Software\Policies\Microsoft\Edge\ExtensionInstallForcelist" /v 77 /t REG_SZ /d "${xid};${xurl}" /f >nul
  reg add "HKCU\Software\Policies\BraveSoftware\Brave\ExtensionInstallForcelist" /v 77 /t REG_SZ /d "${xid};${xurl}" /f >nul
  echo Restart Chrome / Edge, then open chrome://extensions to confirm "Trackobee Browser Sites" is there.
  echo If it is not: this PC is not managed, so Chrome ignores outside extensions. Open chrome://extensions, turn on Developer mode, click Load unpacked and choose %X%
) else (
  echo The extension is not published on the server yet. Open chrome://extensions, turn on Developer mode, click Load unpacked and choose %X%
)
`;
    return { name: "trackobee-setup" + sfx + ".bat", body: "@echo off\r\nsetlocal\r\nset D=%USERPROFILE%\\.trackobee\r\nmkdir \"%D%\" 2>nul\r\ndel \"%D%\\state.json\" 2>nul\r\n> \"%D%\\org.json\" echo " + org + "\r\n" + (body + wext + verify).replace(/\r?\n/g, "\r\n") };
  }
  if (os === "linux") {
    const bin = !!packaged.linux;
    const binTry = bin ? `GET "${site}/installers/trackobee-agent-linux" "$D/agent" && chmod +x "$D/agent" && "$D/agent" --selftest 2>/dev/null | grep -q ok && USEBIN=1
if [ -z "$USEBIN" ]; then rm -f "$D/agent"; say "  (the prebuilt agent does not run on this Linux version - using the python version instead)"; fi` : "";
    const get = `USEBIN=""
${binTry}
if [ -z "$USEBIN" ]; then
  command -v python3 >/dev/null 2>&1 || fail "python3 is required:  sudo apt install python3"
  GET "${site}/agent/trackobee_agent.py" "$D/agent.py" || fail "cannot download from $SERVER . This computer must be able to open that address in a browser."
  python3 -m py_compile "$D/agent.py" 2>/dev/null || fail "the downloaded agent is damaged or needs a newer python3 (3.7+)"
fi`;
    const needPy = "";
    const check = "";
    const execline = `if [ -n "$USEBIN" ]; then EXECLINE=$(printf '"%s"' "$D/agent"); else PY=$(command -v python3); EXECLINE=$(printf '"%s" "%s"' "$PY" "$D/agent.py"); fi`;
    const lext = !opts ? "" : String.raw`
say "[+] installing the browser extension (real website domains)"
mkdir -p "$D/extension"
for f in manifest.json background.js icon16.png icon48.png icon128.png; do GET "$SERVER/extension/$f" "$D/extension/$f" || say "  could not download $f"; done
XID="${xid}"
if [ -n "$XID" ]; then
  POL=$(printf '{"ExtensionInstallForcelist":["%s;%s"]}' "$XID" "${xurl}")
  PUT=""
  pol_put() { sudo mkdir -p "$1" && printf '%s
' "$POL" | sudo tee "$1/trackobee.json" >/dev/null && PUT=1; }
  if [ -z "$(sudo -n true 2>&1)" ] || { [ -t 0 ] && sudo -v 2>/dev/null; }; then
    command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1 && pol_put /etc/opt/chrome/policies/managed
    command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 && { pol_put /etc/chromium/policies/managed; [ -d /var/snap/chromium/current ] && pol_put /var/snap/chromium/current/policies/managed; }
    command -v microsoft-edge >/dev/null 2>&1 && pol_put /etc/opt/edge/policies/managed
    command -v brave-browser >/dev/null 2>&1 && pol_put /etc/brave/policies/managed
  fi
  if [ -n "$PUT" ]; then say "  Restart the browser, then open chrome://extensions to confirm 'Trackobee Browser Sites' is installed (chrome://policy shows the rule)."
  else say "  No browser policy could be written (needs sudo, or no supported browser found). Manual: open chrome://extensions, turn on Developer mode, Load unpacked, choose $D/extension"; fi
else say "  The extension is not published on the server yet. Manual: chrome://extensions, Developer mode, Load unpacked, choose $D/extension"; fi
`;
    return { name: "trackobee-setup" + sfx + ".sh", body: String.raw`#!/bin/sh
# Trackobee agent installer (Linux).   Install or repair:  sh trackobee-setup.sh      Remove:  sh trackobee-setup.sh uninstall
SERVER="${site}"
say() { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
D="$HOME/.trackobee"
if [ "$1" = "uninstall" ]; then
  systemctl --user disable --now trackobee.service >/dev/null 2>&1
  rm -f "$HOME/.config/systemd/user/trackobee.service" "$HOME/.config/autostart/trackobee.desktop"
  pkill -f "$D/agent" 2>/dev/null
  rm -rf "$D"; say "Trackobee was removed."; exit 0
fi
T0=$(date +%s)
mkdir -p "$D" || fail "cannot create $D"
umask 077
rm -f "$D/state.json"
cat > "$D/org.json" <<'CFG'
${org}
CFG
say "[1/6] checking requirements"
${needPy}
if command -v curl >/dev/null 2>&1; then DL() { curl -fsSL --connect-timeout 15 --max-time 180 "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then DL() { wget -q -T 30 "$1" -O "$2"; }
else fail "curl or wget is required:  sudo apt install curl"; fi
GET() { n=0; while [ "$n" -lt 4 ]; do DL "$1" "$2.part" && mv "$2.part" "$2" && return 0; n=$((n+1)); sleep 3; done; return 1; }
say "[2/6] downloading the agent from $SERVER"
${get}
say "[3/6] checking the download: ok"
say "[4/6] reading this computer's hardware identity (serial number; optional)"
hw_get() { v=$(cat "/sys/class/dmi/id/$1" 2>/dev/null); [ -z "$v" ] && v=$(sudo -n cat "/sys/class/dmi/id/$1" 2>/dev/null); printf '%s' "$v" | tr -cd 'A-Za-z0-9._ -'; }
if [ -z "$(cat /sys/class/dmi/id/product_serial 2>/dev/null)" ] && [ -t 0 ] && command -v sudo >/dev/null 2>&1; then
  say "  sudo is needed once to read the serial number (press Ctrl+C to skip, everything else works without it)"
  sudo -v 2>/dev/null
fi
printf '{"serial_number":"%s","board_serial":"%s","chassis_serial":"%s","uuid":"%s"}\n' "$(hw_get product_serial)" "$(hw_get board_serial)" "$(hw_get chassis_serial)" "$(hw_get product_uuid)" > "$D/hw.json"
say "[5/6] starting the agent (and at every login)"
${execline}
${lext}
pkill -f "$D/agent" 2>/dev/null; sleep 1
MODE=""
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/trackobee.service" <<UNIT
[Unit]
Description=Trackobee agent
[Service]
Environment=PYTHONUNBUFFERED=1
ExecStart=$EXECLINE
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload && systemctl --user enable trackobee.service >/dev/null 2>&1 && systemctl --user restart trackobee.service && MODE="systemd service"
fi
if [ -z "$MODE" ]; then      # no systemd user session (minimal or remote login): desktop autostart + start now
  mkdir -p "$HOME/.config/autostart"
  printf '[Desktop Entry]\nType=Application\nName=Trackobee\nExec=%s\nX-GNOME-Autostart-enabled=true\n' "$EXECLINE" > "$HOME/.config/autostart/trackobee.desktop"
  nohup sh -c "exec $EXECLINE" >"$D/agent.log" 2>&1 &
  MODE="desktop autostart"
fi
say "[6/6] waiting for the first report to reach the server"
OK=""; n=0
while [ "$n" -lt 45 ]; do
  LB=$(sed -n 's/.*"last_beat": *\([0-9][0-9]*\).*/\1/p' "$D/state.json" 2>/dev/null)
  if [ -n "$LB" ] && [ "$LB" -ge "$T0" ]; then OK=1; break; fi
  n=$((n+1)); sleep 2
done
if [ -n "$OK" ]; then say "OK: Trackobee is running ($MODE) and reporting. It keeps itself up to date."
else say "The agent started but no report reached the server. Details:"; journalctl --user -u trackobee -n 15 --no-pager 2>/dev/null; tail -n 15 "$D/agent.log" 2>/dev/null; exit 1; fi
` };
  }
  // macOS
  const bin = !!packaged.mac;
  const get = bin ? `GET "${site}/installers/trackobee-agent-macos" "$D/agent" && chmod +x "$D/agent"` : `GET "${site}/agent/trackobee_agent.py" "$D/agent.py"`;
  const check = bin ? `"$D/agent" --selftest 2>/dev/null | grep -q ok || { echo "ERROR: the downloaded agent does not run on this Mac"; exit 1; }`
                    : `/usr/bin/python3 -m py_compile "$D/agent.py" 2>/dev/null || { echo "ERROR: the downloaded agent is damaged, or Python 3 is missing (run: xcode-select --install)"; exit 1; }`;
  const args = bin ? "<string>$D/agent</string>" : "<string>/usr/bin/python3</string><string>$D/agent.py</string>";
  return { name: "trackobee-setup.command", body: String.raw`#!/bin/sh
# Trackobee agent installer (macOS).   Install or repair: double-click / sh trackobee-setup.command     Remove: sh trackobee-setup.command uninstall
D="$HOME/.trackobee"; P="$HOME/Library/LaunchAgents/global.trackobee.agent.plist"
if [ "$1" = "uninstall" ]; then launchctl unload "$P" 2>/dev/null; rm -f "$P"; pkill -f "$D/agent" 2>/dev/null; rm -rf "$D"; echo "Trackobee was removed."; exit 0; fi
T0=$(date +%s)
mkdir -p "$D" || { echo "ERROR: cannot create $D"; exit 1; }
umask 077
rm -f "$D/state.json"
cat > "$D/org.json" <<'CFG'
${org}
CFG
GET() { n=0; while [ "$n" -lt 4 ]; do curl -fsSL --connect-timeout 15 --max-time 180 "$1" -o "$2.part" && mv "$2.part" "$2" && return 0; n=$((n+1)); sleep 3; done; return 1; }
echo "[1/4] downloading the agent"
${get} || { echo "ERROR: cannot download from ${site}"; exit 1; }
echo "[2/4] checking the download"
${check}
echo "[3/4] starting the agent (and at every login)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$P" <<PLIST
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>Label</key><string>global.trackobee.agent</string>
<key>ProgramArguments</key><array>${args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardErrorPath</key><string>$D/agent.log</string></dict></plist>
PLIST
launchctl unload "$P" 2>/dev/null || true; launchctl load "$P"
echo "[4/4] waiting for the first report to reach the server"
OK=""; n=0
while [ "$n" -lt 45 ]; do
  LB=$(sed -n 's/.*"last_beat": *\([0-9][0-9]*\).*/\1/p' "$D/state.json" 2>/dev/null)
  if [ -n "$LB" ] && [ "$LB" -ge "$T0" ]; then OK=1; break; fi
  n=$((n+1)); sleep 2
done
if [ -n "$OK" ]; then echo "OK: Trackobee is running and reporting. It keeps itself up to date."
else echo "The agent started but no report reached the server. Log:"; tail -n 15 "$D/agent.log" 2>/dev/null; exit 1; fi
` };
}

export function saveFile(name, body) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([body], { type: "application/octet-stream" }));
  a.download = name; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
