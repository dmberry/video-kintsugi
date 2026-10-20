#!/bin/bash
# Start the Video Kintsugi server if it isn't already running, hand it any files given as arguments
# (from the Finder Quick Action), then open the page unless one is already open.
PORT="${KINTSUGI_PORT:-8767}"
URL="http://127.0.0.1:$PORT/"
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/Library/Logs/Video Kintsugi.log"
PY=/opt/homebrew/bin/python3
[ -x "$PY" ] || PY=/usr/bin/python3

if ! /usr/bin/curl -fs -o /dev/null "${URL}api/status"; then
  nohup "$PY" "$DIR/kintsugi.py" </dev/null >>"$LOG" 2>&1 &
  for _ in $(seq 1 50); do
    /usr/bin/curl -fs -o /dev/null "${URL}api/status" && break
    sleep 0.1
  done
  if ! /usr/bin/curl -fs -o /dev/null "${URL}api/status"; then
    # usually no working python3: on a new Mac, /usr/bin/python3 needs Apple's command line tools
    /usr/bin/osascript -e 'display alert "Video Kintsugi couldn’t start" message "It needs Python 3. If macOS offered to install the command line developer tools, install them and open Video Kintsugi again; otherwise run “xcode-select --install” in Terminal. Details are in ~/Library/Logs/Video Kintsugi.log." as critical' >/dev/null 2>&1
    exit 1
  fi
fi
if [ $# -gt 0 ]; then
  "$PY" -c 'import json, sys; print(json.dumps({"paths": sys.argv[1:]}))' "$@" |
    /usr/bin/curl -fs -o /dev/null -X POST -H "Content-Type: application/json" -H "X-Kintsugi: 1" \
      --data-binary @- "${URL}api/queue"
  # a page that polled in the last few seconds picks the files up itself
  seen=$(/usr/bin/curl -fs "${URL}api/status" | "$PY" -c 'import json, sys, time; print(int(time.time() - json.load(sys.stdin)["page_seen"] < 5))')
  [ "$seen" = 1 ] && exit 0
fi
[ -n "$KINTSUGI_NO_OPEN" ] || /usr/bin/open "$URL"
