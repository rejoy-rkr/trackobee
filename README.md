# Trackobee
Embedded CRM plugin + endpoint agent + live dashboard.

## Run the server
    TB_AGENT_TOKEN=<random> TB_CRM_SECRET=<random> TB_DASH_PASS=<pass> \
    TB_PUBLIC_URL=https://trackobee.yourco.com TB_ALLOWED_ORIGINS=https://customercentral.co \
    TB_TRUST_PROXY=1 python3 app.py        # behind HTTPS reverse proxy; PORT=8090 default

## Embed in customercentral.co (only on pages for logged-in users)
The CRM backend signs a short-lived token for the logged-in user using the same TB_CRM_SECRET:

    # Python
    import base64,hmac,hashlib,time
    def tb_token(user, ttl=86400):
        p=base64.urlsafe_b64encode(f"{user}:{int(time.time())+ttl}".encode()).decode().rstrip("=")
        return p+"."+hmac.new(SECRET,p.encode(),hashlib.sha256).hexdigest()
    // PHP
    function tb_token($u,$s,$ttl=86400){$p=rtrim(strtr(base64_encode("$u:".(time()+$ttl)),'+/','-_'),'=');
      return $p.'.'.hash_hmac('sha256',$p,$s);}

    <script src="https://trackobee.yourco.com/embed.js" data-token="{{ tb_token(user) }}" async></script>
    <!-- on CRM logout: Trackobee.unpair() -->

Flow: page loads → detects OS → no agent? shows "Install for Windows/macOS/Linux" banner
(installer download needs the signed token) → agent runs on 127.0.0.1:47821 → page hands it the token
→ agent heartbeats every 15s with the verified CRM user → dashboard shows user, machine, IP, facility, availability.

## Facilities
Dashboard → Facilities: map public IP / CIDR (and optionally LAN subnets) to a site name.

## Notes
- Installers need Python 3 on the PC (macOS/Linux have it; Windows: install python.org). For no-dependency
  installs, package agent with PyInstaller into .exe/.pkg and sign it.
- Chrome may ask the user to allow "local network access" for the CRM site once.
- Collected: host, LAN/public IP, OS user, lock/idle state, CRM user. No screenshots, keystrokes, or content.
  Inform employees and get consent per your local law.

## Packaged installers (no Python needed on PCs)
`.exe` must be built on Windows and `.pkg` on macOS (PyInstaller can't cross-compile).
- **CI (recommended):** add repo secrets `TB_SERVER`, `TB_AGENT_TOKEN`, `TB_ALLOWED_ORIGINS` (optional `SIGN_APP`, `SIGN_PKG`),
  run the "Build Trackobee agent" workflow, download the `windows` and `macos` artifacts.
- **Manual:** `packaging\build_windows.bat` (Windows) / `sh packaging/build_macos.sh` (macOS) with the same env vars set.
- Put `trackobee-setup.exe` and `trackobee.pkg` in `./installers/` (or `TB_INSTALLERS`). `/download/windows|mac` then serves
  them automatically; Linux (and any OS without a built file) falls back to the script installer.
- Windows: the exe self-installs per-user (no admin), runs at logon via the HKCU Run key. Remove: `trackobee-agent.exe --uninstall`.
- macOS: pkg installs `/Library/Trackobee/trackobee-agent` + a LaunchAgent for every user. Remove: `sudo sh packaging/macos/uninstall.sh`.
- Unsigned builds trigger SmartScreen (Windows) / Gatekeeper (macOS) warnings: sign the exe, and sign + notarize the pkg.
- The server URL and agent token are baked into the binary (so the token is extractable): rotate `TB_AGENT_TOKEN` and rebuild if leaked.
