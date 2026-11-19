"""Content engine box: runs the day's plan on the phone, one job at a time, and serves the dashboard.

  uv run python engine.py            -> dashboard on http://127.0.0.1:8080

New code arrives by itself: see updater.py (CI moves the deploy branch; the engine fast-forwards while idle).

Plans arrive as mailbox/inbox/<name>/plan.json (+ videos/). Each plan is imported once into engine.db as jobs:
  post      one per post x app/account, at a random minute of its round hour (12/16/20) or at an exact "at"
  stats     added after a post succeeds, at the configured offsets (1h 3h 6h 12h 24h 48h)
  research  fills the gaps between fixed jobs, inside its time window, within daily limits
  delete    clean-up of test posts
Each job runs the matching script (tiktok_upload.py, instagram_upload.py, stats.py, research.py, delete.py) as
a subprocess; its output goes to logs/<id>.log. Failures retry once, then raise an alert (dashboard + email).
Take over pauses the runner: research/stats are stopped at once, a post or delete is allowed to finish first.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import signal
import smtplib
import sqlite3
import sys
import time
import urllib.request
import tomllib
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import access
import updater

from aiohttp import WSMsgType, web

from plan_check import caption_key, check_plan
from scrcpy_bridge import Mirror

ROOT = Path(__file__).parent
CFG = tomllib.loads((ROOT / "config.toml").read_text())
USB = CFG["phone"].get("usb_serial") or CFG["phone"].get("hw_serial", "")  # over the cable: preferred


def wifi_addr() -> str:
    """The Wi-Fi adb address (ip:5555), the fallback. Read fresh: adb_keeper.sh rewrites it when the phone moves."""
    try:
        return tomllib.loads((ROOT / "config.toml").read_text())["phone"]["serial"]
    except Exception:
        return CFG["phone"]["serial"]


SERIAL = USB or CFG["phone"]["serial"]  # the transport in use now; Runner.find_phone() switches it
DB = ROOT / "engine.db"
LOGS = ROOT / "logs"
INBOX = ROOT / CFG["mailbox"]["inbox"]
SCHED = CFG["schedule"]
FIXED = ("preflight", "post", "delete", "stats")  # jobs with a set time; research fills the gaps
TIMEOUT_MIN = {"post": 15, "delete": 8, "stats": 10, "preflight": 15}
PUBLISH_TAP = re.compile(r"tap (Post|Share) at")  # once seen, a failed post must not be retried blindly


# ================================================================ database
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
    create table if not exists jobs (
      id integer primary key, plan text, kind text, app text, account text, run_at real, not_after real,
      args text, status text default 'waiting', attempts integer default 0, started_at real, ended_at real,
      result text, error text, shot text, post_key text, label text, created_at real);
    create table if not exists plans (name text primary key, day text, imported_at real);
    create table if not exists alerts (id integer primary key, at real, kind text, text text, emailed integer default 0,
      seen integer default 0);
    create table if not exists state (k text primary key, v text);
    """)
    return con


