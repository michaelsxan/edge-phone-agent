"""The plan validator is the engine's guard against a bad nightly plan: test what it lets through and what it drops."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from plan_check import check_plan

TODAY = date(2026, 11, 3)
CFG = {"accounts": {
    "instagram": {"tinyloveshows": {"use": "noswipe", "posts_per_day": 4, "likes_per_day": 20, "comments_per_day": 5},
                  "nomoreawkwardness": {"use": "practice", "posts_per_day": 3}},
    "tiktok": {"nomoreawkwardness": {"use": "practice", "posts_per_day": 3}},
}}


@pytest.fixture(scope="session")
def folder(tmp_path_factory) -> Path:
    """A plan folder with a real vertical 4 s video (ffprobe checks it) and a horizontal one."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    d = tmp_path_factory.mktemp("plan")
    (d / "videos").mkdir()
    for name, size in (("v.mp4", "360x640"), ("wide.mp4", "640x360")):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30", "-t", "4",
                        "-b:v", "2M", "-pix_fmt", "yuv420p", str(d / "videos" / name)], check=True)
    return d


def post(**kw) -> dict:
    p = {"id": "s-1-a", "at": "13:11", "video": "videos/v.mp4", "caption": "A stranger leaves me clues. Part 1 #story",
         "targets": [{"app": "instagram", "account": "tinyloveshows", "audience": "everyone"}]}
    p.update(kw)
    return p


def run(folder, posts=None, existing=frozenset(), posted=None, **plan):
    plan = {"day": "2026-11-03", "posts": posts if posts is not None else [post()], **plan}
    return check_plan(plan, folder, CFG, set(existing), posted or {}, TODAY)


def test_valid_plan_passes(folder):
    clean, problems = run(folder)
    assert problems == []
    assert [p["id"] for p in clean["posts"]] == ["s-1-a"]


@pytest.mark.parametrize("caption", ["Emoji here \U0001F496 nope", "Curly ’quote’ caption", "two\nlines here", "short"])
def test_untypeable_or_short_captions_are_dropped(folder, caption):
    clean, problems = run(folder, [post(caption=caption)])
    assert clean["posts"] == [] and len(problems) == 1


def test_caption_key_must_be_new(folder):
    key = "a stranger leaves me clues. pa"  # first 30 characters, lowercased
    clean, problems = run(folder, existing={key})
    assert clean["posts"] == [] and "already used" in problems[0]
    clean, problems = run(folder, [post(), post(id="s-1-b")])  # same caption twice in one plan
    assert [p["id"] for p in clean["posts"]] == ["s-1-a"] and "already used" in problems[0]


def test_video_must_exist_inside_the_folder_and_be_vertical(folder):
    for video in ("videos/missing.mp4", "../plan/videos/v.mp4/../../../etc/passwd", "videos/wide.mp4"):
        clean, problems = run(folder, [post(video=video)])
        assert clean["posts"] == [], video


@pytest.mark.parametrize("timing", [{"at": "08:30"}, {"at": "23:30"}, {"at": "7pm"}, {"at": None, "round": 14}])
def test_bad_times_are_dropped(folder, timing):
    clean, _ = run(folder, [post(**timing)])
    assert clean["posts"] == []


def test_round_is_accepted(folder):
    clean, problems = run(folder, [post(at=None, round=16)])
    assert problems == [] and len(clean["posts"]) == 1


def test_practice_accounts_only_in_practice_plans(folder):
    t = [{"app": "instagram", "account": "nomoreawkwardness", "audience": "everyone"}]
    clean, problems = run(folder, [post(targets=t)])
    assert clean["posts"] == [] and "practice" in problems[0]
    clean, problems = run(folder, [post(targets=t)], practice=True)
    assert problems == [] and len(clean["posts"]) == 1


def test_unknown_account_is_dropped(folder):
    clean, problems = run(folder, [post(targets=[{"app": "instagram", "account": "someoneelse"}])])
    assert clean["posts"] == [] and "not in config.toml" in problems[0]


def test_daily_post_limit(folder):
    posts = [post(id=f"p{i}", caption=f"Caption number {i} is long enough #tag") for i in range(3)]
    clean, problems = run(folder, posts, posted={("instagram", "tinyloveshows"): 2})
    assert len(clean["posts"]) == 2 and "daily limit" in problems[0]


@pytest.mark.parametrize("day", ["2026-11-02", "2026-11-09", "tomorrow"])
def test_plan_day_must_be_today_or_soon(folder, day):
    clean, problems = run(folder, day=day)
    assert clean is None and problems


def test_deletes_only_in_practice_plans(folder):
    d = [{"app": "instagram", "account": "nomoreawkwardness", "caption": "x", "at": "12:00"}]
    clean, problems = run(folder, deletes=d)
    assert clean["deletes"] == [] and "only practice plans" in problems[0]
    clean, _ = run(folder, deletes=d, practice=True)
    assert clean["deletes"] == d


def test_research_is_validated_and_capped(folder):
    r = [{"app": "instagram", "account": "tinyloveshows", "keyword": "swipe fatigue", "minutes": 12, "max_likes": 99,
          "max_comments": 9},
         {"app": "instagram", "account": "tinyloveshows", "keyword": "\U0001F496", "minutes": 12},
         {"app": "instagram", "account": "tinyloveshows", "keyword": "too long", "minutes": 90}]
    clean, problems = run(folder, research=r)
    assert len(clean["research"]) == 1 and len(problems) == 2
    assert clean["research"][0]["max_likes"] == 20 and clean["research"][0]["max_comments"] == 5
