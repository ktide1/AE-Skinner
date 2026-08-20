"""Plan and execute a skin: back up, patch, verify, roll back on any failure."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import backup, images, pe, security, sounds, theming
from .installs import Install
from .util import is_writable, running_afterfx, sha256

# Resources that hold UI colour. Matched by name, but only ever patched if the
# binary actually contains them.
THEME_NAME_HINTS = ("COLORTHEME", "COLORTHEMES", "COLOUR", "SPECTRUM")
THEME_RESOURCE_TYPES = {"XML", "JSON", "CSS", "TEXT"}
THEME_BINARIES = ("AfterFXLib.dll", "dvaui.dll")


@dataclass
class Job:
    install: Install
    splash_image: Path | None = None
    about_image: Path | None = None
    fit: str = "cover"
    focus: tuple = (0.5, 0.5)
    keep_mask: bool = True
    corner_radius: int | None = 12
    text: images.TextBlock | None = None
    about_text: images.TextBlock | None = None
    include_small: bool = False
    theme: theming.Theme | None = None
    theme_binaries: tuple = THEME_BINARIES
    set_prefs: bool = True
    sound: Path | None = None
    sound_targets: list | None = None
    note: str = ""


@dataclass
class Plan:
    job: Job
    resource_updates: dict = field(default_factory=dict)   # binary path -> [(Resource, bytes)]
    text_updates: list = field(default_factory=list)       # (path, new text)
    sound_source: Path | None = None
    sound_targets: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def touched_files(self):
        paths = list(self.resource_updates)
        paths += [p for p, _ in self.text_updates]
        paths += list(self.sound_targets)
        return paths

    def is_empty(self) -> bool:
        return not (self.resource_updates or self.text_updates or self.sound_targets)

    def summary(self) -> str:
        lines = []
        for path, updates in self.resource_updates.items():
            lines.append("  %s: %d resource(s)" % (Path(path).name, len(updates)))
        for path, _ in self.text_updates:
            lines.append("  %s: rewrite" % Path(path).name)
        for path in self.sound_targets:
            lines.append("  %s: replace" % Path(path).name)
        return "\n".join(lines) if lines else "  (nothing to do)"


# --------------------------------------------------------------------------- planning

def _theme_resources(binary: Path):
    for res in pe.iter_resources(binary, THEME_RESOURCE_TYPES):
        upper = res.name.upper()
        if any(hint in upper for hint in THEME_NAME_HINTS):
            yield res


def build_plan(job: Job, log=print) -> Plan:
    plan = Plan(job=job)
    install = job.install
    lib = install.binaries.get("AfterFXLib.dll")

    # --- artwork ------------------------------------------------------------
    if (job.splash_image or job.about_image) and lib:
        groups = []
        if job.splash_image:
            groups.append("splash")
        if job.about_image:
            groups.append("about")
        slots = images.discover(lib, include_small=job.include_small, groups=tuple(groups))
        if not slots:
            plan.warnings.append("no splash/about PNG resources found in %s" % lib.name)
        sources = {}
        if job.splash_image:
            sources["splash"] = images.load_source(Path(job.splash_image))
        if job.about_image:
            sources["about"] = images.load_source(Path(job.about_image))
        updates = plan.resource_updates.setdefault(lib, [])
        for slot in slots:
            source = sources.get(slot.group)
            if source is None:
                continue
            caption = job.text if slot.group == "splash" else (job.about_text or job.text)
            png = images.render_slot(
                slot, source, mode=job.fit, focus=job.focus,
                keep_mask=job.keep_mask, corner_radius=job.corner_radius,
                text=caption,
            )
            updates.append((slot.resource, png))
            log("    %-26s %dx%d  %d bytes" % (slot.resource.name, slot.width, slot.height, len(png)))

    # --- theme --------------------------------------------------------------
    if job.theme is not None:
        if not install.themable:
            plan.warnings.append(
                "%s ignores UI theme colour (%s) -- the resources will be patched but "
                "the panels stay grey" % (install.label, install.theme_note())
            )
        for name in job.theme_binaries:
            binary = install.binaries.get(name)
            if not binary:
                continue
            updates = plan.resource_updates.setdefault(binary, [])
            for res in _theme_resources(binary):
                patched, changes = theming.patch_blob(res.name, res.data, job.theme)
                if changes and patched != res.data:
                    updates.append((res, patched))
                    log("    %-14s %-26s %d change(s)" % (binary.name, res.name, changes))
            if not updates:
                plan.resource_updates.pop(binary, None)

        if job.set_prefs and install.prefs_dir and install.prefs_dir.is_dir():
            general = next(iter(install.prefs_dir.glob("*Prefs-indep-general.txt")), None)
            if general:
                text, changes = theming.patch_general_prefs(
                    general.read_text(encoding="utf-8", errors="replace"), job.theme
                )
                if changes:
                    plan.text_updates.append((general, text))
                    log("    prefs: %d key(s) in %s" % (changes, general.name))
            debug = install.prefs_dir / "Debug Database.txt"
            if debug.exists():
                text, _ = theming.patch_debug_database(
                    debug.read_text(encoding="utf-8", errors="replace")
                )
                plan.text_updates.append((debug, text))
                log("    prefs: Enable_Theme_Colorizing = true")
            else:
                plan.warnings.append(
                    "no 'Debug Database.txt' in %s -- launch AE once with Ctrl+Shift+Alt held "
                    "to create it, or theme colourising may stay off" % install.prefs_dir
                )

    # --- sounds -------------------------------------------------------------
    if job.sound:
        available = sounds.discover(install)
        if not available:
            plan.warnings.append("no sounds folder in this install")
        else:
            wanted = set(n.lower() for n in (job.sound_targets or []))
            chosen = [p for p in available if not wanted or p.name.lower() in wanted]
            if not chosen:
                plan.warnings.append(
                    "requested sounds not present; available: %s"
                    % ", ".join(p.name for p in available)
                )
            else:
                plan.sound_source = Path(job.sound)
                plan.sound_targets = chosen
                for path in chosen:
                    log("    sound -> %s" % path.name)

    return plan


# --------------------------------------------------------------------------- checks

def preflight(plan: Plan) -> list:
    """Blocking problems, as human-readable strings."""
    problems = []

    running = running_afterfx()
    if running:
        problems.append(
            "After Effects is running (%s). Save your work and close it first."
            % ", ".join("%s PID %s" % (name, pid) for name, pid in running)
        )

    # Never rewrite resources in something that is not Adobe's. This is what
    # stops a mis-detected path, or a hand-edited one, from corrupting an
    # unrelated DLL.
    for binary in plan.resource_updates:
        ok, why = security.is_after_effects_binary(binary)
        if not ok:
            problems.append(why)

    for path in plan.touched_files:
        if not is_writable(Path(path)):
            problems.append("cannot write %s -- run AE Skinner as administrator" % path)

    needed = 0
    for path in plan.touched_files:
        try:
            needed += Path(path).stat().st_size
        except OSError:
            continue
    headroom = int(needed * 2.5) + (64 << 20)
    if backup.STORE.exists() or backup.STORE.parent.exists():
        available = security.free_space(backup.STORE)
        if available and available < headroom:
            problems.append(
                "not enough disk space for a backup: %.0f MB free, about %.0f MB needed"
                % (available / 1e6, headroom / 1e6)
            )
    return problems


# --------------------------------------------------------------------------- execution

def _verify(plan: Plan, log=print) -> list:
    failures = []
    for binary, updates in plan.resource_updates.items():
        current = pe.read_resources(Path(binary), {u[0].type_name for u in updates})
        for res, expected in updates:
            got = current.get(res.key)
            if got is None:
                failures.append("%s: %s vanished" % (Path(binary).name, res.label()))
                continue
            data = got.data
            # Windows may pad a resource; a prefix match is still a match.
            if data != expected and not data.startswith(expected):
                failures.append(
                    "%s: %s did not take (%d bytes on disk vs %d written)"
                    % (Path(binary).name, res.label(), len(data), len(expected))
                )
        log("    verified %s (%d resources)" % (Path(binary).name, len(updates)))
    for path, expected in plan.text_updates:
        if Path(path).read_text(encoding="utf-8", errors="replace") != expected:
            failures.append("%s did not take" % Path(path).name)
    return failures


def execute(plan: Plan, log=print) -> backup.Snapshot:
    job = plan.job
    install = job.install

    problems = preflight(plan)
    if problems:
        raise RuntimeError("\n".join(problems))
    if plan.is_empty():
        raise RuntimeError("nothing to apply")

    snapshot = backup.create(
        install.id, install.root, plan.touched_files,
        note=job.note or "AE Skinner",
    )
    log("  backup -> %s" % snapshot.directory)

    try:
        for binary, updates in plan.resource_updates.items():
            log("  patching %s" % Path(binary).name)
            pe.write_resources(Path(binary), updates, log=log)
            pe.fix_checksum(Path(binary))
        for path, text in plan.text_updates:
            security.atomic_write_text(path, text)
            log("  wrote %s" % Path(path).name)
        if plan.sound_source and plan.sound_targets:
            log("  sounds")
            sounds.apply(
                install, plan.sound_source,
                targets=[p.name for p in plan.sound_targets],
                work_dir=snapshot.directory, log=log,
            )
        failures = _verify(plan, log=log)
        if failures:
            raise RuntimeError("verification failed:\n  " + "\n  ".join(failures))
    except Exception:
        log("  FAILED -- rolling back from %s" % snapshot.directory)
        backup.restore(snapshot, log=log,
                       allowed_roots=security.allowed_restore_roots([install]))
        raise

    for binary in plan.resource_updates:
        log("  %s sha256 %s" % (Path(binary).name, sha256(Path(binary))))
    return snapshot


def preview(plan: Plan, out_dir: Path, log=print) -> list:
    """Write the generated PNGs to a folder instead of into the binary."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for binary, updates in plan.resource_updates.items():
        for res, data in updates:
            if res.type_name != "PNG":
                continue
            path = out_dir / ("%s.png" % res.name)
            path.write_bytes(data)
            written.append(path)
            log("    %s" % path)
    return written
