"""Self-update (the CD half of CI/CD): the engine pulls new code itself, so nothing ever needs access to this box.

CI on GitHub tests every push to main and, when the tests pass, moves the `deploy` branch to that commit. Every
[update].every_min minutes the engine fetches origin/<branch>; when it moved, and only while the runner is idle with
no post, delete or stats read due within [update].quiet_min minutes, it:
  1. fast-forwards this checkout (never when the checkout has local edits or has diverged: alert instead),
  2. runs `uv sync` if the dependencies changed,
  3. byte-compiles every module; on failure it resets to the previous commit and alerts,
  4. returns True: the engine then restarts itself (os.execv) on the new code.

    [update]
    branch = "deploy"     # the branch CI moves after green tests
    every_min = 15
    quiet_min = 20
"""

from __future__ import annotations

import compileall
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent
_last_check = 0.0


def check(cfg: dict, idle: bool, next_fixed_job: float | None, alert, root: Path = ROOT) -> bool:
    """True when the code was updated and the engine must restart."""
    global _last_check

    def git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)

    u = cfg.get("update") or {}
    if not u or not (root / ".git").exists():
        return False
    now = time.time()
    if now - _last_check < u.get("every_min", 15) * 60:
        return False
    if not idle or (next_fixed_job is not None and next_fixed_job - now < u.get("quiet_min", 20) * 60):
        return False  # try again on a later pass, not every_min later
    _last_check = now
    branch = u.get("branch", "deploy")
    if git("fetch", "-q", "origin", branch).returncode != 0:
        return False  # offline: keep running the current code
    head = git("rev-parse", "HEAD").stdout.strip()
    target = git("rev-parse", f"origin/{branch}").stdout.strip()
    if not target or head == target:
        return False
    if git("merge-base", "--is-ancestor", "HEAD", target).returncode != 0:
        alert("update", f"origin/{branch} is not ahead of this checkout (local commits?): not updating", every_min=24 * 60)
        return False
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        alert("update", "this checkout has local edits: not updating until they are committed or discarded",
              every_min=24 * 60)
        return False
    changed = git("diff", "--name-only", head, target).stdout.split()
    if git("merge", "-q", "--ff-only", target).returncode != 0:
        alert("update", f"could not fast-forward to {target[:7]}", every_min=0)
        return False
    if {"uv.lock", "pyproject.toml"} & set(changed):
        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
        r = subprocess.run([uv, "sync", "--frozen"], cwd=root, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            git("reset", "-q", "--hard", head)
            subprocess.run([uv, "sync", "--frozen"], cwd=root, capture_output=True, timeout=600)
            alert("update", f"uv sync failed for {target[:7]}; stayed on {head[:7]}: {r.stderr.strip()[-200:]}", every_min=0)
            return False
    if not compileall.compile_dir(root, quiet=1, rx=__import__("re").compile(r"[/\\]\.(venv|git)[/\\]")):
        git("reset", "-q", "--hard", head)
        alert("update", f"{target[:7]} does not compile; stayed on {head[:7]}", every_min=0)
        return False
    subject = git("log", "-1", "--format=%s", target).stdout.strip()
    alert("update", f"updated {head[:7]} -> {target[:7]} ({subject}); restarting", every_min=0)
    return True
