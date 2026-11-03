"""Delete one of our posts, found by its caption (used to clean up test posts).

Usage:
  python delete.py tiktok "caption start" [--account nomoreawkwardness]
  python delete.py instagram "caption start" [--account nomoreawkwardness]
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import stats
from phone import SERIAL, Abort, Phone


def tt_delete(p: Phone, caption: str) -> None:
    stats.tt_open_post(p, caption)
    p.tap_node(p.find(re.compile(r"^Share video")), "Share arrow")
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


def ig_delete(p: Phone, caption: str) -> None:
    stats.ig_open_post(p, caption)
    p.tap_node(p.find("More"), "reel menu")
    ns = p.wait("reel menu", "Delete")
    p.tap_node(next(n for n in ns if n["label"] == "Delete"), "Delete")
    p.wait("delete confirm", "Delete reel?")
    p.tap_node(p.by_id("igds_alert_dialog_primary_button"), "Delete (confirm)")
    time.sleep(3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=("tiktok", "instagram"))
    ap.add_argument("caption")
    ap.add_argument("--account")
    a = ap.parse_args()
    p = Phone(SERIAL)
    p.wake()
    try:
        if a.account:
            import accounts
            accounts.switch(p, a.app, a.account)
        (tt_delete if a.app == "tiktok" else ig_delete)(p, a.caption)
        print("deleted", flush=True)
    except Abort as e:
        print(f"ABORTED: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
