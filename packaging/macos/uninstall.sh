#!/bin/sh
# sudo sh uninstall.sh
for u in $(/usr/bin/dscl . -list /Users UniqueID | /usr/bin/awk '$2>=500{print $2}'); do
  /bin/launchctl bootout gui/$u /Library/LaunchAgents/global.trackobee.agent.plist 2>/dev/null; done
rm -f /Library/LaunchAgents/global.trackobee.agent.plist; rm -rf /Library/Trackobee
pkgutil --forget global.trackobee.agent 2>/dev/null; echo removed
