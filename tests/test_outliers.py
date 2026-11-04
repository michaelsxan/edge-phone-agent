"""Grid tiles have no ids, so scrolled screens are stitched by their view-count texts."""

from __future__ import annotations

import outliers


def t(*raws):
    return [{"raw": r, "views": outliers.count(r)} for r in raws]


def test_merge_drops_the_overlap():
    got = outliers.merge(t("1", "22.4K", "3", "331K", "61K", "6"), t("331K", "61K", "6", "7", "8", "9"))
    assert [x["raw"] for x in got] == ["1", "22.4K", "3", "331K", "61K", "6", "7", "8", "9"]


def test_merge_without_overlap_appends():
    assert [x["raw"] for x in outliers.merge(t("1", "2"), t("3"))] == ["1", "2", "3"]


def test_merge_same_screen_adds_nothing():
    assert len(outliers.merge(t("1", "2", "3"), t("1", "2", "3"))) == 3


def test_counts():
    assert outliers.count("331K") == 331000 and outliers.count("1,886") == 1886 and outliers.count("2.5M") == 2500000


def test_loop_length_finds_one_play():
    import random

    import reel_record as rr
    rnd = random.Random(1)
    play = [bytes(rnd.randrange(256) for _ in range(64)) for _ in range(int(7.5 * rr.FPS))]  # a 7.5 s reel
    frames = (play * 4)[: 25 * rr.FPS]  # 25 s of it looping
    assert rr.loop_length(frames) == 7.5
    assert rr.loop_length([bytes(64)] * 100) is None  # a still picture: no loop to see
