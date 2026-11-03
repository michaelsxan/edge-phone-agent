#!/bin/sh
# Build the screen-map page: data + thumbnails, with the data embedded in index.html.
set -e
cd "$(dirname "$0")"
OUT="${1:?usage: publish_viewer.sh OUT_DIR}"
uv run python build_viewer.py viewer
rm -rf "$OUT"; mkdir -p "$OUT"; cp -R viewer/thumbs "$OUT/"
python3 - "$OUT" <<'PY'
import sys, pathlib
out = pathlib.Path(sys.argv[1]); s = pathlib.Path("viewer/index.html").read_text()
blocks = "".join(f'<script type="application/json" id="data-{a}">' + pathlib.Path(f"viewer/data/{a}.json").read_text().replace("</", "<\\/") + "</script>\n" for a in ("tiktok", "instagram"))
(out / "index.html").write_text(s.replace("<script>\nconst state", blocks + "<script>\nconst state", 1))
PY
