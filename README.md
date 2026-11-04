# content-engine

The box that runs noswipe's short-video accounts on a real Android phone: it posts the nightly plan to
Instagram/TikTok, reads each post's stats (1h 3h 6h 12h 24h 48h), does research sessions (search, watch,
like, comment), and serves a dashboard with a live phone screen. It runs on a Mac now and moves to a
Raspberry Pi later.

```
render server (noswipe-videos)                      this box (content-engine)                 phone
  nightly Claude run -> ~/outbox/<day>/  --pull-->  mailbox/inbox -> engine.db jobs  --adb-->  Instagram / TikTok
  ~/feedback/ (stats, research, jobs)   <--push--  stats.db research.db engine.db
```
The box always starts the connection (over Tailscale), so the server never needs access to it.

## Run
```bash
cp config.example.toml config.toml   # then fill in the phone serial, accounts, email (app password)
uv sync
uv run python engine.py              # dashboard: http://127.0.0.1:8080
```
`config.toml`, the databases, `mailbox/`, `logs/` and `runs/` are local only (gitignored).

## Pieces
| File | Does |
|---|---|
| `engine.py` | job runner (post, stats, research, delete, preflight), plan import, sync, alerts, dashboard |
| `plan_check.py` | validates a nightly plan before it becomes jobs (nothing in it is trusted) |
| `preflight.py` | 09:30 checks: phone, accounts logged in, disks, render server, today's plan |
| `instagram_upload.py`, `tiktok_upload.py` | post one video, checking every screen before tapping |
| `stats.py`, `research.py`, `delete.py`, `accounts.py` | stats reads, research sessions, test clean-up, account switching |
| `updater.py` | self-update from the `deploy` branch |
| `flows/` | how each app flow works, with lessons from failures |

## CI/CD
- **CI** (GitHub Actions, `.github/workflows/ci.yml`): every push and pull request compiles every module and runs
  `pytest` (plan validation, self-update).
- **CD**: when `main` is green, CI moves the `deploy` branch to it. The engine checks `origin/deploy` every 15
  minutes and fast-forwards only while idle with no post due within 20 minutes; it re-runs `uv sync` when the
  lock changed, refuses code that does not compile (and rolls back), never touches a checkout with local edits,
  emails what it did, and restarts itself.
- So: change code -> pull request (or push to `main`) -> green CI -> live on the box within about 15 minutes.
  Roll back with `git revert` on `main`.

UI flows can't be tested in CI (they need the phone). After changing one, run it once by hand on a practice
account (see `flows/`).
