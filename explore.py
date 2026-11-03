"""Map an Android app's screens and how they connect.

Walks the app depth-first: on each screen it taps every safe clickable element,
records which screen that leads to, then goes back. Output per app:

  maps/<package>/graph.json        screens + edges (from, action, to)
  maps/<package>/screens/<id>.xml  UI tree of each screen
  maps/<package>/screens/<id>.png  screenshot of each screen

Read-only by design: anything that likes, follows, posts, sends, buys, deletes,
reports, logs out or flips a setting is never tapped (see DENY).

Usage: python explore.py com.zhiliaoapp.musically --minutes 60 --depth 4
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import uiautomator2 as u2

DENY = [
    r"\blike\b", r"\bunlike\b", r"\bfollow\b", r"\bunfollow\b", r"follow back",
    r"^post$", r"post now", r"\bpublish\b", r"^send\b", r"\bsend to\b", r"\bdelete\b",
    r"\bremove\b", r"\bblock\b", r"\brestrict\b", r"\breport\b", r"log ?out", r"sign out",
    r"add account", r"switch account", r"\bbuy\b", r"checkout", r"\bpay\b", r"add to (cart|bag)",
    r"place order", r"go live", r"^live$", r"\brepost\b", r"not interested", r"\bdownload\b",
    r"\bfavou?rites?\b", r"\bsave\b", r"\bmute\b", r"\bunsend\b", r"\barchive\b", r"\bhide\b",
    r"\bpin\b", r"turn on", r"\bnotify\b", r"\bsubscribe\b", r"\baccept\b", r"\bconfirm\b",
    r"\bshare\b", r"\bcall\b", r"\bgift\b", r"\bcoins?\b", r"\brecharge\b", r"\bdonate\b",
    r"\binvite\b", r"\bcontacts?\b", r"\bsync\b", r"\bclear\b", r"\breset\b", r"\bdeactivate\b",
    r"switch to (a )?(professional|business|creator)", r"\brecord\b", r"\bcapture\b",
    r"\bshutter\b", r"\bstitch\b", r"\bduet\b", r"\brate\b", r"\breview\b", r"\bdelete account\b",
    r"\bsign up\b", r"\bupgrade\b", r"\bpromote\b", r"\bboost\b", r"\bad\b", r"\bsponsored\b",
    # messaging: never start chats or groups, react, or poke anyone
    r"add people", r"add friends?", r"\bgroup\b", r"invite link", r"\bpoll\b", r"\bgames?\b",
    r"reaction", r"\bwave\b", r"say hi", r"new (message|chat)", r"^[^a-z0-9]+$",
    # settings and lists: never change a value or add/remove people
    r"^add$", r"^remove$", r"^done$", r"^apply$", r"^update$", r"^save changes$", r"^turn off", r"^enable", r"^disable",
    r"^allow\b", r"^restrict", r"^unblock", r"^hide story", r"close friends", r"\bpassword\b", r"two-factor",
    # camera: never start recording or a countdown
    r"countdown", r"^start\b", r"\bshoot\b", r"take photo", r"hands-free",
    # never open our own story camera/creation (not needed for posting feed videos)
    r"add to story", r"create a story", r"^your story", r"your profile\..*story", r"nomoreawkwardness's story",
]
DENY_RE = re.compile("|".join(DENY), re.I)

# Screens where one wrong tap publishes or messages (upload flow, story/reel editors, post screen, chats).
# On these, a deny list is not enough: only buttons matching SAFE_IN_SENSITIVE may be tapped.
SENSITIVE_ACTIVITY = re.compile(r"publish|edit|record|camera|draft|story|chat|shell|direct|thought|creation|modal", re.I)
SENSITIVE_LABELS = re.compile(
    r"^post$|^share$|^send$|shutter|new reel|caption|"
    r"describe your post|hashtags|say something|^message|add sound|^drafts?\b|group chat|new message|send to|^to:$|"
    r"countdown|^flip$|^flash$|^ratio$|ai cast|^timer$", re.I)
PICKER_ACTIVITY = re.compile(r"choosephoto|gallery|album|mediapicker", re.I)
SAFE_IN_SENSITIVE = re.compile(
    r"^(next( \(\d+\))?|back|back to home|close|cancel|exit.*|recents|photos?|videos?|all|gallery|all albums|albums?|"
    r"select|select multiple.*|(un)?selected (photo|video) thumbnail.*|(photo|video) thumbnail.*|edit|music|sounds?|"
    r"text|stickers|effects|filters|templates|timer|speed.*|flash.*|layout|camera settings|story settings)$", re.I)
ALLOW_PERMISSION = re.compile(r"while using the app|only this time|^allow$|allow all|allow access", re.I)
PERMISSION_PKGS = ("com.google.android.permissioncontroller", "com.android.permissioncontroller")
MAX_SAME_ID = 2          # list items: try at most this many siblings sharing an id
MAX_ACTIONS = 28         # per screen
MAX_SCROLLS = 3           # per screen: look this many swipes below the fold for more buttons
MATCH = 0.75             # Jaccard similarity to call two snapshots the same screen
FAMILY_CAP = 6           # same layout seen this many times: further copies count as variants


def norm(label: str) -> str:
    label = re.sub(r"\d[\d.,]*\s*[kmb]?", "#", label.lower())
    return re.sub(r"\s+", " ", label).strip()[:70]


@dataclass
class Node:
    rid: str
    cls: str
    label: str
    clickable: bool
    checkable: bool
    bounds: tuple[int, int, int, int]
    pkg: str
    selected: bool = False
    scrollable: bool = False

    @property
    def short_id(self) -> str:
        return self.rid.split(":id/")[-1] if self.rid else ""

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


@dataclass
class Snap:
    pkg: str
    activity: str
    xml: str
    nodes: list[Node]
    features: frozenset[str]
    keyboard: bool = False


@dataclass
class Screen:
    id: str
    pkg: str
    activity: str
    features: frozenset[str]
    path: list[dict]
    title: str
    labels: list[str]
    actions_done: set[str] = field(default_factory=set)
    unreachable: bool = False


def inner_label(el: ET.Element) -> str:
    """First text or description inside an element, for buttons that carry no label themselves."""
    for sub in el.iter("node"):
        lbl = (sub.get("content-desc") or sub.get("text") or "").strip()
        if lbl:
            return lbl
    return ""


def parse(xml: str) -> list[Node]:
    out = []
    for el in ET.fromstring(xml).iter("node"):
        b = re.findall(r"\d+", el.get("bounds", ""))
        if len(b) != 4:
            continue
        bounds = tuple(int(v) for v in b)
        if bounds[2] - bounds[0] < 8 or bounds[3] - bounds[1] < 8:
            continue
        out.append(Node(
            rid=el.get("resource-id", ""),
            cls=el.get("class", "").split(".")[-1],
            label=(el.get("content-desc") or el.get("text") or "").strip()
            or (inner_label(el) if el.get("clickable") == "true" else ""),
            clickable=el.get("clickable") == "true" and el.get("enabled") == "true",
            checkable=el.get("checkable") == "true",
            bounds=bounds,
            pkg=el.get("package", ""),
            selected=el.get("selected") == "true",
            scrollable=el.get("scrollable") == "true",
        ))
    return out


def features(nodes: list[Node], pkg: str, height: int) -> frozenset[str]:
    """Describe a screen by its chrome (top bar, bottom bar, selected tabs), not its scrolling content.

    Feed items, usernames and counts change every visit; the bars around them don't.
    """
    f = set()
    for n in nodes:
        if n.pkg != pkg:
            continue
        chrome = n.bounds[3] <= height * 0.16 or n.bounds[1] >= height * 0.88
        if n.selected and n.label:
            f.add("sel:" + norm(n.label))
        if not chrome:
            if n.rid and n.clickable:
                f.add("body:" + n.short_id)
            continue
        if n.rid:
            f.add("id:" + n.short_id)
        if n.label and len(n.label) <= 30 and (n.selected or not n.clickable or n.bounds[1] >= height * 0.88):
            f.add("lbl:" + norm(n.label))
    return frozenset(f)


def layout(f: frozenset[str]) -> frozenset[str]:
    return frozenset(x for x in f if not x.startswith("lbl:"))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / max(1, len(a | b))


class Explorer:
    def __init__(self, serial: str, package: str, out: Path, depth: int, minutes: float):
        self.serial = serial
        self.d = u2.connect(serial)
        self.pkg = package
        self.out = out
        (out / "screens").mkdir(parents=True, exist_ok=True)
        self.depth = depth
        self.deadline = time.time() + minutes * 60
        self.w, self.h = self.d.window_size()
        self.screens: dict[str, Screen] = {}
        self.edges: list[dict] = []
        self.log = open(out / "explore.log", "a")

    def resume(self) -> None:
        """Load an earlier run so new screens and edges are added to the same map."""
        g = json.loads((self.out / "graph.json").read_text())
        for sid, s in g["screens"].items():
            xml = (self.out / "screens" / f"{sid}.xml").read_text()
            self.screens[sid] = Screen(sid, s["package"], s["activity"], features(parse(xml), s["package"], self.h),
                                       s["path"], s["title"], s["labels"], set(s.get("actions_tried", [])),
                                       s.get("unreachable", False))
        self.edges = g["edges"]
        self.say(f"resumed {len(self.screens)} screens, {len(self.edges)} edges")

    # ---------- device helpers
    def say(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        self.log.write(line + "\n")
        self.log.flush()

    def reconnect(self, wait_minutes: float = 45) -> bool:
        """Wait for the phone to come back (wireless debugging restarts get a new port; the mDNS name stays)."""
        end = time.time() + wait_minutes * 60
        self.say("phone offline, waiting for it to come back")
        while time.time() < end:
            if ":" in self.serial:
                subprocess.run(["adb", "connect", self.serial], capture_output=True)
            out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
            if re.search(re.escape(self.serial) + r"\s+device\b", out):
                try:
                    self.d = u2.connect(self.serial)
                    self.d.info
                    self.say("phone back online")
                    self.deadline += 0  # keep the original budget
                    return True
                except Exception:
                    pass
            time.sleep(20)
        self.say("phone did not come back, stopping")
        return False

    def keyboard_shown(self) -> bool:
        out = self.d.shell("dumpsys input_method | grep mInputShown").output
        return "mInputShown=true" in out

    def snap(self) -> Snap:
        xml = self.d.dump_hierarchy()
        nodes = parse(xml)
        cur = self.d.app_current()
        pkg = cur.get("package", "")
        return Snap(pkg, cur.get("activity", ""), xml, nodes, features(nodes, pkg, self.h))

    def settle(self) -> Snap:
        time.sleep(1.2)
        prev = self.snap()
        for _ in range(4):
            time.sleep(0.8)
            cur = self.snap()
            if jaccard(prev.features, cur.features) > 0.95 and prev.pkg == cur.pkg:
                return cur
            prev = cur
        return prev

    def wake(self) -> None:
        self.d.shell("input keyevent KEYCODE_WAKEUP")
        self.d.shell("wm dismiss-keyguard")

    def restart(self) -> Snap:
        self.wake()
        self.d.app_stop(self.pkg)
        time.sleep(1)
        self.d.app_start(self.pkg)
        time.sleep(6)
        return self.settle()

    # ---------- screen bookkeeping
    def match(self, s: Snap) -> Screen | None:
        best, score = None, 0.0
        for sc in self.screens.values():
            if sc.pkg != s.pkg or sc.activity != s.activity:
                continue
            j = jaccard(sc.features, s.features)
            if j > score:
                best, score = sc, j
        if score >= MATCH:
            return best
        # Same layout, different labels (another user's profile, another hashtag...):
        # keep a few as separate screens, then treat the rest as variants of the first.
        family = [sc for sc in self.screens.values()
                  if sc.activity == s.activity and jaccard(layout(sc.features), layout(s.features)) >= 0.9]
        return family[0] if len(family) >= FAMILY_CAP else None

    def add(self, s: Snap, path: list[dict]) -> Screen:
        sid = f"s{len(self.screens):03d}"
        labels = [n.label for n in s.nodes if n.label][:40]
        title = self.guess_title(s)
        sc = Screen(sid, s.pkg, s.activity, s.features, list(path), title, labels)
        self.screens[sid] = sc
        (self.out / "screens" / f"{sid}.xml").write_text(s.xml)
        try:
            self.d.screenshot(str(self.out / "screens" / f"{sid}.png"))
        except Exception as e:  # screenshots are nice to have
            self.say(f"screenshot failed for {sid}: {e}")
        self.say(f"NEW {sid} [{s.activity.split('.')[-1]}] {title!r} via {' > '.join(a['label'] for a in path) or 'launch'}")
        self.save()
        return sc

    def guess_title(self, s: Snap) -> str:
        mine = [n for n in s.nodes if n.pkg == s.pkg]
        sel = [n for n in mine if n.selected and n.label]
        top = [n for n in mine if n.label and n.bounds[1] < self.h * 0.16 and len(n.label) < 40]
        if sel and top:
            return f"{top[0].label} ({sel[0].label})"[:60]
        if top:
            return top[0].label
        return (s.activity.split(".")[-1] + (" / " + sel[0].label if sel else ""))[:60]

    def save(self) -> None:
        data = {
            "package": self.pkg,
            "screen_size": [self.w, self.h],
            "screens": {
                sid: {
                    "activity": sc.activity, "package": sc.pkg, "title": sc.title,
                    "path": sc.path, "labels": sc.labels, "unreachable": sc.unreachable,
                    "actions_tried": sorted(sc.actions_done),
                }
                for sid, sc in self.screens.items()
            },
            "edges": self.edges,
        }
        (self.out / "graph.json").write_text(json.dumps(data, indent=1))

    # ---------- safety
    def sensitive(self, s: Snap) -> bool:
        if SENSITIVE_ACTIVITY.search(s.activity.split(".")[-1]):
            return True
        for n in s.nodes:
            if n.pkg != self.pkg or not n.label:
                continue
            if SENSITIVE_LABELS.search(n.label.strip()) and not (
                    re.match(r"message", n.label.strip(), re.I) and n.bounds[1] >= self.h * 0.88):
                return True  # (the "Message" bottom tab is navigation, not a chat composer)
            # Editors put a one-tap "Your story" / "Close Friends" publish button at the bottom;
            # the same words at the top are just the stories tray or a profile badge.
            if re.search(r"your story|close friends", n.label, re.I) and n.bounds[1] > self.h * 0.7:
                return True
        return False

    def allowed(self, s: Snap, n: Node) -> bool:
        """The single gate every tap goes through, including path replays."""
        if not n.clickable or n.checkable or n.pkg != self.pkg:
            return False
        if n.label and DENY_RE.search(n.label):
            return False
        # "Create" is only safe as the bottom-bar tab (opens the upload flow); elsewhere it creates things
        if norm(n.label) == "create":
            tabs = {norm(x.label) for x in s.nodes if x.bounds[1] >= self.h * 0.88}
            if n.bounds[1] < self.h * 0.88 or not {"home", "profile"} <= tabs:
                return False
        if not n.label and not n.rid:
            return False
        if self.sensitive(s):
            if PICKER_ACTIVITY.search(s.activity):
                return True
            return bool(n.label) and bool(SAFE_IN_SENSITIVE.search(n.label.strip()))
        return True

    # ---------- actions
    def actions(self, s: Snap) -> list[dict]:
        seen: dict[str, int] = {}
        acts = []
        for n in sorted(s.nodes, key=lambda n: (n.bounds[1], n.bounds[0])):
            if not self.allowed(s, n):
                continue
            group = n.short_id or ("lbl:" + norm(n.label))
            # Menus and settings lists reuse one id for every row; short wordy labels tell the rows apart.
            # Feed items (long captions, counts) keep sharing one group so we don't open dozens of posts.
            if n.short_id and n.label and len(n.label) <= 32 and not re.search(r"\d", n.label):
                group = n.short_id + ":" + norm(n.label)
            seen[group] = seen.get(group, 0) + 1
            if seen[group] > MAX_SAME_ID:
                continue
            key = f"{group}#{seen[group]}|{norm(n.label)}"
            acts.append({"key": key, "id": n.short_id, "label": n.label[:60] or n.short_id,
                         "nlabel": norm(n.label), "nth": seen[group], "center": n.center})
        return acts[:MAX_ACTIONS]

    # ---------- scrolling (menus and settings lists hide most items below the fold)
    def scroller(self, s: Snap) -> Node | None:
        cands = [n for n in s.nodes if n.scrollable and n.pkg == self.pkg]
        return max(cands, key=lambda n: (n.bounds[2] - n.bounds[0]) * (n.bounds[3] - n.bounds[1]), default=None)

    def swipe(self, s: Snap, up: bool, times: int = 1) -> None:
        n = self.scroller(s)
        if n is None:
            return
        x = (n.bounds[0] + n.bounds[2]) // 2
        top, bot = n.bounds[1] + (n.bounds[3] - n.bounds[1]) * 0.3, n.bounds[1] + (n.bounds[3] - n.bounds[1]) * 0.8
        for _ in range(times):
            if up:
                self.d.swipe(x, bot, x, top, 0.25)
            else:
                self.d.swipe(x, top, x, bot, 0.15)
            time.sleep(0.6)

    def all_actions(self, s: Snap) -> list[dict]:
        acts = self.actions(s)
        if self.scroller(s) is None or self.sensitive(s):
            return acts
        keys = {a["key"].split("#")[0] + "|" + a["nlabel"] for a in acts}
        for k in range(1, MAX_SCROLLS + 1):
            self.swipe(s, up=True)
            cur = self.snap()
            more = [a for a in self.actions(cur) if a["key"].split("#")[0] + "|" + a["nlabel"] not in keys]
            if not more:
                break
            for a in more:
                a["scroll"] = k
                a["key"] += f"@{k}"
                keys.add(a["key"].split("#")[0] + "|" + a["nlabel"])
            acts += more
        self.swipe(s, up=False, times=MAX_SCROLLS + 1)
        return acts[:MAX_ACTIONS + 12]

    def find(self, s: Snap, a: dict) -> Node | None:
        cands = [n for n in s.nodes if n.clickable and (n.short_id or ("lbl:" + norm(n.label))) == (a["id"] or "lbl:" + a["nlabel"])]
        exact = [n for n in cands if norm(n.label) == a["nlabel"]]
        if exact:
            return exact[0]
        if len(cands) >= a["nth"]:
            return cands[a["nth"] - 1]
        return None

    def tap(self, s: Snap, a: dict) -> bool:
        if a.get("scroll"):
            self.swipe(s, up=False, times=MAX_SCROLLS + 1)
            self.swipe(s, up=True, times=a["scroll"])
            s = self.snap()
        n = self.find(s, a)
        if n is None or not self.allowed(s, n):
            self.say(f"refused tap {a['label']!r} on {s.activity.split('.')[-1]}")
            return False
        with open(self.out / "taps.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')}\t{s.activity.split('.')[-1]}\t{'S' if self.sensitive(s) else '-'}\t{n.label[:60]!r}\t{n.short_id}\n")
        self.d.click(*n.center)
        return True

    # ---------- navigation
    def is_on(self, sc: Screen) -> Snap | None:
        s = self.snap()
        if s.pkg == sc.pkg and s.activity == sc.activity and jaccard(s.features, sc.features) >= MATCH:
            return s
        return None

    def go_to(self, sc: Screen) -> Snap | None:
        s = self.is_on(sc)
        if s:
            return s
        for _ in range(3):
            if self.snap().pkg != self.pkg:
                break
            self.d.press("back")
            time.sleep(1.0)
            s = self.is_on(sc)
            if s:
                return s
        s = self.restart()
        for a in sc.path:
            if not self.tap(s, a):
                return None
            s = self.settle()
            s = self.clear_overlays(s)
            if self.keyboard_shown():
                self.d.press("back")
                time.sleep(0.6)
                s = self.settle()
        return self.is_on(sc)

    def clear_overlays(self, s: Snap) -> Snap:
        if s.pkg in PERMISSION_PKGS:
            for n in s.nodes:
                if n.clickable and ALLOW_PERMISSION.search(n.label or ""):
                    self.say(f"permission dialog: tapping {n.label!r}")
                    self.d.click(*n.center)
                    return self.settle()
        for n in s.nodes:
            if n.clickable and re.fullmatch(r"wait", n.label or "", re.I) and "respond" in s.xml:
                self.d.click(*n.center)
                return self.settle()
        return s

    def explore(self, sc: Screen, depth: int) -> list[Screen]:
        """Try every safe action on one screen; return the new screens it led to."""
        found: list[Screen] = []
        s = self.go_to(sc)
        if not s:
            sc.unreachable = True
            self.say(f"cannot reach {sc.id}, skipping")
            return found
        for a in self.all_actions(s):
            if time.time() > self.deadline or len(self.screens) >= 400:
                break
            if a["key"] in sc.actions_done:
                continue
            sc.actions_done.add(a["key"])
            s = self.go_to(sc)
            if not s:
                sc.unreachable = True
                break
            if not self.tap(s, a):
                continue
            new = self.settle()
            kb = False
            if new.pkg == self.pkg and self.keyboard_shown():
                kb = True
                self.d.press("back")
                time.sleep(0.6)
                new = self.settle()
            if new.pkg in PERMISSION_PKGS:
                perm = self.match(new) or self.add(new, sc.path + [a])
                self.edges.append({"from": sc.id, "action": a["label"], "to": perm.id})
                new = self.clear_overlays(new)
            if new.pkg != self.pkg:
                self.edges.append({"from": sc.id, "action": a["label"], "to": f"external:{new.pkg}"})
                self.say(f"{sc.id} --{a['label']!r}--> left app ({new.pkg})")
                self.save()
                continue
            to = self.match(new)
            if to is None:
                to = self.add(new, sc.path + [a])
                found.append(to)
            elif not to.actions_done and not to.unreachable and to is not sc and to not in found:
                to.path = sc.path + [a]  # a fresh, replayable route to a screen we never explored
                found.append(to)
            if to.id != sc.id or kb:
                edge = {"from": sc.id, "action": a["label"], "to": to.id}
                if kb:
                    edge["keyboard"] = True
                self.edges.append(edge)
                self.save()
            if to.id != sc.id:
                self.d.press("back")
                time.sleep(0.8)
        return found

    def run(self, start: list[str] | None = None) -> None:
        """Breadth-first: every screen one tap from launch first, then two taps, and so on.

        With start ids (after resume), the walk begins at those screens instead of launch,
        and depth counts from them.
        """
        self.say(f"start {self.pkg}, depth {self.depth}" + (f", from {start}" if start else ""))
        if start and start[0].startswith("path:"):
            queue = []
            for spec in start:
                labels = spec[5:].split(">")
                s = self.clear_overlays(self.restart())
                path = []
                for lbl in labels:
                    n = next((n for n in s.nodes if norm(n.label) == norm(lbl) and self.allowed(s, n)), None)
                    if n is None:
                        self.say(f"start path: no tappable {lbl!r}")
                        break
                    a = {"key": "", "id": n.short_id, "label": n.label[:60], "nlabel": norm(n.label), "nth": 1, "center": n.center}
                    self.tap(s, a)
                    path.append(a)
                    s = self.settle()
                else:
                    sc = self.match(s) or self.add(s, path)
                    sc.path = path
                    sc.actions_done = set()  # explicitly asked for: explore it again under the current rules
                    queue.append((sc, 0))
        elif start:
            queue = [(self.screens[sid], 0) for sid in start]
        else:
            root = self.clear_overlays(self.restart())
            first = self.match(root) or self.add(root, [])
            queue = [(first, 0)]
        while queue and time.time() < self.deadline:
            sc, depth = queue.pop(0)
            try:
                found = self.explore(sc, depth)
            except Exception as e:  # usually the phone dropping off Wi-Fi
                self.say(f"error on {sc.id}: {type(e).__name__}: {e}")
                self.save()
                if not self.reconnect():
                    break
                queue.insert(0, (sc, depth))
                continue
            for nxt in found:
                if depth + 1 < self.depth:
                    queue.append((nxt, depth + 1))
        self.save()
        self.say(f"done: {len(self.screens)} screens, {len(self.edges)} edges, {len(queue)} screens left unexplored")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("package")
    p.add_argument("--serial", default="192.168.1.150:5555",
                   help="classic adb tcpip port; survives Wi-Fi band changes, resets on reboot")
    p.add_argument("--depth", type=int, default=4)
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--out", default=str(Path(__file__).parent / "maps"))
    p.add_argument("--from", dest="start", default="", help="comma-separated screen ids to continue from (resumes the saved map)")
    args = p.parse_args()
    out = Path(args.out) / args.package
    ex = Explorer(args.serial, args.package, out, args.depth, args.minutes)
    start = [x for x in args.start.split(",") if x]
    if start:
        ex.resume()
        if start and start[0].startswith("path:"):
            pass
        elif start == ["unexplored"]:
            start = [sid for sid, sc in ex.screens.items() if not sc.actions_done and not sc.unreachable]
    ex.run(start or None)


if __name__ == "__main__":
    sys.exit(main())
