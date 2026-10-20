#!/bin/bash
# Runs the Quest Log test suite in a throw-away sandbox: a temp copy of the scripts (so runs/, pending.json and the like
# never touch the real ones), a fake `questcard` that records the cards it is given instead of opening a window, and a
# fake `osascript`. Nothing is sent to Notion or ntfy.sh (the tests stub the network). Usage: tests/run.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SB="$(mktemp -d /tmp/questlog-test.XXXXXX)"
trap 'rm -rf "$SB"' EXIT
cp "$HERE"/*.py "$HERE/card.html" "$SB/"
cp "$HERE/tests/fake_questcard.py" "$SB/questcard"
chmod +x "$SB/questcard"
mkdir -p "$SB/fakebin" "$SB/emptybin"
printf '#!/bin/sh\necho "$@" >> "%s/osascript.log"\n' "$SB" > "$SB/fakebin/osascript"
chmod +x "$SB/fakebin/osascript"
QL_SANDBOX="$SB" /usr/bin/python3 -I "$HERE/tests/test_questlog.py"
