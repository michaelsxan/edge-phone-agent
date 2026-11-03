"""Upload one video to TikTok on the phone, end to end, checking every screen before tapping.

Steps (see flows/tiktok_upload.md): copy to gallery -> Create -> gallery -> newest video -> Next ->
post screen (visibility, description) -> Post -> confirm it appears on the profile.
Each tap only happens after the expected screen is recognised; anything unexpected aborts
with a screenshot instead of guessing. "Your Story", "Drafts", location chips and Share-to are
never tapped.

Usage:
  python tiktok_upload.py VIDEO.mp4 --caption "text" --visibility only_me [--delete-after]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from phone import SERIAL, Abort, Phone

PKG = "com.zhiliaoapp.musically"
VISIBILITY = {"only_me": "Only you", "friends": "Friends", "everyone": "Everyone"}


def upload(video: Path, caption: str, visibility: str, delete_after: bool) -> None:
    p = Phone(SERIAL)
    print("1. copy video to the gallery", flush=True)
    remote = p.push_video(video)
    try:
        _upload(p, remote, video, caption, visibility, delete_after)
    finally:  # never leave copies behind, even when the post failed
        p.remove_video(remote)
        remove_tiktok_copy(p, video)
    print("done", flush=True)


def remove_tiktok_copy(p: Phone, video: Path) -> None:
    """TikTok saves every posted video to DCIM/Camera. Remove only that copy: same size, written in the last 15 min."""
    size = video.stat().st_size
    out = p.adb("shell", f"find /storage/emulated/0/DCIM/Camera -name '*.mp4' -mmin -15 -size {size}c")
    for path in (l.strip() for l in out.splitlines()):
        if path.endswith(".mp4"):
            p.remove_video(path)
            print(f"  removed TikTok's saved copy {path}", flush=True)


def _upload(p: Phone, remote: str, video: Path, caption: str, visibility: str, delete_after: bool) -> None:

    print("2. open TikTok", flush=True)
    p.wake()
    p.d.app_start(PKG, stop=True)
    ns = p.wait("home screen", "Home", "Create", "Profile", timeout=30)
    create = next((n for n in ns if n["label"] == "Create" and n["box"][1] >= p.h * 0.88), None)
    p.tap_node(create, "Create tab")

    print("3. camera -> gallery", flush=True)
    p.wait("camera", "Flip", "Timer", "CAMERA")
    p.tap(70, 1510, "gallery thumbnail (bottom-left)")
    ns = p.wait("gallery picker", "Recents", "Videos")
    p.tap_node(p.find("Videos", ns, clickable=True), "Videos tab")
    time.sleep(1.5)
    p.tap(120, 360, "newest video (first tile)")

    print("4. editor -> Next", flush=True)
    # The left button is the story shortcut ("Your Story", or "Story" + a counter in newer versions): never tap it.
    ns = p.wait("editor", "Next", timeout=30)
    if not (p.find("Add sound", ns) or p.find(re.compile(r"^(Your )?Story"), ns)):
        p.fail("Next is on screen but this does not look like the video editor")
    nxt = next((n for n in ns if n["label"] == "Next" and n["box"][0] > 300), None)
    p.tap_node(nxt, "Next (right button, not Story)")

    print("5. post screen", flush=True)
    ns = p.wait("post screen", "Post", "Drafts", timeout=30)
    want = VISIBILITY[visibility]
    # Tap the row's text itself (the clickable container's centre can land on "Add link"), and only once the
    # layout has settled: location suggestions load late and push the rows down.
    def vis_text():
        texts = [n for n in p.nodes() if re.search(r"can view this post$", n["label"])]
        return min(texts, key=lambda n: (n["box"][2] - n["box"][0]) * (n["box"][3] - n["box"][1])) if texts else None
    row, prev = None, None
    for _ in range(8):
        time.sleep(1.2)
        row = vis_text()
        if row and prev and row["box"] == prev["box"]:
            break
        prev = row
    if row is None:
        p.fail("visibility row not found")
    p.tap_node(row, "visibility row (its text)")
    ns = p.wait("visibility sheet", "Who can view this post", want)
    p.tap_node(next(n for n in ns if n["label"] == want and n.get("clickable") == "true"
                    or n["label"] == want), f"visibility '{want}'")
    ns = p.wait("post screen", "Post", "Drafts")
    vis_row = p.find(re.compile(r"can view this post$"), ns, clickable=True)
    expected_row = {"only_me": "Only you can view this post", "everyone": "Everyone can view this post"}.get(visibility)
    if expected_row and (vis_row is None or vis_row["label"] != expected_row):
        p.fail(f"visibility did not stick (row says {vis_row and vis_row['label']!r})")

    desc = p.find("Add description...", ns) or p.find(re.compile(r"."), [n for n in ns if n.get("resource-id", "").endswith(":id/h9x")])
    p.tap_node(desc, "description field")
    p.type_text(caption)
    p.d.press("back")
    time.sleep(1.5)
    ns = p.wait("post screen after caption", "Post", "Drafts")
    if not p.find(caption, ns):
        p.fail("caption did not appear in the description field")

    post = next((n for n in ns if n["label"] == "Post" and n["box"][1] >= p.h * 0.85), None)
    print(f"6. POST  (visibility: {want}, caption: {caption!r})", flush=True)
    p.tap_node(post, "Post")

    print("7. confirm it is on the profile", flush=True)
    time.sleep(35)
    p.d.app_start(PKG, stop=True)
    ns = p.wait("home screen", "Profile", timeout=30)
    p.tap_node(next(n for n in ns if n["label"] == "Profile" and n["box"][1] >= p.h * 0.88), "Profile tab")
    tab = "Private videos" if visibility == "only_me" else "Videos"
    ns = p.wait("profile", tab)
    p.tap_node(p.find(tab, ns), f"{tab} tab")
    time.sleep(2)
    p.tap(119, 795, "newest post tile")
    ns = p.wait("the new post", caption, timeout=20)
    print("  OK: post found with the right caption", flush=True)

    if delete_after:
        print("8. delete the test post", flush=True)
        p.tap_node(p.find(re.compile(r"^Share video"), ns), "Share arrow")
        p.wait("share sheet", "More")
        for _ in range(3):
            if p.find("Delete"):
                break
            p.adb("shell", "input", "swipe", "650", "1440", "80", "1440", "300")
            time.sleep(1)
        p.tap_node(p.find("Delete"), "Delete in share sheet")
        ns = p.wait("delete confirm", "Delete and re-edit")
        confirm = [n for n in ns if n["label"] == "Delete" and n["box"][1] > 1300]
        p.tap_node(confirm[-1] if confirm else None, "Delete (confirm)")
        time.sleep(3)
        print("  deleted", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--caption", required=True)
    ap.add_argument("--visibility", choices=VISIBILITY, default="only_me")
    ap.add_argument("--account", help="switch to this already-logged-in account first")
    ap.add_argument("--delete-after", action="store_true", help="for tests: delete the post once verified")
    a = ap.parse_args()
    try:
        if a.account:
            import accounts
            p = Phone(SERIAL)
            accounts.switch(p, "tiktok", a.account)
        upload(a.video, a.caption, a.visibility, a.delete_after)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
