#!/bin/sh
# Run on macOS. Produces a GENERIC agent (no secrets): dist/trackobee-agent-macos and an optional MDM pkg dist/trackobee.pkg.
# The pkg installs /Library/Trackobee/trackobee-agent + a LaunchAgent for all users; push the organization's settings with
# MDM as /Library/Trackobee/org.json  ({"server":..., "agent_token":..., "allowed_origins":...}).
# Optional signing: SIGN_APP="Developer ID Application: ..." SIGN_PKG="Developer ID Installer: ..."
# UNIVERSAL_BIN=/path/to/lipo-merged-binary skips the PyInstaller step (CI).
set -e
cd "$(dirname "$0")"; mkdir -p dist
if [ -z "$UNIVERSAL_BIN" ]; then
  python3 -m pip install --quiet pyinstaller
  python3 -m PyInstaller --onefile --clean --name trackobee-agent ../agent/trackobee_agent.py
  BIN=dist/trackobee-agent
else BIN="$UNIVERSAL_BIN"; fi
[ -n "$SIGN_APP" ] && codesign --force --options runtime --timestamp -s "$SIGN_APP" "$BIN"
cp "$BIN" dist/trackobee-agent-macos
rm -rf root scripts && mkdir -p root/Library/Trackobee root/Library/LaunchAgents scripts
cp "$BIN" root/Library/Trackobee/trackobee-agent && chmod 755 root/Library/Trackobee/trackobee-agent
cp macos/global.trackobee.agent.plist root/Library/LaunchAgents/ && chmod 644 root/Library/LaunchAgents/*.plist
cp macos/postinstall scripts/postinstall && chmod 755 scripts/postinstall
[ -n "$SIGN_PKG" ] && S="--sign $SIGN_PKG"
pkgbuild --root root --scripts scripts --identifier global.trackobee.agent --version 2.0 --install-location / $S dist/trackobee.pkg
echo "Built: $(pwd)/dist/trackobee-agent-macos and dist/trackobee.pkg"
