#!/bin/bash
# Build Video Kintsugi into ~/Applications, with the Finder Quick Action "Video Kintsugi".
#   ./make-app.sh            the Mac app: a native window around the page (mac/VideoKintsugi.swift),
#                            with the server and page copied inside the bundle
#   ./make-app.sh --browser  "Video Kintsugi (Browser).app": starts the server from this folder
#                            and opens the page in your web browser
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$DIR/VERSION")"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

if [ "$1" = "--browser" ]; then
  APP="$HOME/Applications/Video Kintsugi (Browser).app"
  rm -rf "$APP"
  /usr/bin/osacompile -o "$APP" -e "do shell script quoted form of \"$DIR/launch.sh\" & \" >/dev/null 2>&1\""
  cp "$DIR/branding/VideoKintsugi.icns" "$APP/Contents/Resources/applet.icns"
  rm -f "$APP/Contents/Resources/Assets.car"
  /usr/bin/plutil -remove CFBundleIconName "$APP/Contents/Info.plist" 2>/dev/null || true
else
  if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "Building the app needs Apple's command line tools: run  xcode-select --install  and try again."
    exit 1
  fi
  APP="$HOME/Applications/Video Kintsugi.app"
  BUILD="$(mktemp -d)"
  xcrun swiftc -swift-version 5 -O -o "$BUILD/Video Kintsugi" "$DIR/mac/VideoKintsugi.swift" -framework Cocoa -framework WebKit
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/web/branding"
  mv "$BUILD/Video Kintsugi" "$APP/Contents/MacOS/"
  rm -rf "$BUILD"
  sed "s/@VERSION@/$VERSION/g" "$DIR/mac/Info.plist" > "$APP/Contents/Info.plist"
  cp "$DIR/branding/VideoKintsugi.icns" "$APP/Contents/Resources/"
  cp "$DIR"/{kintsugi.py,index.html,app.js,desk.js,VERSION} "$APP/Contents/Resources/web/"
  cp "$DIR/branding/mark.svg" "$APP/Contents/Resources/web/branding/"
fi
/usr/bin/codesign --force --sign - "$APP" 2>/dev/null   # ad hoc, so macOS will run what was built here
touch "$APP"
"$LSREGISTER" -f "$APP" 2>/dev/null || true
echo "Built $APP"

# the Quick Action opens the Mac app when it is built, otherwise the browser version
NATIVE_APP="$HOME/Applications/Video Kintsugi.app"
if [ -x "$NATIVE_APP/Contents/MacOS/Video Kintsugi" ]; then
  QUICK="open -a \"$NATIVE_APP\" \"\$@\""
else
  QUICK="\"$DIR/launch.sh\" \"\$@\""
fi
WF="$HOME/Library/Services/Video Kintsugi.workflow"
rm -rf "$WF"
mkdir -p "$WF/Contents"
QUICK_XML="$(printf '%s' "$QUICK" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/[|]/\\|/g')"
sed "s|@COMMAND@|$QUICK_XML|" "$DIR/quick-action/document.wflow" > "$WF/Contents/document.wflow"
cp "$DIR/quick-action/Info.plist" "$WF/Contents/Info.plist"
/System/Library/CoreServices/pbs -update 2>/dev/null || true
echo "Built $WF"
