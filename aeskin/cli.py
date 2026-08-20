"""Command line front end for AE Skinner."""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import backup, images, security, sounds, theming
from .apply import Job, build_plan, execute, preflight, preview
from .installs import find_installs, resolve
from .util import app_root, find_ffmpeg, is_admin, list_themes, pretty_path



def _log(message=""):
    print(message, flush=True)


# --------------------------------------------------------------------------- shared options

def _add_art_options(parser):
    group = parser.add_argument_group("artwork")
    group.add_argument("--image", metavar="FILE", help="use one image for both splash and about")
    group.add_argument("--splash", metavar="FILE", help="image for the startup splash")
    group.add_argument("--about", metavar="FILE", help="image for the About window")
    group.add_argument("--fit", choices=("cover", "contain", "stretch"), default="cover",
                       help="how the image fills each slot (default: cover)")
    group.add_argument("--focus", default="center",
                       help="crop bias: 'x,y' in 0..1 or left/right/top/bottom/center")
    group.add_argument("--background", default="#000000", help="letterbox colour for --fit contain")
    group.add_argument("--no-mask", action="store_true",
                       help="ignore AE's original alpha (square corners)")
    group.add_argument("--corner-radius", type=int, default=12,
                       help="fallback rounding when the slot has no alpha (default: 12)")
    group.add_argument("--include-small", action="store_true",
                       help="also replace icon-sized splash/about art")


def _add_text_options(parser):
    group = parser.add_argument_group("caption drawn over the artwork")
    group.add_argument("--title", default="")
    group.add_argument("--subtitle", default="")
    group.add_argument("--footer", default="")
    group.add_argument("--title-color", default="#FFFFFF")
    group.add_argument("--subtitle-color", default="#FFFFFF")
    group.add_argument("--footer-color", default="#B9AECF")
    group.add_argument("--text-align", choices=("left", "center", "right"), default="left")
    group.add_argument("--text-anchor", choices=("top", "middle", "bottom"), default="bottom")
    group.add_argument("--font", action="append", default=[],
                       help="ttf file or Windows font filename; repeatable")
    group.add_argument("--no-text-shadow", action="store_true")


def _add_theme_options(parser):
    group = parser.add_argument_group("UI theme")
    group.add_argument("--theme", metavar="NAME|FILE",
                       help="a themes/*.json preset or your own file")
    group.add_argument("--accent", help="override the accent colour, e.g. #7C5CFC")
    group.add_argument("--ramp", help="override the chrome ramp: comma separated hex, dark to light")
    group.add_argument("--themes", dest="theme_targets",
                       help="which AE themes to recolour (default: Darkest,Dark,Middark,Medium)")
    group.add_argument("--tint-strength", type=float, help="0..1 hue strength on greys (default 0.40)")
    group.add_argument("--no-prefs", action="store_true",
                       help="do not touch preferences or Debug Database.txt")
    group.add_argument("--no-dvaui", action="store_true", help="only patch AfterFXLib.dll")


def _add_sound_options(parser):
    group = parser.add_argument_group("sounds")
    group.add_argument("--sound", metavar="FILE", help="audio file for AE's render chimes")
    group.add_argument("--sound-target", action="append", default=[],
                       help="specific wav to overwrite, e.g. rnd_okay.wav; repeatable")


def _text_from(args, default_align=None):
    block = images.TextBlock(
        title=args.title, subtitle=args.subtitle, footer=args.footer,
        title_color=args.title_color, subtitle_color=args.subtitle_color,
        footer_color=args.footer_color,
        align=default_align or args.text_align, anchor=args.text_anchor,
        shadow=not args.no_text_shadow, font_files=list(args.font),
    )
    return None if block.is_empty() else block


def _theme_from(args):
    if not (args.theme or args.accent or args.ramp or args.theme_targets or args.tint_strength):
        return None
    theme = theming.Theme.load(args.theme) if args.theme else theming.Theme()
    if args.accent:
        theme.accent = args.accent
    if args.ramp:
        theme.ramp = [c.strip() for c in args.ramp.split(",") if c.strip()]
    if args.theme_targets:
        theme.themes = [c.strip() for c in args.theme_targets.split(",") if c.strip()]
    if args.tint_strength is not None:
        theme.tint_max_sat = args.tint_strength
    return theme


def _job_from(args, install):
    splash = args.splash or args.image
    about = args.about or args.image
    return Job(
        install=install,
        splash_image=Path(splash) if splash else None,
        about_image=Path(about) if about else None,
        fit=args.fit,
        focus=images.parse_focus(args.focus),
        keep_mask=not args.no_mask,
        corner_radius=args.corner_radius,
        text=_text_from(args),
        include_small=args.include_small,
        theme=_theme_from(args),
        theme_binaries=("AfterFXLib.dll",) if args.no_dvaui else ("AfterFXLib.dll", "dvaui.dll"),
        set_prefs=not args.no_prefs,
        sound=Path(args.sound) if args.sound else None,
        sound_targets=args.sound_target or None,
        note=getattr(args, "note", "") or "",
    )


