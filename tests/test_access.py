"""The remote port must only serve someone who typed the PIN; the local port needs nothing."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import access

CFG = {"port": 0, "pin": "correct-horse", "days": 30, "max_tries": 3}


def test_cookie_is_signed_expires_and_dies_with_a_new_pin():
    g = access.Gate(CFG)
    c = g.cookie(now=1000)
    assert g.valid(c, now=1001)
    assert not g.valid(c, now=1000 + 31 * 86400)  # expired
    assert not g.valid(c.replace(c.split(".")[0], str(10**12)), now=1001)  # forged expiry
    assert not g.valid("garbage", now=1001) and not g.valid(None)
    assert not access.Gate({**CFG, "pin": "new-pin-123"}).valid(c, now=1001)  # PIN changed: logged out


def test_lockout_after_wrong_tries():
    g = access.Gate(CFG)
    assert [g.try_pin("x", now=100), g.try_pin("y", now=101)] == ["wrong", "wrong"]
    assert g.try_pin("z", now=102) == "locked"
    assert g.try_pin("correct-horse", now=103) == "locked"  # even the right PIN waits
    assert g.try_pin("correct-horse", now=102 + access.WINDOW) == "ok"


async def test_middleware():
    async def h(_req):
        return web.Response(text="SECRET")

    gate = access.Gate(CFG)
    app = web.Application(middlewares=[access.middleware(gate)])
    app.router.add_get("/", h)
    app.router.add_get("/api/state", h)
    async with TestClient(TestServer(app)) as c:
        gate.port = c.server.port  # the test server plays the remote port
        r = await c.get("/")
        assert r.status == 200 and "name=pin" in await r.text() and "SECRET" not in await r.text()
        assert (await c.get("/api/state")).status == 401
        assert (await c.post("/login", data={"pin": "nope"}, allow_redirects=False)).status == 401
        r = await c.post("/login", data={"pin": "correct-horse"}, allow_redirects=False)
        assert r.status == 303 and access.COOKIE in r.cookies
        cookie = r.cookies[access.COOKIE].value
        assert cookie and r.cookies[access.COOKIE]["httponly"]
        r = await c.get("/api/state", cookies={access.COOKIE: cookie})
        assert r.status == 200 and await r.text() == "SECRET"
        gate.port = 1  # now it is the local port: no PIN
        c.session.cookie_jar.clear()
        assert await (await c.get("/api/state")).text() == "SECRET"
