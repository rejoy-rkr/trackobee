#!/bin/sh
# Run on macOS (Xcode CLT installed). Env: TB_SERVER TB_AGENT_TOKEN TB_ALLOWED_ORIGINS
# Optional signing: SIGN_APP="Developer ID Application: ..." SIGN_PKG="Developer ID Installer: ..."
#   (notarize afterwards with: xcrun notarytool submit dist/trackobee.pkg --wait ... && xcrun stapler staple ...)
# Optional: UNIVERSAL_BIN=/path/to/lipo-merged-binary skips the PyInstaller step (used by CI).
set -e
cd "$(dirname "$0")"
python3 make_config.py
if [ -z "$UNIVERSAL_BIN" ]; then
  python3 -m pip install --quiet pyinstaller
  python3 -m PyInstaller --onefile --clean --name trackobee-agent --add-data "config.json:." ../agent/trackobee_agent.py
  BIN=dist/trackobee-agent
else BIN="$UNIVERSAL_BIN"; fi
rm -rf root && mkdir -p root/Library/Trackobee root/Library/LaunchAgents scripts pkgout
cp "$BIN" root/Library/Trackobee/trackobee-agent && chmod 755 root/Library/Trackobee/trackobee-agent
[ -n "$SIGN_APP" ] && codesign --force --options runtime --timestamp -s "$SIGN_APP" root/Library/Trackobee/trackobee-agent
cp macos/global.trackobee.agent.plist root/Library/LaunchAgents/ && chmod 644 root/Library/LaunchAgents/*.plist
cp macos/postinstall scripts/postinstall && chmod 755 scripts/postinstall
mkdir -p dist
if [ -n "$SIGN_PKG" ]; then S="--sign $SIGN_PKG"; fi
pkgbuild --root root --scripts scripts --identifier global.trackobee.agent --version 1.1 \
  --install-location / $S dist/trackobee.pkg
echo "Built: $(pwd)/dist/trackobee.pkg"
