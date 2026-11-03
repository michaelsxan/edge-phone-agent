#!/bin/sh
until [ "$(grep -c "safe chain finished" maps-run.log)" -ge 3 ]; do sleep 20; done
uv run python explore.py com.zhiliaoapp.musically --from "path:Profile>Profile menu>TikTok Studio" --depth 2 --minutes 25
echo "$(date +%H:%M:%S) studio chain finished"
