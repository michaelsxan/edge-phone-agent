"""Record a reel playing on the phone (video + sound) and break it down for someone who cannot watch video.

    rec = Recorder(SERIAL, out_mp4, seconds=60); rec.start(); <tap the reel>; rec.mark(); ...; rec.wait()
    info = breakdown(out_mp4, rec.offset, out_dir, name)

breakdown() returns:
  length_s       how long one play of the reel is, found where the picture starts repeating (reels loop);
                 None when it did not repeat within the recording (then `min_length_s` says how long it is at least)
  strip          one image: a frame every second (every 0.5 s for reels under 12 s) of one play, left to right
  transcript     speech (and sung words) in one play, by whisper.cpp when installed; "" when silent / music only
  loudness_db    mean loudness, to tell silent reels from ones with sound

Sound: Instagram plays reels muted at media volume 0, so the volume goes up only while recording; scrcpy captures
the app's playback, and the phone's own speaker stays silent while it does.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

FPS = 4  # analysis frames per second for the loop search
COLOR = "setparams=colorspace=bt709:color_trc=bt709:color_primaries=bt709"  # scrcpy tags its stream oddly
CROP = "crop=iw*0.83:ih*0.69:0:ih*0.094"  # the video area without the side buttons and the caption
MODEL = Path(os.environ.get("WHISPER_MODEL", Path.home() / ".cache/whisper/ggml-base.bin"))


class Recorder:
    def __init__(self, serial: str, out: Path, seconds: int = 60):
        self.serial, self.out, self.seconds = serial, out, seconds
        self.proc: subprocess.Popen | None = None
        self.started = self.tapped = 0.0

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["scrcpy", "-s", self.serial, "--no-window", "--no-control", "--record", str(self.out),
             "--audio-source=playback", "--time-limit", str(self.seconds), "--max-size", "1600",
             "--video-bit-rate", "3M"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        # scrcpy's INFO lines go to stdout, which is block-buffered into a pipe; stderr is not. The muxer's
        # "[FFmpeg]" warning comes when the file gets its first packets: recording has started.
        end = time.time() + 20
        for line in self.proc.stderr:
            if "[FFmpeg]" in line or "Recording started" in line:
                self.started = time.time()
                return
            if time.time() > end or "ERROR" in line:
                break
        self.stop()
        raise RuntimeError("scrcpy did not start recording")

    def mark(self) -> None:
        """Call right after tapping the reel."""
        self.tapped = time.time()

    @property
    def offset(self) -> float:
        return max(0.0, self.tapped - self.started + 0.6)  # the reel screen appears ~0.6 s after the tap

    def wait(self) -> None:
        if self.proc:
            self.proc.wait(timeout=self.seconds + 30)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)


def gray_frames(mp4: Path, start: float) -> list[bytes]:
    w, h = 24, 44
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-i", str(mp4), "-vf",
                          f"{COLOR},{CROP},fps={FPS},scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, timeout=120).stdout
    return [raw[i:i + w * h] for i in range(0, len(raw) - w * h + 1, w * h)]


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def loop_length(frames: list[bytes], min_s: float = 2.5, overlap_s: float = 2.5) -> float | None:
    """Smallest period P (seconds) at which the picture repeats: frame t matches frame t+P all along."""
    n = len(frames)
    lo, hi = int(min_s * FPS), n - int(overlap_s * FPS)
    if hi <= lo:
        return None
    scores = {}
    for p in range(lo, hi + 1):
        pairs = range(0, n - p, 2)
        scores[p] = sum(diff(frames[t], frames[t + p]) for t in pairs) / len(pairs)
    typical = sorted(scores.values())[len(scores) // 2]
    if typical < 1.0:  # an almost still picture: no way to see a loop
        return None
    for p in range(lo, hi + 1):
        s = scores[p]
        if s < 0.4 * typical and s <= scores.get(p - 1, 1e9) and s <= scores.get(p + 1, 1e9):
            # a weaker repeat at half the period means we found two plays, not one
            half = [scores[h] for h in range(p // 2 - 1, p // 2 + 2) if h in scores]
            if half and min(half) < 0.6 * typical:
                return round((p // 2 - 1 + half.index(min(half))) / FPS, 1)
            return round(p / FPS, 1)
    return None


def loudness(mp4: Path, start: float, length: float) -> float | None:
    r = subprocess.run(["ffmpeg", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(mp4), "-af", "volumedetect",
                        "-vn", "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else None


def strip(mp4: Path, start: float, length: float, out: Path) -> int:
    step = 0.5 if length < 12 else 1.0
    n = max(1, min(60, int(length / step)))
    cols = 6 if n > 12 else min(n, 6)
    rows = math.ceil(n / cols)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start + 0.2:.2f}", "-t", f"{n * step:.2f}", "-i", str(mp4),
                    "-vf", f"{COLOR},fps={1 / step},scale=180:-1,tile={cols}x{rows}", "-frames:v", "1", "-q:v", "5",
                    str(out)], capture_output=True, timeout=120, check=True)
    return n


def transcribe(mp4: Path, start: float, length: float) -> str | None:
    """Words in one play of the reel, or None when whisper.cpp or its model is missing."""
    exe = shutil.which("whisper-cli") or next(
        (str(c) for c in (Path.home() / ".local/bin/whisper-cli", Path.home() / "tools/whisper.cpp/build/bin/whisper-cli")
         if c.exists()), None)
    if not exe or not MODEL.exists():
        return None
    wav = mp4.with_suffix(".wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(mp4),
                    "-vn", "-ac", "1", "-ar", "16000", str(wav)], capture_output=True, timeout=120, check=True)
    r = subprocess.run([exe, "-m", str(MODEL), "-f", str(wav), "-l", "auto", "-nt", "-np", "-ng"],  # -ng: CPU only (Metal crashes on Intel Macs)
                       capture_output=True, text=True, timeout=600)
    wav.unlink(missing_ok=True)
    text = " ".join(l.strip() for l in r.stdout.splitlines() if l.strip())
    return re.sub(r"\s*[\[(](music|Music|MUSIC|applause|silence|BLANK_AUDIO)[\])]\s*", " ", text).strip()


def duration(mp4: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
                       capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip() or 0)


def breakdown(mp4: Path, offset: float, out_dir: Path, name: str) -> dict:
    recorded = duration(mp4) - offset
    length = loop_length(gray_frames(mp4, offset))
    play = length or recorded
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / f"{name}-strip.jpg"
    n = strip(mp4, offset, play, sheet)
    db = loudness(mp4, offset, play)
    info = {"length_s": length, "strip": sheet.name, "strip_frames": n,
            "strip_step_s": 0.5 if play < 12 else 1.0, "loudness_db": db}
    if length is None:
        info["min_length_s"] = round(recorded - 2.5, 1)
    if db is not None and db > -60:
        info["transcript"] = transcribe(mp4, offset, play)
    else:
        info["transcript"] = ""  # silent
    return info
