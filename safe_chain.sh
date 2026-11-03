#!/bin/sh
run() { uv run python explore.py "$1" --from "$2" --depth "$3" --minutes "$4"; }
run com.zhiliaoapp.musically "path:Profile>Profile menu" 2 30
run com.instagram.android "path:Profile>Options" 2 30
echo "$(date +%H:%M:%S) safe chain finished"
