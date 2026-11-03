"""Turn maps/<package>/graph.json into data + thumbnails for the screen-map viewer page.

Usage: python build_viewer.py OUT_DIR
Writes OUT_DIR/data/<app>.json and OUT_DIR/thumbs/<app>/<screen>.jpg
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

APPS = {"tiktok": "com.zhiliaoapp.musically", "instagram": "com.instagram.android"}
MAPS = Path(__file__).parent / "maps"


def build(app: str, pkg: str, out: Path) -> None:
    src = MAPS / pkg
    if not (src / "graph.json").exists():
        return
    g = json.loads((src / "graph.json").read_text())
    out_edges: dict[str, list[dict]] = defaultdict(list)
    in_count: dict[str, int] = defaultdict(int)
    seen = set()
    for e in g["edges"]:
        k = (e["from"], e["action"], e["to"])
        if k in seen:
            continue
        seen.add(k)
        out_edges[e["from"]].append({"action": e["action"], "to": e["to"], "keyboard": e.get("keyboard", False)})
        in_count[e["to"]] += 1

    def clean(label: str) -> str:
        """Turn a tapped label into a readable breadcrumb step."""
        label = label.replace("\n", " ").strip()
        if re.fullmatch(r"[a-z0-9_]{1,8}|@\d+", label) or "_" in label and " " not in label:
            return "(icon)"
        label = re.sub(r"\s*\d[\d.,]*[KMB]?\s*(comments?|likes?|shares?)\b", "", label)
        label = re.sub(r"^(Read or add comments\.?).*", "Comments", label)
        label = re.sub(r"'s story, \d+ of \d+.*", "'s story", label)
        label = re.sub(r"^Unselected (Photo|Video) thumbnail.*", r"Pick a \1", label)
        return label[:30] + ("…" if len(label) > 30 else "")

    # Tree = the path each screen was first discovered through.
    screens = {}
    for sid, s in g["screens"].items():
        crumb = [clean(a["label"]) for a in s["path"]]
        name = " › ".join(crumb) if crumb else "Launch screen"
        screens[sid] = {"id": sid, "title": name, "raw_title": s["title"], "activity": s["activity"].split(".")[-1],
                        "package": s["package"], "path": [a["label"] for a in s["path"]],
                        "depth": len(s["path"]), "labels": s["labels"][:14],
                        "out": out_edges.get(sid, []), "inbound": in_count.get(sid, 0),
                        "unreachable": s["unreachable"], "parent": None}
    by_path = {tuple(s["path"]): sid for sid, s in screens.items()}
    for sid, s in screens.items():
        if s["path"]:
            s["parent"] = by_path.get(tuple(s["path"][:-1]))
            if s["parent"] is None:  # parent merged into another screen: find who links here first
                s["parent"] = next((e["from"] for e in g["edges"] if e["to"] == sid), None)

    thumbs = out / "thumbs" / app
    thumbs.mkdir(parents=True, exist_ok=True)
    for sid in screens:
        png = src / "screens" / f"{sid}.png"
        jpg = thumbs / f"{sid}.jpg"
        if png.exists() and not jpg.exists():
            subprocess.run(["sips", "-Z", "480", "-s", "format", "jpeg", "-s", "formatOptions", "60",
                            str(png), "--out", str(jpg)], capture_output=True, check=False)
        screens[sid]["thumb"] = f"thumbs/{app}/{sid}.jpg" if jpg.exists() else None

    (out / "data").mkdir(parents=True, exist_ok=True)
    data = {"app": app, "package": pkg, "screens": screens,
            "edges": sum(len(v) for v in out_edges.values())}
    (out / "data" / f"{app}.json").write_text(json.dumps(data))
    print(f"{app}: {len(screens)} screens, {data['edges']} links")


if __name__ == "__main__":
    dest = Path(sys.argv[1])
    for app, pkg in APPS.items():
        build(app, pkg, dest)