def _pick(selector, allow_many=False):
    matches = resolve(selector)
    if not matches:
        raise SystemExit("no After Effects install matches %r. Run 'aeskin list'." % selector)
    if len(matches) > 1 and not allow_many:
        _log("that matches more than one install:")
        for install in matches:
            _log("   %s" % install.describe())
        raise SystemExit("narrow it down, or pass 'all'.")
    return matches


# --------------------------------------------------------------------------- commands

def cmd_list(args):
    installs = find_installs()
    if not installs:
        _log("No After Effects installs found.")
        return 1
    _log("After Effects installs:")
    for install in installs:
        flag = "theme OK " if install.themable else "no theme"
        _log("  [%s] %-26s %-9s %s" % (flag, install.label, install.version or "?", install.root))
        _log("           id=%s" % install.id)
    _log("")
    _log("'no theme' = AE 2025+ ignores UI theme colour. Splash, About and sounds still work.")
    return 0


def cmd_inspect(args):
    for install in _pick(args.install, allow_many=True):
        _log("=" * 72)
        _log(install.describe())
        _log("  version   %s   %s" % (install.version or "?", install.theme_note()))
        _log("  prefs     %s" % (install.prefs_dir or "not found"))
        lib = install.binaries.get("AfterFXLib.dll")
        if lib:
            slots = images.discover(lib, include_small=args.include_small)
            _log("  artwork   %d slot(s)" % len(slots))
            for slot in slots:
                _log("      %s" % slot.label())
            from .apply import _theme_resources
            names = [r.name for r in _theme_resources(lib)]
            _log("  theme     %d colour resource(s): %s" % (len(names), ", ".join(sorted(set(names)))))
        wavs = sounds.discover(install)
        _log("  sounds    %d file(s)" % len(wavs))
        for wav in wavs:
            _log("      %-16s %s" % (wav.name, sounds.describe(wav)))
        snapshots = backup.listing(install.id)
        _log("  backups   %d snapshot(s)" % len(snapshots))
    return 0


def cmd_apply(args):
    installs = _pick(args.install, allow_many=args.install and args.install.lower() == "all")
    failures = 0
    for install in installs:
        _log("=" * 72)
        _log(install.describe())
        job = _job_from(args, install)
        if not (job.splash_image or job.about_image or job.theme or job.sound):
            raise SystemExit("nothing requested. Give --image / --theme / --sound.")
        _log("  planning...")
        plan = build_plan(job, log=_log)
        for warning in plan.warnings:
            _log("  ! %s" % warning)
        if plan.is_empty():
            _log("  nothing to change here.")
            continue
        _log("  plan:")
        _log(plan.summary())
        if args.dry_run:
            problems = preflight(plan)
            for problem in problems:
                _log("  BLOCKED: %s" % problem)
            _log("  dry run -- nothing written.")
            continue
        if not args.yes:
            answer = input("  apply to %s? [y/N] " % install.label).strip().lower()
            if answer not in ("y", "yes"):
                _log("  skipped.")
                continue
        try:
            snapshot = execute(plan, log=_log)
            _log("  done. restore with:  aeskin restore %s" % install.id)
            _log("  snapshot: %s" % snapshot.directory)
        except Exception as error:
            failures += 1
            _log("  ERROR: %s" % error)
            if args.traceback:
                traceback.print_exc()
    return 1 if failures else 0


def cmd_preview(args):
    install = _pick(args.install)[0]
    job = _job_from(args, install)
    job.theme = None
    job.sound = None
    if not (job.splash_image or job.about_image):
        raise SystemExit("preview needs --image / --splash / --about")
    plan = build_plan(job, log=lambda *a: None)
    out = Path(args.out or (Path.home() / "AESkinner-preview"))
    _log("writing previews:")
    written = preview(plan, out, log=_log)
    _log("%d file(s) in %s" % (len(written), out))
    return 0


def cmd_backups(args):
    snapshots = backup.listing(args.install and _pick(args.install)[0].id)
    if not snapshots:
        _log("No snapshots yet.")
        return 0
    for snapshot in snapshots:
        _log("%-28s %s" % (snapshot.install_id, snapshot.describe()))
        if args.verbose:
            for record in snapshot.files:
                _log("      %s" % record["src"])
    _log("")
    _log("Store: %s" % pretty_path(backup.STORE))
    return 0


