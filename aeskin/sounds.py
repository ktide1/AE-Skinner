"""Swap the WAV files After Effects plays when a render finishes or fails."""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

from .util import find_ffmpeg

TARGET_RATE = 44100
TARGET_CHANNELS = 2
TARGET_WIDTH = 2  # bytes per sample (16-bit PCM)


def discover(install) -> list:
    """The WAVs this install actually ships, e.g. rnd_okay.wav / rnd_fail.wav."""
    if not install.sounds_dir or not install.sounds_dir.is_dir():
        return []
    return sorted(install.sounds_dir.glob("*.wav"))


def describe(path: Path) -> str:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            return "%d Hz  %dch  %d-bit  %.2fs" % (
                rate, handle.getnchannels(), handle.getsampwidth() * 8, frames / rate,
            )
    except Exception:
        return "not a readable PCM WAV"


def is_compatible(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as handle:
            return (
                handle.getcomptype() == "NONE"
                and handle.getsampwidth() == TARGET_WIDTH
                and handle.getnchannels() in (1, 2)
            )
    except Exception:
        return False


def convert(source: Path, dest: Path, log=print) -> Path:
    """Normalise any audio file to the PCM WAV shape AE is happy with.

    ffmpeg does the job properly. Without it we can still pass through a plain
    16-bit PCM WAV unchanged, which covers most files people drop in.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(source),
             "-ac", str(TARGET_CHANNELS), "-ar", str(TARGET_RATE),
             "-sample_fmt", "s%d" % (TARGET_WIDTH * 8), str(dest)],
            check=True, capture_output=True,
        )
        log("    converted with ffmpeg -> %s (%d bytes)" % (dest.name, dest.stat().st_size))
        return dest
    if source.suffix.lower() == ".wav" and is_compatible(source):
        shutil.copy2(source, dest)
        log("    ffmpeg not found; copied compatible PCM WAV as-is")
        return dest
    raise RuntimeError(
        "ffmpeg is not installed and %s is not a 16-bit PCM WAV. "
        "Install ffmpeg or supply a 16-bit PCM .wav." % source.name
    )


def apply(install, source: Path, targets=None, work_dir: Path | None = None, log=print) -> list:
    """Write `source` over the chosen sound files. Returns the paths written."""
    available = discover(install)
    if not available:
        raise RuntimeError("this install has no Support Files\\sounds folder")
    wanted = set(n.lower() for n in targets) if targets else None
    chosen = [p for p in available if wanted is None or p.name.lower() in wanted]
    if not chosen:
        raise RuntimeError(
            "none of %s exist here; available: %s"
            % (sorted(wanted or []), ", ".join(p.name for p in available))
        )

    staging = (work_dir or Path.home() / ".aeskinner") / "converted.wav"
    converted = convert(Path(source), staging, log=log)

    written = []
    for target in chosen:
        shutil.copy2(converted, target)
        log("    wrote %s" % target)
        written.append(target)
    return written
