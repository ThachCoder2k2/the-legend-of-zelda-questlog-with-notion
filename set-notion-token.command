#!/bin/bash
# Saves the Notion integration token for Quest Log in the login Keychain (opened by the menu-bar button's
# "Notion not connected" item). The token is typed at the prompt, so it never lands in shell history or on screen.
cd "$(dirname "$0")" || exit 1
echo "Quest Log: save your Notion integration token to the login Keychain."
echo
echo "You need an internal Notion integration with Read, Update AND Insert content, added to the page"
echo "'Quest Log Data' (… menu > Connections). Steps: README.md, \"Saving done to Notion\"."
echo
echo "Paste the token (nothing is shown), press Return, then paste it once more to confirm."
if security add-generic-password -U -s "QuestLog Notion" -a token -w; then
  size=$(security find-generic-password -s "QuestLog Notion" -a token -w 2>/dev/null | tr -d '\n' | wc -c | tr -d ' ')
  if [ "${size:-0}" -lt 20 ]; then
    echo
    echo "That looks empty or too short (${size:-0} characters): nothing usable was pasted. Run this again and paste the whole token."
  else
    echo
    echo "Saved. Checking it against Notion..."
    echo
    /usr/bin/python3 report.py check
    /usr/bin/python3 report.py refresh >/dev/null 2>&1
  fi
else
  echo
  echo "Not saved (cancelled, or the two entries differed). Run this again to retry."
fi
echo
read -n 1 -s -r -p "Press any key to close."
echo