def cmd_restore(args):
    install = _pick(args.install)[0]
    snapshot = backup.resolve(install.id, args.snapshot)
    if not snapshot:
        raise SystemExit("no snapshot %r for %s" % (args.snapshot, install.label))
    from .util import running_afterfx
    running = running_afterfx()
    if running:
        raise SystemExit(
            "close After Effects first (%s)"
            % ", ".join("%s PID %s" % (n, p) for n, p in running)
        )
    _log("restoring %s into %s" % (snapshot.stamp, install.label))
    for record in snapshot.files:
        _log("  %s" % record["src"])
    if not args.yes:
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            return 0
    count = backup.restore(snapshot, log=_log,
                           allowed_roots=security.allowed_restore_roots([install]))
    _log("restored %d file(s)." % count)
    return 0


def cmd_themes(args):
    if args.export:
        theme = theming.Theme()
        theme.name = "My Theme"
        theme.save(Path(args.export))
        _log("wrote a starter theme to %s" % args.export)
        return 0
    presets = list_themes()
    if not presets:
        _log("no theme presets found")
        return 1
    for _name, path in sorted(presets.items()):
        try:
            theme = theming.Theme.load(path)
        except Exception as error:
            _log("  %-14s (unreadable: %s)" % (path.stem, error))
            continue
        _log("  %-14s accent %s  ramp %d stops  themes %s"
             % (path.stem, theme.accent, len(theme.ramp), ",".join(theme.themes)))
    _log("")
    _log("Use with:  aeskin apply <install> --theme <name>")
    return 0


def cmd_doctor(args):
    _log("AE Skinner environment")
    _log("  python      %s" % sys.version.split()[0])
    _log("  admin       %s" % ("yes" if is_admin() else "no (needed for C:\\Program Files installs)"))
    _log("  ffmpeg      %s" % (find_ffmpeg() or "not found (sound swaps need a 16-bit PCM wav)"))
    for module in ("pefile", "PIL", "tkinterdnd2"):
        try:
            __import__(module)
            _log("  %-11s ok" % module)
        except ImportError:
            note = "optional, enables drag and drop" if module == "tkinterdnd2" else "REQUIRED"
            _log("  %-11s missing (%s)" % (module, note))
    _log("  themes      %s" % ", ".join(sorted(list_themes())) or "none")
    _log("  backups     %s" % pretty_path(backup.STORE))
    _log("  installs    %d" % len(find_installs()))
    return 0


# --------------------------------------------------------------------------- entry

def build_parser():
    parser = argparse.ArgumentParser(
        prog="aeskin",
        description="Reskin any After Effects install: splash, About, UI theme and sounds.",
    )
    parser.add_argument("--traceback", action="store_true", help="show full tracebacks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="show every AE install found").set_defaults(func=cmd_list)

    inspect = subparsers.add_parser("inspect", help="show what an install exposes")
    inspect.add_argument("install", nargs="?", default="all")
    inspect.add_argument("--include-small", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    apply_cmd = subparsers.add_parser("apply", help="patch an install")
    apply_cmd.add_argument("install", help="year, label fragment, id, path, or 'all'")
    apply_cmd.add_argument("--dry-run", action="store_true")
    apply_cmd.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    apply_cmd.add_argument("--note", default="", help="label stored with the backup")
    _add_art_options(apply_cmd)
    _add_text_options(apply_cmd)
    _add_theme_options(apply_cmd)
    _add_sound_options(apply_cmd)
    apply_cmd.set_defaults(func=cmd_apply)

    preview_cmd = subparsers.add_parser("preview", help="render the artwork to PNGs, patch nothing")
    preview_cmd.add_argument("install")
    preview_cmd.add_argument("-o", "--out", help="output folder")
    _add_art_options(preview_cmd)
    _add_text_options(preview_cmd)
    preview_cmd.set_defaults(
        func=cmd_preview, theme=None, accent=None, ramp=None, theme_targets=None,
        tint_strength=None, no_prefs=True, no_dvaui=True, sound=None, sound_target=[],
    )

    backups_cmd = subparsers.add_parser("backups", help="list saved snapshots")
    backups_cmd.add_argument("install", nargs="?")
    backups_cmd.add_argument("-v", "--verbose", action="store_true")
    backups_cmd.set_defaults(func=cmd_backups)

    restore_cmd = subparsers.add_parser("restore", help="put an install back")
    restore_cmd.add_argument("install")
    restore_cmd.add_argument("--snapshot", default="first",
                             help="'first' (closest to stock), 'last', or a timestamp")
    restore_cmd.add_argument("-y", "--yes", action="store_true")
    restore_cmd.set_defaults(func=cmd_restore)

    themes_cmd = subparsers.add_parser("themes", help="list presets or export a starter file")
    themes_cmd.add_argument("--export", metavar="FILE")
    themes_cmd.set_defaults(func=cmd_themes)

    subparsers.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except SystemExit:
        raise
    except Exception as error:
        if getattr(args, "traceback", False):
            traceback.print_exc()
        print("error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
