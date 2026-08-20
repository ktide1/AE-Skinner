"""Find every After Effects install on the machine and describe what it exposes."""
from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

from . import pe
from .util import slug

# Native UI theme colourising was removed from the AE UI toolkit in the 2025
# line. The resources still exist in those builds; AE just ignores their hue.
FIRST_UNTHEMABLE_MAJOR = 25


@dataclass
class Install:
    root: Path
    support: Path
    exe: Path
    label: str
    version: str | None = None          # "24.6.1" from the exe's VERSIONINFO
    year: str | None = None             # "2024" / "Beta" from the folder name
    is_beta: bool = False
    prefs_dir: Path | None = None
    sounds_dir: Path | None = None
    binaries: dict[str, Path] = field(default_factory=dict)

    @property
    def major(self) -> int | None:
        if not self.version:
            return None
        try:
            return int(self.version.split(".")[0])
        except ValueError:
            return None

    @property
    def id(self) -> str:
        return slug(f"{self.label}-{self.version or 'x'}")

    @property
    def themable(self) -> bool:
        """False for AE 2025+, where the app ignores the theme resources' hue."""
        major = self.major
        return major is None or major < FIRST_UNTHEMABLE_MAJOR

    def theme_note(self) -> str:
        if self.themable:
            return "native UI theming supported"
        return f"AE {self.major}+ ignores UI theme colour (splash/about/sounds still work)"

    def describe(self) -> str:
        return f"{self.label}  [{self.version or '?'}]  {self.root}"


def _fixed_drives() -> list[Path]:
    drives = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                drives.append(root)
        except OSError:
            continue
    return drives


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for drive in _fixed_drives():
        roots.append(drive / "Adobe")
        roots.append(drive / "Program Files" / "Adobe")
        roots.append(drive / "Program Files (x86)" / "Adobe")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value) / "Adobe")
    seen: dict[str, Path] = {}
    for root in roots:
        seen.setdefault(str(root).lower(), root)
    return list(seen.values())


def _registry_roots() -> list[Path]:
    found: list[Path] = []
    try:
        import winreg
    except ImportError:
        return found
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for base in (r"SOFTWARE\Adobe\After Effects", r"SOFTWARE\WOW6432Node\Adobe\After Effects"):
            try:
                key = winreg.OpenKey(hive, base)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, sub) as child:
                            path, _ = winreg.QueryValueEx(child, "InstallPath")
                    except OSError:
                        continue
                    if path:
                        found.append(Path(path))
    return found


def _prefs_dir(is_beta: bool, version: str | None) -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "Adobe" / ("After Effects (Beta)" if is_beta else "After Effects")
    if not base.is_dir():
        return None
    versions = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    if not versions:
        return None
    if version:
        parts = version.split(".")
        exact = f"{parts[0]}.{parts[1]}" if len(parts) > 1 else parts[0]
        for candidate in versions:
            if candidate.name == exact:
                return candidate
        for candidate in versions:
            if candidate.name.startswith(parts[0] + "."):
                return candidate
    return versions[0]


def _make_install(root: Path) -> Install | None:
    root = Path(root)
    support = root / "Support Files"
    if not support.is_dir():
        # Some layouts point InstallPath straight at Support Files.
        if root.name.lower() == "support files":
            support, root = root, root.parent
        else:
            return None
    exes = sorted(support.glob("AfterFX*.exe"))
    exes = [e for e in exes if "render" not in e.stem.lower()] or exes
    if not exes:
        return None
    exe = exes[0]

    name = root.name
    is_beta = "beta" in name.lower()
    match = re.search(r"(20\d{2}|CC\s*20\d{2}|CC)", name)
    year = "Beta" if is_beta else (match.group(1) if match else None)

    version = pe.file_version(exe) or pe.file_version(support / "AfterFXLib.dll")
    label = f"After Effects {year}" if year else f"After Effects ({name})"
    if is_beta and version:
        label = f"After Effects Beta {version.rsplit('.', 1)[0]}"

    binaries = {}
    for candidate in ("AfterFXLib.dll", "dvaui.dll", "dvacore.dll"):
        path = support / candidate
        if path.exists():
            binaries[candidate] = path

    sounds = support / "sounds"
    return Install(
        root=root, support=support, exe=exe, label=label, version=version,
        year=year, is_beta=is_beta,
        prefs_dir=_prefs_dir(is_beta, version),
        sounds_dir=sounds if sounds.is_dir() else None,
        binaries=binaries,
    )


def find_installs() -> list[Install]:
    """Every AE install we can prove exists, newest-looking first."""
    candidates: list[Path] = list(_registry_roots())
    for root in _candidate_roots():
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and "after effects" in entry.name.lower():
                candidates.append(entry)

    installs: dict[str, Install] = {}
    for candidate in candidates:
        try:
            install = _make_install(candidate)
        except Exception:
            continue
        if install is None:
            continue
        installs.setdefault(str(install.support).lower(), install)

    def sort_key(item: Install):
        return (item.major or 0, item.label)

    return sorted(installs.values(), key=sort_key, reverse=True)


def resolve(selector: str | None) -> list[Install]:
    """Turn a CLI selector ('all', a year, a label fragment, a path) into installs."""
    installs = find_installs()
    if not selector or selector.lower() == "all":
        return installs
    path = Path(selector)
    if path.exists():
        one = _make_install(path)
        return [one] if one else []
    needle = selector.lower()
    matches = [i for i in installs if needle in i.label.lower()
               or needle == (i.year or "").lower()
               or needle == i.id
               or needle in str(i.root).lower()]
    return matches
