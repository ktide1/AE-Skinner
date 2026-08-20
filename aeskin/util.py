"""Small shared helpers: colour maths, hashing, process and privilege checks."""
from __future__ import annotations

import colorsys
import csv
import ctypes
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# --------------------------------------------------------------------------- colour

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(round(c)))) for c in rgb))


def hex_to_hsv(value: str) -> tuple[float, float, float]:
    """Return (hue in degrees, saturation 0-1, value 0-1)."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in hex_to_rgb(value)))
    return h * 360.0, s, v


def hsv_to_hex(h_deg: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb((h_deg % 360.0) / 360.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return rgb_to_hex((r * 255, g * 255, b * 255))


def hsv_attrs(value: str, precision: tuple[int, int, int] = (2, 4, 4)) -> str:
    """Render a hex colour as the h/s/v attribute triple AE's theme XML uses."""
    h, s, v = hex_to_hsv(value)
    ph, ps, pv = precision
    return f'h="{h:.{ph}f}" s="{s:.{ps}f}" v="{v:.{pv}f}"'


def resample_ramp(ramp: list[str], count: int) -> list[str]:
    """Stretch or squeeze a hex ramp to exactly `count` stops (linear in RGB).

    AE's grey ramps have a different number of stops in different versions, so
    a theme file declares one ramp and we fit it to whatever the install wants.
    """
    if count <= 0:
        return []
    if not ramp:
        raise ValueError("empty ramp")
    if count == 1:
        return [ramp[len(ramp) // 2]]
    if len(ramp) == 1:
        return [ramp[0]] * count
    stops = [hex_to_rgb(c) for c in ramp]
    out: list[str] = []
    last = len(stops) - 1
    for i in range(count):
        pos = i * last / (count - 1)
        lo = int(pos)
        hi = min(lo + 1, last)
        frac = pos - lo
        blended = tuple(stops[lo][c] + (stops[hi][c] - stops[lo][c]) * frac for c in range(3))
        out.append(rgb_to_hex(blended))  # type: ignore[arg-type]
    return out


def is_neutral(r: int, g: int, b: int, tolerance: int = 14) -> bool:
    return max(r, g, b) - min(r, g, b) <= tolerance


# --------------------------------------------------------------------------- files

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-").lower() or "unnamed"


def is_writable(path: Path) -> bool:
    """True when we can actually replace this file (Program Files needs admin)."""
    path = Path(path)
    target = path if path.exists() else path.parent
    if target.is_dir():
        try:
            probe = Path(tempfile.mkstemp(dir=target)[1])
        except OSError:
            return False
        probe.unlink(missing_ok=True)
        return True
    try:
        with path.open("r+b"):
            return True
    except OSError:
        return False


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- processes

def running_afterfx() -> list[tuple[str, str]]:
    """[(image name, pid)] for every live AfterFX* process."""
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [
        (row[0], row[1])
        for row in csv.reader(io.StringIO(result.stdout))
        if len(row) >= 2 and row[0].lower().startswith("afterfx")
    ]


def find_ffmpeg() -> str | None:
    from shutil import which
    found = which("ffmpeg")
    if found:
        return found
    for candidate in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


# --------------------------------------------------------------------------- resource roots

def app_root() -> Path:
    """The folder the user sees: next to the .exe when frozen, else the project."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_root() -> Path:
    """Read-only files packed inside a PyInstaller build."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else app_root()


def theme_dirs():
    """Where themes can live. A themes/ folder beside the exe wins over the bundle."""
    dirs = []
    for candidate in (bundled_root() / "themes", app_root() / "themes"):
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def list_themes():
    """{name: path} for every theme preset, later directories overriding earlier."""
    found = {}
    for directory in theme_dirs():
        for path in sorted(directory.glob("*.json")):
            found[path.stem] = path
    return found


def find_theme(name):
    return list_themes().get(str(name))


def pretty_path(path) -> str:
    """Collapse a path back to its environment variable for display.

    Keeps a username out of the UI, logs, and any screenshot someone posts.
    """
    text = str(path)
    for variable in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        value = os.environ.get(variable)
        if value and text.lower().startswith(value.lower()):
            return "%" + variable + "%" + text[len(value):]
    return text