def get_state(k: str, default=None):
    row = db().execute("select v from state where k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_state(k: str, v) -> None:
    con = db()
    con.execute("insert into state (k, v) values (?, ?) on conflict(k) do update set v=excluded.v", (k, json.dumps(v)))
    con.commit()


def add_job(con, **f) -> int:
    f.setdefault("created_at", time.time())
    if isinstance(f.get("args"), dict):
        f["args"] = json.dumps(f["args"])
    cur = con.execute(f"insert into jobs ({', '.join(f)}) values ({', '.join('?' * len(f))})", tuple(f.values()))
    return cur.lastrowid


# ================================================================ alerts
_last_alert: dict[str, float] = {}


def alert(kind: str, text: str, every_min: float = 30) -> None:
    """Record an alert; email it to yourself if an app password is configured. Same kind at most every N min."""
    now = time.time()
    if now - _last_alert.get(kind, 0) < every_min * 60:
        return
    _last_alert[kind] = now
    print(f"ALERT [{kind}] {text}", flush=True)
    emailed = 0
    e = CFG.get("email", {})
    if e.get("app_password"):
        try:
            m = EmailMessage()
            m["Subject"] = f"[content engine] {kind}: {text[:80]}"
            m["From"], m["To"] = e["from"], e["to"]
            m.set_content(f"{text}\n\n{datetime.now():%Y-%m-%d %H:%M}\nDashboard: http://{CFG['dashboard']['host']}:{CFG['dashboard']['port']}")
            with smtplib.SMTP_SSL(e["smtp_host"], e["smtp_port"], timeout=20) as s:
                s.login(e["from"], e["app_password"])
                s.send_message(m)
            emailed = 1
        except Exception as ex:  # never let alerting break the runner
            print(f"  email failed: {ex}", flush=True)
    con = db()
    con.execute("insert into alerts (at, kind, text, emailed) values (?,?,?,?)", (now, kind, text, emailed))
    con.commit()


# ================================================================ plans -> jobs
def at_time(day: str, hhmm: str) -> float:
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").timestamp()


def import_plans() -> None:
    con = db()
    for f in sorted(INBOX.glob("*/plan.json")):
        name = f.parent.name
        if con.execute("select 1 from plans where name=?", (name,)).fetchone():
            continue
        try:
            raw = json.loads(f.read_text())
            existing = {caption_key(json.loads(r["args"]).get("caption", ""))
                        for r in con.execute("select args from jobs where kind='post'")}
            per_day: dict = {}
            try:
                d0 = datetime.strptime(str(raw.get("day")), "%Y-%m-%d").timestamp()
                for r in con.execute("select app, account, count(*) n from jobs where kind='post' and status not in "
                                     "('failed','skipped') and run_at>=? and run_at<? group by app, account", (d0, d0 + 86400)):
                    per_day[(r["app"], r["account"])] = r["n"]
            except ValueError:
                pass
            plan, problems = check_plan(raw, f.parent, CFG, existing, per_day)
            if problems:
                alert("plan", f"plan {name}: {'rejected' if plan is None else 'some items dropped'}:\n- " + "\n- ".join(problems),
                      every_min=0)
            if plan is None:
                raise ValueError("plan rejected")
            day = plan["day"]
            for post in plan.get("posts", []):
                for t in post["targets"]:
                    if t.get("at") or post.get("at"):
                        run_at = at_time(day, t.get("at") or post["at"])
                    else:  # a random minute (and second) of the round's hour
                        run_at = at_time(day, f"{int(post['round']):02d}:00") + random.randrange(0, 3600)
                    args = {"video": str((f.parent / post["video"]).resolve()), "caption": post["caption"],
                            "stats_offsets_min": plan.get("stats_offsets_min")}
                    if t["app"] == "tiktok":
                        args["visibility"] = t.get("visibility", "everyone")
                    else:
                        args["audience"] = t.get("audience", "everyone")
                    add_job(con, plan=name, kind="post", app=t["app"], account=t["account"], run_at=run_at,
                            args=args, post_key=f"{name}/{post['id']}/{t['app']}", label=post["id"])
            for r in plan.get("research", []):
                add_job(con, plan=name, kind="research", app=r["app"], account=r["account"],
                        run_at=at_time(day, r.get("not_before", "09:00")), not_after=at_time(day, r.get("not_after", "23:00")),
                        args={k: r[k] for k in ("keyword", "minutes", "max_likes", "max_comments") if k in r},
                        label=r["keyword"])
            for d in plan.get("deletes", []):
                add_job(con, plan=name, kind="delete", app=d["app"], account=d["account"], run_at=at_time(day, d["at"]),
                        args={"caption": d["caption"]}, label="clean-up")
            con.execute("insert into plans (name, day, imported_at) values (?,?,?)", (name, day, time.time()))
            con.commit()
            print(f"imported plan {name}", flush=True)
        except Exception as ex:
            con.rollback()
            if str(ex) != "plan rejected":
                alert("plan", f"could not import {name}: {ex}", every_min=0)
            con.execute("insert into plans (name, day, imported_at) values (?,?,?)", (name, "?", time.time()))
            con.commit()


# ================================================================ sync with the render server (over Tailscale)
_last_sync = 0.0


def sync() -> None:
    """Every [sync].every_min minutes (engine-initiated, so the server never needs access to this machine):
    - push consistent copies of stats.db / research.db / engine.db to <host>:feedback/ (what the nightly session reads)
    - pull finished plans from <host>:outbox/<name>/ (plan.json + videos/) into the inbox, deleting them on the
      server once copied. A plan is staged in mailbox/incoming/ and only moved into the inbox when fully copied.
    The server writes a plan as outbox/.<name>.part/ and renames it to outbox/<name>/ when complete."""
    global _last_sync
    s = CFG.get("sync")
    if not s or not s.get("host") or time.time() - _last_sync < s.get("every_min", 10) * 60:
        return
    _last_sync = time.time()
    import shutil
    import subprocess
    host = s["host"]
    ssh = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    stage = ROOT / "mailbox" / "feedback"
    stage.mkdir(parents=True, exist_ok=True)
    for name in ("stats.db", "research.db", "engine.db"):
        src = ROOT / name
        if src.exists():
            with sqlite3.connect(src) as a, sqlite3.connect(stage / name) as b:
                a.backup(b)  # consistent snapshot even while jobs write
    # which accounts exist and their daily limits (no secrets), so the nightly session plans within capacity
    (stage / "accounts.json").write_text(json.dumps(
        {"accounts": [{"app": app, "account": name, **lim} for app, accts in CFG.get("accounts", {}).items()
                      for name, lim in accts.items()],
         "stats_offsets_min": SCHED["stats_offsets_min"], "rounds": [12, 16, 20],
         "written_at": datetime.now().isoformat(timespec="seconds")}, indent=1))
    r = subprocess.run(["rsync", "-az", "-e", ssh, f"{stage}/", f"{host}:feedback/"], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        alert("sync", f"could not push feedback to {host}: {r.stderr.strip()[:200]}")
        return
    ls = subprocess.run(ssh.split() + [host, "ls -1 outbox 2>/dev/null"], capture_output=True, text=True, timeout=60)
    for name in [n for n in ls.stdout.split() if not n.startswith(".")]:
        incoming = ROOT / "mailbox" / "incoming" / name
        incoming.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["rsync", "-az", "--remove-source-files", "-e", ssh, f"{host}:outbox/{name}/", f"{incoming}/"],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not (incoming / "plan.json").exists():
            alert("sync", f"could not pull plan {name} from {host}: {r.stderr.strip()[:200]}")
            continue
        subprocess.run(ssh.split() + [host, f"rm -rf outbox/{name}"], capture_output=True, timeout=60)
        target = INBOX / name
        if target.exists():
            alert("sync", f"plan {name} already in the inbox: kept the new copy in mailbox/incoming/{name}", every_min=0)
            continue
        shutil.move(str(incoming), str(target))
        print(f"sync: pulled plan {name}", flush=True)


# ================================================================ missing-plan warning
def check_plan_arrived() -> None:
    """If the nightly session failed (crash, usage limit, server down), no plan arrives and the engine would quietly
    do nothing. From [sync].plan_deadline (default 11:00, local time) warn once a day when today has no plan."""
    s = CFG.get("sync")
    if not s or not s.get("host") or not s.get("expect_plans"):  # off until nightly runs go live
        return
    now = datetime.now()
    deadline = s.get("plan_deadline", "11:00")
    if now.strftime("%H:%M") < deadline:
        return
    today = now.strftime("%Y-%m-%d")
    if get_state("plan_warned") == today:
        return
    if db().execute("select 1 from plans where day=?", (today,)).fetchone():
        return
    set_state("plan_warned", today)
    alert("plan", f"no plan for {today} arrived by {deadline}: the nightly run on the render server probably failed "
                  f"(check ~/nightly/runs/{today}/run.log on {s['host']}). Nothing will be posted today.", every_min=0)


# ================================================================ morning preflight
_last_beat = 0.0


def heartbeat() -> None:
    """Ping [health].engine_url (a healthchecks.io check) every [health].every_min minutes while the runner loop is
    alive. A silent engine (Pi off, no network, crashed loop) then reaches the user as an email from healthchecks.io,
    which this box could not send itself. Off when no URL is set."""
    global _last_beat
    h = CFG.get("health", {})
    url = h.get("engine_url")
    if not url or time.time() - _last_beat < h.get("every_min", 5) * 60:
        return
    _last_beat = time.time()
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as ex:
        print(f"heartbeat failed: {ex!r}", flush=True)


def schedule_preflight() -> None:
    """Once a day at [preflight].at (default 09:30, local time) queue a preflight job (phone, accounts, disks,
    render server, today's plan). It runs like any job: failure -> one retry -> email with every problem found."""
    pf = CFG.get("preflight", {})
    if pf.get("enabled", True) is False:
        return
    now = datetime.now()
    if now.strftime("%H:%M") < pf.get("at", "09:30"):
        return
    today = now.strftime("%Y-%m-%d")
    if get_state("preflight_day") == today:
        return
    set_state("preflight_day", today)
    con = db()
    add_job(con, plan="daily", kind="preflight", app="-", account="-", run_at=time.time(), args={}, label="preflight")
    con.commit()


def schedule_outliers() -> None:
    """Every [outliers].every_days days queue one niche-outlier scan (outliers.py) on the practice account. It is a
    research job (args mode=outliers): it only runs in a gap between fixed jobs and counts as research time."""
    o = CFG.get("outliers", {})
    if not o.get("account") or o.get("enabled", True) is False:
        return
    last = float(get_state("outliers_queued") or 0)
    if time.time() - last < o.get("every_days", 7) * 86400:
        return
    if datetime.now().strftime("%H:%M") < o.get("after", "10:00"):
        return
    set_state("outliers_queued", time.time())
    con = db()
    add_job(con, plan="weekly", kind="research", app="instagram", account=o["account"], run_at=time.time(),
            args={"mode": "outliers", "minutes": o.get("minutes", 40)}, label="niche outliers")
    con.commit()


# ================================================================ disk clean-up
_last_cleanup = 0.0


def cleanup() -> None:
    """Keep the disk from filling up (runs at most once an hour):
    - a plan's videos are deleted as soon as none of its posts is waiting or running (they are on the phone's
      accounts by then; captions stay in engine.db for stats),
    - job logs older than 30 days and failure screenshots older than 14 days are deleted."""
    global _last_cleanup
    if time.time() - _last_cleanup < 3600:
        return
    _last_cleanup = time.time()
    con = db()
    freed = 0
    for videos in INBOX.glob("*/videos"):
        pending = con.execute("select count(*) from jobs where plan=? and kind='post' and status in ('waiting','running')",
                              (videos.parent.name,)).fetchone()[0]
        imported = con.execute("select 1 from plans where name=?", (videos.parent.name,)).fetchone()
        if imported and not pending:
            for f in videos.glob("*"):
                freed += f.stat().st_size
                f.unlink()
    now = time.time()
    for folder, days in ((LOGS, 30), (ROOT / "runs", 14)):
        for f in folder.glob("*"):
            if f.is_file() and now - f.stat().st_mtime > days * 86400 and f.name != "engine.out":
                freed += f.stat().st_size
                f.unlink()
    if freed:
        print(f"clean-up: freed {freed / 1e6:.0f} MB", flush=True)


# ================================================================ limits
def acct_cfg(app: str, account: str) -> dict:
    return CFG.get("accounts", {}).get(app, {}).get(account, {})


def used_today(app: str, account: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    out = {"likes": 0, "comments": 0, "posts": 0, "research_minutes": 0.0}
    rdb = ROOT / "research.db"
    if rdb.exists():
        rc = sqlite3.connect(rdb)
        cols = [r[1] for r in rc.execute("pragma table_info(videos)")]
        if "account" in cols:
            row = rc.execute("select coalesce(sum(liked),0), count(commented) from videos where app=? and account=? "
                             "and seen_at like ?", (app, account, today + "%")).fetchone()
            out["likes"], out["comments"] = row
    start = datetime.strptime(today, "%Y-%m-%d").timestamp()
    con = db()
    out["posts"] = con.execute("select count(*) from jobs where kind='post' and app=? and account=? and status in "
                               "('done','unverified') and started_at>=?", (app, account, start)).fetchone()[0]
    out["research_minutes"] = con.execute("select coalesce(sum(ended_at-started_at),0)/60.0 from jobs where kind='research' "
                                          "and app=? and account=? and started_at>=? and ended_at is not null",
                                          (app, account, start)).fetchone()[0]
    return out


# ================================================================ runner
class Runner:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.job: sqlite3.Row | None = None
        self.tail: list[str] = []
        self.interrupted = False
        self.phone_ok = None

    @property
    def paused(self) -> bool:
        return bool(get_state("paused", False))

    async def adb(self, *args: str, timeout: float = 15) -> str:
        try:
            p = await asyncio.create_subprocess_exec("adb", *args, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
            return out.decode(errors="replace").strip()
        except Exception as ex:
            return f"error {ex}"

    async def find_phone(self) -> str | None:
        """The phone over the USB cable if it answers there, else over Wi-Fi. Switches SERIAL (jobs, dashboard,
        live screen) to whichever answers; None when neither does."""
        global SERIAL
        wifi = wifi_addr()
        for s in dict.fromkeys(x for x in (USB, wifi) if x):
            ok = await self.adb("-s", s, "get-state") == "device"
            if not ok and ":" in s:
                await self.adb("connect", s)
                ok = await self.adb("-s", s, "get-state") == "device"
            if ok:
                if s != SERIAL:
                    print(f"phone: now over {'Wi-Fi' if ':' in s else 'USB'} ({s}), was {SERIAL}", flush=True)
                    SERIAL = MIRROR.serial = s
                return s
        return None

    async def check_phone(self) -> bool:
        ok = await self.find_phone() is not None
        self.phone_ok = ok
        if not ok:
            alert("phone", f"phone is not connected over adb (USB {USB or '-'}, Wi-Fi {wifi_addr()})")
        return ok

    def pick(self, now: float):
        """Next job to run now, or None. Fixed jobs first (posts > deletes > stats), then research in a gap."""
        con = db()
        job = con.execute("select * from jobs where status='waiting' and kind in ('preflight','post','delete','stats') and run_at<=? "
                          "order by case kind when 'preflight' then 0 when 'post' then 1 when 'delete' then 2 else 3 end, run_at limit 1",
                          (now,)).fetchone()
        if job:
            return job, None
        nxt = con.execute("select min(run_at) from jobs where status='waiting' and kind in ('preflight','post','delete','stats')").fetchone()[0]
        # fetchall: an open read cursor keeps a shared lock, and finish() below writes on another connection
        for r in con.execute("select * from jobs where status='waiting' and kind='research' and run_at<=? order by run_at",
                             (now,)).fetchall():
            if r["not_after"] and now > r["not_after"]:
                self.finish(r["id"], "skipped", error="its time window passed")
                continue
            a = json.loads(r["args"])
            lim, used = acct_cfg(r["app"], r["account"]), used_today(r["app"], r["account"])
            minutes = min(float(a.get("minutes", 10)),
                          (r["not_after"] - now) / 60 if r["not_after"] else 1e9,
                          (nxt - now) / 60 - SCHED["research_margin_min"] if nxt else 1e9,
                          lim.get("research_minutes_per_day", 60) - used["research_minutes"])
            if minutes >= (20 if a.get("mode") == "outliers" else 2):
                return r, round(minutes, 1)
        return None, None

    def finish(self, job_id: int, status: str, **f) -> None:
        con = db()
        f["status"] = status
        con.execute(f"update jobs set {', '.join(k + '=?' for k in f)} where id=?", (*f.values(), job_id))
        con.commit()

    def command(self, job, minutes: float | None) -> list[str]:
        a = json.loads(job["args"])
        py = sys.executable
        app, acct = job["app"], job["account"]
        if job["kind"] == "post":
            if app == "tiktok":
                return [py, "tiktok_upload.py", a["video"], "--caption", a["caption"], "--visibility", a["visibility"], "--account", acct]
            return [py, "instagram_upload.py", a["video"], "--caption", a["caption"], "--audience", a["audience"], "--account", acct]
        if job["kind"] == "stats":
            cmd = [py, "stats.py", app, a["caption"], "--account", acct, "--label", job["label"]]
            if app == "tiktok" and a.get("offset_min", 0) >= SCHED["tiktok_detailed_from_min"]:
                cmd.append("--detailed")
            return cmd
        if job["kind"] == "research" and a.get("mode") == "outliers":
            return [py, "outliers.py", "--account", acct, "--minutes", str(minutes)]
        if job["kind"] == "research":
            lim, used = acct_cfg(app, acct), used_today(app, acct)
            likes = max(0, min(int(a.get("max_likes", 3)), lim.get("likes_per_day", 20) - used["likes"]))
            comments = max(0, min(int(a.get("max_comments", 1)), lim.get("comments_per_day", 5) - used["comments"]))
            return [py, "research.py", app, a["keyword"], "--minutes", str(minutes), "--max-likes", str(likes),
                    "--max-comments", str(comments), "--account", acct]
        if job["kind"] == "delete":
            return [py, "delete.py", app, a["caption"], "--account", acct]
        if job["kind"] == "preflight":
            return [py, "preflight.py"]
        raise ValueError(job["kind"])

    async def run(self, job, minutes: float | None) -> None:
        if job["kind"] == "post":
            lim, used = acct_cfg(job["app"], job["account"]), used_today(job["app"], job["account"])
            if used["posts"] >= lim.get("posts_per_day", 3):
                self.finish(job["id"], "skipped", error=f"daily post limit reached for {job['account']}")
                return
        self.job, self.tail, self.interrupted = job, [], False
        started = time.time()
        attempts = job["attempts"] + 1
        self.finish(job["id"], "running", started_at=started, attempts=attempts, error=None)
        cmd = self.command(job, minutes)
        log = LOGS / f"{job['id']}.log"
        timeout = (minutes + 8 if job["kind"] == "research" else TIMEOUT_MIN[job["kind"]]) * 60
        env = {**os.environ, "PHONE_SERIAL": SERIAL, "PYTHONUNBUFFERED": "1"}
        result, aborted = None, None
        with log.open("a") as fh:
            fh.write(f"\n=== attempt {attempts} at {datetime.now():%Y-%m-%d %H:%M:%S}: {' '.join(cmd[1:])}\n")
            self.proc = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            set_state("job_pgid", self.proc.pid)  # so a restarted engine can stop an orphaned job

            async def pump():
                nonlocal result, aborted
                async for raw in self.proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    fh.write(line + "\n")
                    fh.flush()
                    self.tail = (self.tail + [line])[-300:]
                    if line.startswith("RESULT "):
                        result = line[7:]
                    elif line.startswith("ABORTED:"):
                        aborted = line[8:].strip()

            try:
                await asyncio.wait_for(pump(), timeout)
                rc = await self.proc.wait()
            except asyncio.TimeoutError:
                os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
                rc, aborted = -9, f"timed out after {timeout / 60:.0f} min"
        self.proc = None
        set_state("job_pgid", None)
        ended = time.time()
        shots = sorted((p for p in (ROOT / "runs").glob("fail-*.png") if p.stat().st_mtime >= started), key=lambda p: p.stat().st_mtime)
        shot = shots[-1].name if shots else None
        log_text = "\n".join(self.tail)
        self.job = None

        if self.interrupted:  # taken over mid-job
            if job["kind"] == "research":
                self.finish(job["id"], "done", ended_at=ended, result=result, error="stopped early (taken over)")
            else:
                self.finish(job["id"], "waiting", ended_at=ended, attempts=attempts - 1, error="interrupted (taken over)")
            return
        if rc == 0:
            self.finish(job["id"], "done", ended_at=ended, result=result, shot=shot)
            if job["kind"] == "post":
                self.schedule_stats(job, ended)
            return
        err = aborted or f"exit code {rc}"
        if job["kind"] == "post" and PUBLISH_TAP.search(log_text):
            # It may have gone out: never post twice. Stats will tell whether it exists.
            self.finish(job["id"], "unverified", ended_at=ended, error=f"published but not verified: {err}", shot=shot)
            self.schedule_stats(job, ended)
            alert("post", f"{job['app']} @{job['account']} '{job['label']}': published but not verified ({err}). Check the profile.", every_min=0)
            return
        if attempts < SCHED["max_attempts"] and job["kind"] != "research":
            self.finish(job["id"], "waiting", ended_at=ended, error=err, shot=shot,
                        run_at=time.time() + SCHED["retry_after_min"] * 60)
        else:
            self.finish(job["id"], "failed", ended_at=ended, error=err, shot=shot)
            alert("job", f"{job['kind']} {job['app']} @{job['account']} '{job['label']}' failed: {err}", every_min=0)

    def schedule_stats(self, job, posted_at: float) -> None:
        a = json.loads(job["args"])
        con = db()
        for off in a.get("stats_offsets_min") or SCHED["stats_offsets_min"]:
            label = f"{off // 60}h" if off % 60 == 0 else f"{off}m"
            add_job(con, plan=job["plan"], kind="stats", app=job["app"], account=job["account"],
                    run_at=posted_at + off * 60, args={"caption": a["caption"], "offset_min": off},
                    post_key=job["post_key"], label=label)
        con.commit()

    async def park(self) -> None:
        """Between jobs: leave the app (a reel left open keeps looping and keeps the screen on) and lock the screen.
        Every job wakes the phone itself (Phone.wake), so nothing else needs to change."""
        await self.adb("-s", SERIAL, "shell", "input", "keyevent", "KEYCODE_HOME")
        await self.adb("-s", SERIAL, "shell", "input", "keyevent", "KEYCODE_SLEEP")

    async def take_over(self) -> None:
        set_state("paused", True)
        touch()
        await self.adb("-s", SERIAL, "shell", "input", "keyevent", "KEYCODE_WAKEUP")  # you see a live screen
        await self.adb("-s", SERIAL, "shell", "wm", "dismiss-keyguard")
        if self.proc and self.job and self.job["kind"] in ("research", "stats"):
            self.interrupted = True
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def auto_resume(self) -> None:
        """A take-over nobody touches for [dashboard].resume_after_min minutes ends by itself, so a forgotten
        dashboard tab never stops the day's posts."""
        idle = CFG.get("dashboard", {}).get("resume_after_min", 10)
        if self.paused and self.job is None and idle and time.time() - (get_state("touched", 0) or 0) > idle * 60:
            set_state("paused", False)
            alert("pause", f"take-over idle for {idle} min: resumed by itself", every_min=0)
            await self.park()

    async def loop(self) -> None:
        while True:
            try:
                await self.auto_resume()
                try:
                    await asyncio.to_thread(sync)
                except Exception as ex:
                    alert("sync", f"sync failed: {ex!r}")
                import_plans()
                schedule_preflight()
                schedule_outliers()
                check_plan_arrived()
                cleanup()
                await asyncio.to_thread(heartbeat)
                if not self.paused and self.job is None:
                    nxt = db().execute("select min(run_at) from jobs where status='waiting' "
                                       "and kind in ('preflight','post','delete','stats')").fetchone()[0]
                    if await asyncio.to_thread(updater.check, CFG, True, nxt, alert):
                        print("restarting on the new code", flush=True)
                        os.execv(sys.executable, [sys.executable, *sys.argv])
                if not self.paused and self.job is None:
                    job, minutes = self.pick(time.time())
                    if job is not None and await self.check_phone():
                        await self.run(job, minutes)
                        if not self.paused:
                            await self.park()
                        continue
                if self.job is None and int(time.time()) % 60 < 5:
                    if self.paused:
                        await self.check_phone()
                    else:
                        await self.find_phone()  # idle: keep the dashboard on the live transport (cable back/out)
            except Exception as ex:
                alert("engine", f"runner error: {ex!r}")
                self.job = None
            await asyncio.sleep(5)


RUNNER = Runner()
MIRROR = Mirror(SERIAL)


# ================================================================ web
def rows(cur) -> list[dict]:
    return [dict(r) for r in cur]


async def page(_req):
    return web.FileResponse(ROOT / "web" / "dashboard.html")


async def api_state(_req):
    con = db()
    now = time.time()
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    jobs = rows(con.execute("select * from jobs where run_at>=? - 3600*6 or status in ('waiting','running') "
                            "order by run_at", (start,)))
    for j in jobs:
        j["args"] = json.loads(j["args"] or "{}")
    accounts = []
    for app, accts in CFG.get("accounts", {}).items():
        for name, lim in accts.items():
            accounts.append({"app": app, "account": name, "limits": lim, "used": used_today(app, name)})
    return web.json_response({
        "now": now,
        "paused": RUNNER.paused,
        "pausing": RUNNER.paused and RUNNER.job is not None,
        "phone_ok": RUNNER.phone_ok,
        "running": dict(RUNNER.job) if RUNNER.job else None,
        "tail": RUNNER.tail[-40:],
        "jobs": jobs,
        "alerts": rows(con.execute("select * from alerts order by at desc limit 30")),
        "accounts": accounts,
        "mirror_error": MIRROR.error,
    })


async def api_takeover(_req):
    await RUNNER.take_over()
    return web.json_response({"ok": True})


async def api_resume(_req):
    set_state("paused", False)
    if RUNNER.job is None:
        await RUNNER.park()
    return web.json_response({"ok": True})


async def api_job_action(req):
    jid, action = int(req.match_info["id"]), req.match_info["action"]
    con = db()
    job = con.execute("select * from jobs where id=?", (jid,)).fetchone()
    if job is None or job["status"] == "running":
        return web.json_response({"ok": False, "error": "no such job or it is running"}, status=400)
    if action == "skip":
        con.execute("update jobs set status='skipped', error='skipped by you' where id=?", (jid,))
    elif action in ("retry", "now"):
        con.execute("update jobs set status='waiting', run_at=?, error=null where id=?", (time.time(), jid))
    con.commit()
    return web.json_response({"ok": True})


async def api_log(req):
    f = LOGS / f"{int(req.match_info['id'])}.log"
    return web.Response(text=f.read_text() if f.exists() else "(no log yet)")


async def api_alerts_seen(_req):
    con = db()
    con.execute("update alerts set seen=1")
    con.commit()
    return web.json_response({"ok": True})


async def api_results(_req):
    posts = []
    sdb = ROOT / "stats.db"
    snaps = {}
    if sdb.exists():
        sc = sqlite3.connect(sdb)
        sc.row_factory = sqlite3.Row
        cols = [r[1] for r in sc.execute("pragma table_info(snapshots)")]
        if "label" in cols:
            for r in sc.execute("select * from snapshots where label is not null order by taken_at"):
                snaps.setdefault((r["app"], r["account"], r["caption"]), []).append(
                    {"label": r["label"], "taken_at": r["taken_at"], "data": json.loads(r["data"])})
    con = db()
    for j in con.execute("select * from jobs where kind='post' and status in ('done','unverified') order by started_at desc limit 60"):
        a = json.loads(j["args"])
        posts.append({"id": j["id"], "app": j["app"], "account": j["account"], "label": j["label"], "caption": a["caption"],
                      "posted_at": j["ended_at"], "status": j["status"],
                      "snapshots": snaps.get((j["app"], j["account"], a["caption"]), [])})
    research = []
    rdb = ROOT / "research.db"
    if rdb.exists():
        rc = sqlite3.connect(rdb)
        rc.row_factory = sqlite3.Row
        cols = [r[1] for r in rc.execute("pragma table_info(videos)")]
        sel = "commented, account" if "commented" in cols else "null as commented, null as account"
        research = rows(rc.execute(f"select id, app, keyword, seen_at, creator, caption, likes, comments, relevant, liked, {sel} "
                                   "from videos order by id desc limit 80"))
    return web.json_response({"posts": posts, "research": research})


async def shot(req):
    f = ROOT / "runs" / Path(req.match_info["name"]).name
    if not f.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(f)


async def screen_png(_req):
    """Fallback for browsers without WebCodecs (e.g. plain http over Tailscale): one screenshot."""
    p = await asyncio.create_subprocess_exec("adb", "-s", SERIAL, "exec-out", "screencap", "-p", stdout=asyncio.subprocess.PIPE)
    out, _ = await asyncio.wait_for(p.communicate(), 15)
    return web.Response(body=out, content_type="image/png", headers={"Cache-Control": "no-store"})


def touch() -> None:
    """You used the phone from the dashboard: keeps a take-over from resuming by itself."""
    set_state("touched", time.time())


async def screen_input(req):
    """Fallback input (adb), only while taken over."""
    if not RUNNER.paused:
        return web.json_response({"ok": False, "error": "take over first"}, status=409)
    touch()
    m = await req.json()
    if m["t"] == "tap":
        await RUNNER.adb("-s", SERIAL, "shell", "input", "tap", str(int(m["x"])), str(int(m["y"])))
    elif m["t"] == "swipe":
        await RUNNER.adb("-s", SERIAL, "shell", "input", "swipe", *(str(int(m[k])) for k in ("x1", "y1", "x2", "y2")), "300")
    elif m["t"] == "key":
        from scrcpy_bridge import KEYS
        await RUNNER.adb("-s", SERIAL, "shell", "input", "keyevent", str(KEYS[m["k"]]))
    return web.json_response({"ok": True})


async def ws_screen(req):
    ws = web.WebSocketResponse(max_msg_size=1 << 20)
    await ws.prepare(req)
    try:
        await MIRROR.add(ws)
    except Exception as ex:
        await ws.send_str(json.dumps({"error": str(ex)}))
        await MIRROR.remove(ws)
        await ws.close()
        return ws
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                m = json.loads(msg.data)
                if m.get("t") == "keyframe":  # the viewer's decoder fell behind: watching needs no take-over
                    MIRROR.want_key()
                elif RUNNER.paused:  # the phone is yours only while taken over
                    touch()
                    await MIRROR.handle(m)
    finally:
        await MIRROR.remove(ws)
    return ws


async def on_start(app):
    app["runner"] = asyncio.create_task(RUNNER.loop())


async def on_stop(app):
    app["runner"].cancel()
    await MIRROR.stop()


def single_instance() -> None:
    """Two engines would run every job twice (double posts). Hold an exclusive lock for the process lifetime."""
    import fcntl
    global _LOCK
    _LOCK = open(ROOT / ".engine.lock", "w")
    try:
        fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another engine is already running here: exiting", flush=True)
        sys.exit(1)
    _LOCK.write(str(os.getpid()))
    _LOCK.flush()


def main() -> None:
    single_instance()
    LOGS.mkdir(exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    db()
    pgid = get_state("job_pgid")  # the engine died mid-job: its job process may still be driving the phone
    if pgid:
        try:
            os.killpg(int(pgid), signal.SIGKILL)
            print(f"stopped an orphaned job process group {pgid}", flush=True)
        except (ProcessLookupError, PermissionError):
            pass
        set_state("job_pgid", None)
    con = db()  # a job left "running" by a crash: retry it, except a post (it may already be out: never post twice)
    for j in con.execute("select * from jobs where status='running'").fetchall():
        if j["kind"] == "post":
            con.execute("update jobs set status='unverified', error='engine restarted mid-post: check the profile' where id=?", (j["id"],))
            alert("post", f"engine restarted during a post ({j['app']} @{j['account']} '{j['label']}'): check the profile", every_min=0)
        else:
            con.execute("update jobs set status='waiting', error='engine restarted mid-job' where id=?", (j["id"],))
    con.commit()
    gate = access.Gate(CFG["remote"]) if CFG.get("remote", {}).get("pin") else None
    app = web.Application(middlewares=[access.middleware(gate)])
    app.add_routes([
        web.get("/", page),
        web.get("/api/state", api_state),
        web.get("/api/results", api_results),
        web.post("/api/takeover", api_takeover),
        web.post("/api/resume", api_resume),
        web.post("/api/job/{id}/{action:retry|skip|now}", api_job_action),
        web.get("/api/job/{id}/log", api_log),
        web.post("/api/alerts/seen", api_alerts_seen),
        web.get("/shots/{name}", shot),
        web.get("/api/screen.png", screen_png),
        web.post("/api/input", screen_input),
        web.get("/ws/screen", ws_screen),
    ])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    asyncio.run(serve(app, gate))


async def serve(app, gate) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    host = CFG["dashboard"]["host"]
    await web.TCPSite(runner, host, CFG["dashboard"]["port"]).start()
    print(f"dashboard: http://{host}:{CFG['dashboard']['port']}", flush=True)
    if gate:  # cloudflared sends https://dashboard.zulfkhar.dev here; every request needs the PIN cookie
        await web.TCPSite(runner, "127.0.0.1", gate.port).start()
        print(f"remote dashboard (PIN): 127.0.0.1:{gate.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()
