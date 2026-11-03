#!/bin/sh
# Continue mapping over classic adb tcpip (port 5555), which survives Wi-Fi band changes.
SERIAL="192.168.1.150:5555"
until adb connect "$SERIAL" | grep -q connected && adb -s "$SERIAL" get-state 2>/dev/null | grep -q device; do sleep 20; done
uv run python explore.py com.instagram.android --from unexplored --depth 2 --minutes 40
uv run python explore.py com.zhiliaoapp.musically --from s024,s023,s063,s062 --depth 3 --minutes 60
echo "$(date +%H:%M:%S) chain finished"
