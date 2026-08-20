"""Timestamped backups kept outside the Adobe folder.

Creative Cloud updates replace the binaries and can wipe anything sitting next
to them, so the store lives in %LOCALAPPDATA% and remembers absolute paths.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import security
from .util import sha256, slug

STORE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "AESkinner" / "backups"


@dataclass
class Snapshot:
    directory: Path
    stamp: str
    install_id: str
    install_root: str
    note: str
    files: list

    @property
    def created(self):
        try:
            return datetime.strptime(self.stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            return None

    def describe(self) -> str:
        when = self.created
        pretty = when.strftime("%Y-%m-%d %H:%M:%S") if when else self.stamp
        return "%s  %-22s %d file(s)  %s" % (self.stamp, pretty, len(self.files), self.note)


def _dir_for(install_id: str) -> Path:
    return STORE / slug(install_id)


def create(install_id: str, install_root, paths, note: str = "") -> Snapshot:
    """Copy every existing path into a new snapshot directory."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = _dir_for(install_id) / stamp
    directory.mkdir(parents=True, exist_ok=False)

    records = []
    used = set()
    for path in paths:
        path = Path(path)
        if not path.exists() or not path.is_file():
            continue
        stored = path.name
        index = 1
        while stored.lower() in used:
            stored = "%s__%d%s" % (path.stem, index, path.suffix)
            index += 1
        used.add(stored.lower())
        shutil.copy2(path, directory / stored)
        records.append({"src": str(path), "stored": stored, "sha256": sha256(path)})

    manifest = {
        "stamp": stamp,
        "install_id": install_id,
        "install_root": str(install_root),
        "note": note,
        "files": records,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return Snapshot(directory, stamp, install_id, str(install_root), note, records)


def _load(directory: Path):
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return Snapshot(
        directory=directory,
        stamp=data.get("stamp", directory.name),
        install_id=data.get("install_id", ""),
        install_root=data.get("install_root", ""),
        note=data.get("note", ""),
        files=data.get("files", []),
    )


def listing(install_id: str | None = None):
    """Snapshots, oldest first (so index 0 is the closest thing to stock)."""
    if not STORE.exists():
        return []
    roots = [_dir_for(install_id)] if install_id else [d for d in STORE.iterdir() if d.is_dir()]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if not directory.is_dir():
                continue
            snapshot = _load(directory)
            if snapshot:
                found.append(snapshot)
    found.sort(key=lambda s: s.stamp)
    return found


def resolve(install_id: str, selector: str = "first"):
    """'first' (stock-most), 'last' (newest), or an exact timestamp."""
    snapshots = listing(install_id)
    if not snapshots:
        return None
    key = (selector or "first").lower()
    if key in ("first", "original", "oldest", "stock"):
        return snapshots[0]
    if key in ("last", "latest", "newest"):
        return snapshots[-1]
    for snapshot in snapshots:
        if snapshot.stamp == selector:
            return snapshot
    return None


def restore(snapshot: Snapshot, log=print, allowed_roots=None) -> int:
    """Put a snapshot back.

    manifest.json holds absolute destinations. We wrote them, but it is plain
    JSON on disk, so every target is re-checked against the allowed roots and
    every stored file is re-hashed before it is copied anywhere.
    """
    restored = 0
    for record in snapshot.files:
        stored = snapshot.directory / record["stored"]
        target = Path(record["src"])

        if not stored.exists():
            log("    missing from snapshot: %s" % record["stored"])
            continue
        if allowed_roots is not None and not security.is_allowed_target(target, allowed_roots):
            log("    REFUSED (outside any known After Effects install): %s" % target)
            continue
        expected = record.get("sha256")
        if expected and sha256(stored) != expected:
            log("    REFUSED (snapshot file has been altered): %s" % record["stored"])
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stored, target)
        log("    restored %s" % target)
        restored += 1
    return restored


def prune(install_id: str, keep: int = 10) -> int:
    """Drop the oldest snapshots but always keep the very first one."""
    snapshots = listing(install_id)
    if len(snapshots) <= keep:
        return 0
    doomed = snapshots[1:-(keep - 1)] if keep > 1 else snapshots[1:]
    for snapshot in doomed:
        shutil.rmtree(snapshot.directory, ignore_errors=True)
    return len(doomed)
