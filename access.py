"""PIN gate for the remote dashboard (https://dashboard.zulfkhar.dev through Cloudflare Tunnel).

The dashboard listens on two local ports:
  [dashboard].port  (8080)  only for this box itself, no PIN (http://127.0.0.1:8080)
  [remote].port     (8081)  where cloudflared sends the internet traffic: every request needs the PIN cookie

Typing the right PIN sets a signed cookie good for [remote].days days. The signing key is derived from the PIN, so
changing the PIN in config.toml (and restarting) logs everyone out. Wrong PINs are rate limited: after
[remote].max_tries wrong tries in 15 minutes the login is locked for everyone for 15 minutes.

    [remote]
    port = 8081
    pin = ""          # empty = remote dashboard off. Use 8+ characters; it guards the phone controls.
    days = 30
    max_tries = 10
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time

from aiohttp import web

COOKIE = "ce_session"
WINDOW = 15 * 60

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>content engine</title>
<style>body{{font:16px system-ui;background:#111;color:#eee;display:grid;place-items:center;height:100vh;margin:0}}
form{{display:grid;gap:12px;width:min(300px,85vw)}}input,button{{font:inherit;padding:12px;border-radius:8px;border:1px solid #444}}
input{{background:#222;color:#eee}}button{{background:#2d6cdf;color:#fff;border:0}}p{{color:#f77;margin:0}}</style>
<form method=post action=/login><input type=password name=pin placeholder=PIN autofocus autocomplete=current-password>
<button>Open dashboard</button>{msg}</form>"""


class Gate:
    def __init__(self, cfg: dict):
        self.port = int(cfg.get("port", 8081))
        self.pin = str(cfg["pin"])
        self.days = float(cfg.get("days", 30))
        self.max_tries = int(cfg.get("max_tries", 10))
        self.key = hashlib.sha256(b"content-engine-session:" + self.pin.encode()).digest()
        self.fails: list[float] = []

    def cookie(self, now: float | None = None) -> str:
        exp = str(int((now or time.time()) + self.days * 86400))
        return exp + "." + hmac.new(self.key, exp.encode(), "sha256").hexdigest()

    def valid(self, value: str | None, now: float | None = None) -> bool:
        if not value or "." not in value:
            return False
        exp, sig = value.split(".", 1)
        good = hmac.new(self.key, exp.encode(), "sha256").hexdigest()
        return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > (now or time.time())

    def locked(self, now: float) -> bool:
        self.fails = [t for t in self.fails if now - t < WINDOW]
        return len(self.fails) >= self.max_tries

    def try_pin(self, pin: str, now: float | None = None) -> str:
        """'ok', 'wrong' or 'locked'."""
        now = now or time.time()
        if self.locked(now):
            return "locked"
        if hmac.compare_digest(pin.encode(), self.pin.encode()):
            return "ok"
        self.fails.append(now)
        return "locked" if self.locked(now) else "wrong"


def local_port(req: web.Request) -> int | None:
    sock = req.transport.get_extra_info("sockname") if req.transport else None
    return sock[1] if sock else None


def login_page(msg: str = "", status: int = 200) -> web.Response:
    return web.Response(text=PAGE.format(msg=f"<p>{html.escape(msg)}</p>" if msg else ""), content_type="text/html",
                        status=status, headers={"Cache-Control": "no-store"})


def middleware(gate: Gate | None):
    @web.middleware
    async def check(req: web.Request, handler):
        if gate is None or local_port(req) != gate.port:
            return await handler(req)  # the local port: no PIN
        if req.path == "/login" and req.method == "POST":
            pin = str((await req.post()).get("pin", ""))
            result = gate.try_pin(pin)
            if result == "ok":
                r = web.HTTPSeeOther("/")
                r.set_cookie(COOKIE, gate.cookie(), max_age=int(gate.days * 86400), httponly=True, secure=True,
                             samesite="Strict")
                raise r
            msg = "Too many wrong tries. Wait 15 minutes." if result == "locked" else "Wrong PIN."
            return login_page(msg, 429 if result == "locked" else 401)
        if gate.valid(req.cookies.get(COOKIE)):
            return await handler(req)
        if req.path == "/" and req.method == "GET":
            return login_page()
        return web.Response(status=401, text="log in first")

    return check
