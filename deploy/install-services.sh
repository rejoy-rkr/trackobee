#!/bin/sh
# Installs systemd USER services so Trackobee (and the tunnel, if cloudflared is installed + configured) start at boot.
set -e
APPDIR=$(cd "$(dirname "$0")/.." && pwd); U="$HOME/.config/systemd/user"; mkdir -p "$U"
sed "s#@APPDIR@#$APPDIR#g" "$APPDIR/deploy/trackobee.service" > "$U/trackobee.service"
CF=$(command -v cloudflared || true)
if [ -n "$CF" ] && [ -f "$HOME/.cloudflared/config.yml" ]; then
  sed "s#@CLOUDFLARED@#$CF#g" "$APPDIR/deploy/cloudflared.service" > "$U/cloudflared.service"; TUNNEL=cloudflared.service
else echo "NOTE: cloudflared not installed/configured yet - only Trackobee is started. Re-run this script after the tunnel is set up."; TUNNEL=""; fi
systemctl --user daemon-reload
systemctl --user enable --now trackobee.service $TUNNEL
loginctl enable-linger "$USER" 2>/dev/null || echo "NOTE: run 'sudo loginctl enable-linger $USER' so services start at boot without logging in."
systemctl --user --no-pager status trackobee.service | head -5
echo "Logs: journalctl --user -u trackobee -f"
