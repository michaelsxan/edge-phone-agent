"""Self-update: fast-forward to the deploy branch only when it is safe, roll back code that does not compile."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import updater

CFG = {"update": {"branch": "deploy", "every_min": 0, "quiet_min": 20}}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """origin (bare), a dev clone that pushes to deploy, and the box's checkout."""
    origin, dev, box = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "box"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "deploy", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(dev))
    for d in (dev,):
        sh(d, "git", "config", "user.email", "t@t"); sh(d, "git", "config", "user.name", "t")
        sh(d, "git", "checkout", "-q", "-b", "deploy")
    (dev / "a.py").write_text("x = 1\n")
    sh(dev, "git", "add", "."); sh(dev, "git", "commit", "-qm", "one"); sh(dev, "git", "push", "-q", "origin", "deploy")
    sh(tmp_path, "git", "clone", "-q", "-b", "deploy", str(origin), str(box))
    updater._last_check = 0
    return dev, box


def push(dev: Path, text: str, msg: str) -> str:
    (dev / "a.py").write_text(text)
    sh(dev, "git", "commit", "-qam", msg); sh(dev, "git", "push", "-q", "origin", "deploy")
    return sh(dev, "git", "rev-parse", "HEAD")


def test_updates_when_idle(repos):
    dev, box = repos
    new = push(dev, "x = 2\n", "two")
    alerts = []
    assert updater.check(CFG, True, None, lambda *a, **k: alerts.append(a), root=box)
    assert sh(box, "git", "rev-parse", "HEAD") == new and "restarting" in alerts[0][1]


def test_waits_while_busy_or_before_a_post(repos):
    import time
    dev, box = repos
    old = sh(box, "git", "rev-parse", "HEAD")
    push(dev, "x = 2\n", "two")
    assert not updater.check(CFG, False, None, lambda *a, **k: None, root=box)
    assert not updater.check(CFG, True, time.time() + 5 * 60, lambda *a, **k: None, root=box)
    assert sh(box, "git", "rev-parse", "HEAD") == old
    assert updater.check(CFG, True, time.time() + 60 * 60, lambda *a, **k: None, root=box)


def test_rolls_back_code_that_does_not_compile(repos):
    dev, box = repos
    old = sh(box, "git", "rev-parse", "HEAD")
    push(dev, "def broken(:\n", "bad")
    alerts = []
    assert not updater.check(CFG, True, None, lambda *a, **k: alerts.append(a), root=box)
    assert sh(box, "git", "rev-parse", "HEAD") == old and "does not compile" in alerts[0][1]


def test_never_overwrites_local_edits(repos):
    dev, box = repos
    old = sh(box, "git", "rev-parse", "HEAD")
    push(dev, "x = 2\n", "two")
    (box / "a.py").write_text("x = 'hand edit'\n")
    alerts = []
    assert not updater.check(CFG, True, None, lambda *a, **k: alerts.append(a), root=box)
    assert sh(box, "git", "rev-parse", "HEAD") == old and "local edits" in alerts[0][1]
    assert (box / "a.py").read_text() == "x = 'hand edit'\n"


def test_off_without_config(repos):
    dev, box = repos
    push(dev, "x = 2\n", "two")
    assert not updater.check({}, True, None, lambda *a, **k: None, root=box)
