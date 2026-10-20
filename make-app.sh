#!/bin/bash
# Build ~/Applications/Video Kintsugi.app (a double-click launcher for launch.sh) and the Finder
# Quick Action "Video Kintsugi", which opens the selected files in it.
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Video Kintsugi.app"
rm -rf "$APP"
/usr/bin/osacompile -o "$APP" -e "do shell script quoted form of \"$DIR/launch.sh\" & \" >/dev/null 2>&1\""
ICON="$DIR/branding/VideoKintsugi.icns"
if [ -f "$ICON" ]; then
  cp "$ICON" "$APP/Contents/Resources/applet.icns"
  rm -f "$APP/Contents/Resources/Assets.car"
  /usr/bin/plutil -remove CFBundleIconName "$APP/Contents/Info.plist" 2>/dev/null
fi
/usr/bin/codesign --force --sign - "$APP" 2>/dev/null   # re-sign after editing the bundle
touch "$APP"
echo "Built $APP"

WF="$HOME/Library/Services/Video Kintsugi.workflow"
rm -rf "$WF"
mkdir -p "$WF/Contents"
sed "s|@LAUNCH@|$DIR/launch.sh|" "$DIR/quick-action/document.wflow" > "$WF/Contents/document.wflow"
cp "$DIR/quick-action/Info.plist" "$WF/Contents/Info.plist"
/System/Library/CoreServices/pbs -update 2>/dev/null
echo "Built $WF"
