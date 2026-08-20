"""AE Skinner - reskin any After Effects install.

Drop an image, pick colours, hit Apply. Everything touched is backed up first.

UI notes:
  Typography follows Microsoft's Windows type guidance -- Segoe UI Variable,
  and Semibold rather than Bold for emphasis.
  https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography

  The caption fields debounce before re-rendering. Tk fires a trace on every
  keystroke, and the recommended pattern for anything over ~50 ms of work is
  after_cancel() + after().
  https://copyprogramming.com/howto/tkinter-entry-on-change
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageTk  # noqa: E402

from aeskin import __version__, backup, images, security, sounds, theming  # noqa: E402
from aeskin.apply import Job, build_plan, execute, preflight  # noqa: E402
from aeskin.installs import find_installs  # noqa: E402
from aeskin.util import app_root, is_admin, list_themes, pretty_path  # noqa: E402

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:                                    # pragma: no cover - optional
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

APP_TITLE = "AE Skinner"
SETTINGS_PATH = backup.STORE.parent / "settings.json"

# Re-render the preview this long after the last keystroke, not on each one.
DEBOUNCE_MS = 200

# ---------------------------------------------------------------- palette
INK          = "#0D0A12"   # window
SURFACE      = "#15111C"   # panels
SURFACE_2    = "#1C1626"   # raised
FIELD        = "#221B2E"   # inputs and drop zones
LINE         = "#2E2440"   # hairlines
LINE_BRIGHT  = "#453363"
TEXT         = "#EFEAF7"
TEXT_DIM     = "#9E93B4"
TEXT_FAINT   = "#6F6684"
ACCENT       = "#7C5CFC"
ACCENT_HOVER = "#9B80FF"
ACCENT_INK   = "#0B0714"
WARN         = "#E8B44A"
BAD          = "#F0637A"
GOOD         = "#4CC38A"

# 4 px spacing scale
S1, S2, S3, S4, S5 = 4, 8, 12, 16, 24

FONT_UI = "Segoe UI Variable Text"
FONT_DISPLAY = "Segoe UI Variable Display"

IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff *.gif"), ("All files", "*.*")]
AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.aif *.aiff *.flac *.m4a *.ogg"), ("All files", "*.*")]

PAGES = ("Artwork", "Caption", "Interface", "Sounds", "About")


def font(size=9, weight="normal", display=False):
    """Semibold for emphasis; Microsoft's type guidance advises against Bold."""
    return (FONT_DISPLAY if display else FONT_UI, size, weight)


def _clean_dnd_path(raw: str) -> str:
    """tkdnd hands over '{C:\\path with spaces\\a.png}' style strings."""
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    return raw.split("} {")[0].strip("{}")


_checker_cache = {}


def checkerboard(size, light="#221B2E", dark="#1A1424", cell=8):
    """Alpha backdrop. Built from a 2-cell tile and cached, because this runs
    on every debounced preview render."""
    key = (size, light, dark, cell)
    cached = _checker_cache.get(key)
    if cached is not None:
        return cached
    tile = Image.new("RGBA", (cell * 2, cell * 2), light)
    dark_cell = Image.new("RGBA", (cell, cell), dark)
    tile.paste(dark_cell, (cell, 0))
    tile.paste(dark_cell, (0, cell))
    board = Image.new("RGBA", size)
    for y in range(0, size[1], cell * 2):
        for x in range(0, size[0], cell * 2):
            board.paste(tile, (x, y))
    if len(_checker_cache) > 6:
        _checker_cache.clear()
    _checker_cache[key] = board
    return board


# =========================================================================== widgets

class DropZone(tk.Frame):
    """Click-to-browse box that also accepts a dropped file."""

    def __init__(self, master, title, hint, filetypes, on_change=None):
        super().__init__(master, bg=SURFACE)
        self.filetypes = filetypes
        self.on_change = on_change
        self.path = None
        self.hint = hint

        self.box = tk.Frame(self, bg=FIELD, highlightthickness=1,
                            highlightbackground=LINE, cursor="hand2")
        self.box.pack(fill="x")

        inner = tk.Frame(self.box, bg=FIELD)
        inner.pack(fill="x", padx=S3, pady=S3)

        self.glyph = tk.Label(inner, text="+", bg=FIELD, fg=ACCENT, font=font(18, "normal"))
        self.glyph.pack(side="left", padx=(0, S3))

        stack = tk.Frame(inner, bg=FIELD)
        stack.pack(side="left", fill="x", expand=True)
        self.title_label = tk.Label(stack, text=title, bg=FIELD, fg=TEXT,
                                    font=font(9, "bold"), anchor="w")
        self.title_label.pack(fill="x")
        self.value_label = tk.Label(stack, text=hint, bg=FIELD, fg=TEXT_DIM,
                                    font=font(8), anchor="w", justify="left")
        self.value_label.pack(fill="x")

        self.clear_button = tk.Label(inner, text="\u2715", bg=FIELD, fg=TEXT_FAINT,
                                     font=font(9), cursor="hand2")
        self.clear_button.bind("<Button-1>", lambda e: (self.clear(), "break")[1])

        for widget in (self.box, inner, stack, self.glyph, self.title_label, self.value_label):
            widget.bind("<Button-1>", self._browse)
            widget.bind("<Enter>", lambda e: self._hover(True))
            widget.bind("<Leave>", lambda e: self._hover(False))
            if HAS_DND:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._dropped)
                widget.dnd_bind("<<DragEnter>>", lambda e: self._highlight(True))
                widget.dnd_bind("<<DragLeave>>", lambda e: self._highlight(False))

    def _hover(self, on):
        if not self.path:
            self.box.configure(highlightbackground=LINE_BRIGHT if on else LINE)

    def _highlight(self, on):
        self.box.configure(highlightbackground=ACCENT if on else (GOOD if self.path else LINE))

    def _dropped(self, event):
        self.set(_clean_dnd_path(event.data))

    def _browse(self, _event=None):
        chosen = filedialog.askopenfilename(filetypes=self.filetypes, title="Choose a file")
        if chosen:
            self.set(chosen)

    def set(self, path):
        if not path:
            return self.clear()
        self.path = Path(path)
        self.value_label.configure(text=self.path.name, fg=TEXT)
        self.glyph.configure(text="\u2713", fg=GOOD)
        self.box.configure(highlightbackground=GOOD)
        self.clear_button.pack(side="right")
        if self.on_change:
            self.on_change(self.path)

    def clear(self):
        self.path = None
        self.value_label.configure(text=self.hint, fg=TEXT_DIM)
        self.glyph.configure(text="+", fg=ACCENT)
        self.box.configure(highlightbackground=LINE)
        self.clear_button.pack_forget()
        if self.on_change:
            self.on_change(None)


class Swatch(tk.Frame):
    def __init__(self, master, label, initial, on_change=None, bg=SURFACE):
        super().__init__(master, bg=bg)
        self.value = initial
        self.on_change = on_change
        tk.Label(self, text=label, bg=bg, fg=TEXT_DIM, font=font(8)).pack(anchor="w")
        self.chip = tk.Label(self, text=initial.upper(), width=9, font=font(8, "bold"),
                             cursor="hand2", relief="flat", pady=S1 + 1,
                             highlightthickness=1, highlightbackground=LINE)
        self.chip.pack(anchor="w", pady=(2, 0))
        self.chip.bind("<Button-1>", lambda e: self._pick())
        self._paint()

    def _paint(self):
        rgb = tuple(int(self.value.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        self.chip.configure(bg=self.value, fg="#000000" if luma > 140 else "#FFFFFF",
                            text=self.value.upper())

    def _pick(self):
        chosen = colorchooser.askcolor(color=self.value, title="Pick a colour")[1]
        if chosen:
            self.set(chosen)

    def set(self, value):
        self.value = value
        self._paint()
        if self.on_change:
            self.on_change(value)


class Card(tk.Frame):
    """A titled panel."""

    def __init__(self, master, title, subtitle=None):
        super().__init__(master, bg=SURFACE, highlightthickness=1, highlightbackground=LINE)
        head = tk.Frame(self, bg=SURFACE)
        head.pack(fill="x", padx=S4, pady=(S3, 0))
        tk.Label(head, text=title, bg=SURFACE, fg=TEXT, font=font(10, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(head, text=subtitle, bg=SURFACE, fg=TEXT_DIM, font=font(8),
                     justify="left", wraplength=440).pack(anchor="w", pady=(2, 0))
        self.body = tk.Frame(self, bg=SURFACE)
        self.body.pack(fill="both", expand=True, padx=S4, pady=(S3, S4))


def labelled_combo(master, label, values, variable, width=14, command=None, bg=SURFACE):
    holder = tk.Frame(master, bg=bg)
    tk.Label(holder, text=label, bg=bg, fg=TEXT_DIM, font=font(8)).pack(anchor="w")
    combo = ttk.Combobox(holder, textvariable=variable, state="readonly",
                         values=values, width=width, font=font(9))
    combo.pack(anchor="w", pady=(2, 0))
    if command:
        combo.bind("<<ComboboxSelected>>", lambda e: command())
    return holder, combo


# =========================================================================== app

class App:
    def __init__(self, root):
        self.root = root
        self.installs = []
        self.messages = queue.Queue()
        self.busy = False
        self.preview_photo = None
        self._preview_job = None
        self._scan_generation = 0
        self._preview_generation = 0
        self._settings = self._load_settings()

        root.title("%s %s" % (APP_TITLE, __version__))
        root.configure(bg=INK)
        root.geometry("1200x900")
        root.minsize(1080, 780)
        self._set_icon()

        self._style()
        self._build()
        self.refresh_installs()
        self._apply_settings()
        self.root.after(60, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        root.bind("<F5>", lambda e: self.refresh_installs())
        root.bind("<Control-Return>", lambda e: self.on_apply())

    # ------------------------------------------------------------------ chrome

    def _set_icon(self):
        for candidate in (app_root() / "icon.ico", Path(__file__).parent / "icon.ico"):
            if candidate.exists():
                try:
                    self.root.iconbitmap(str(candidate))
                    return
                except Exception:
                    pass

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=SURFACE, foreground=TEXT, font=font(9))
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, arrowcolor=TEXT_DIM, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE, selectbackground=FIELD,
                        selectforeground=TEXT, padding=S1)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.font", font(9))
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                        insertcolor=TEXT, bordercolor=LINE, lightcolor=LINE,
                        darkcolor=LINE, padding=S1 + 1)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                        bordercolor=LINE, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Accent.Horizontal.TScale", background=SURFACE)

    def _button(self, master, text, command, kind="ghost", width=None):
        colours = {
            "primary": (ACCENT, ACCENT_INK, ACCENT_HOVER),
            "ghost": (SURFACE_2, TEXT, LINE_BRIGHT),
            "quiet": (SURFACE, TEXT_DIM, SURFACE_2),
        }[kind]
        bg, fg, hover = colours
        button = tk.Label(master, text=text, bg=bg, fg=fg, cursor="hand2",
                          font=font(9, "bold" if kind == "primary" else "normal"),
                          padx=S4, pady=S2 + (2 if kind == "primary" else 0))
        if width:
            button.configure(width=width)
        button._enabled = True
        button._bg = bg

        def enter(_):
            if button._enabled:
                button.configure(bg=hover)

        def leave(_):
            button.configure(bg=button._bg if button._enabled else SURFACE_2)

        def click(_):
            if button._enabled:
                command()

        button.bind("<Enter>", enter)
        button.bind("<Leave>", leave)
        button.bind("<Button-1>", click)
        return button

    @staticmethod
    def _set_enabled(button, enabled):
        button._enabled = enabled
        button.configure(bg=button._bg if enabled else SURFACE_2,
                         fg=(ACCENT_INK if button._bg == ACCENT else TEXT) if enabled else TEXT_FAINT,
                         cursor="hand2" if enabled else "arrow")

    # ------------------------------------------------------------------ layout

    def _build(self):
        self._build_header()

        body = tk.Frame(self.root, bg=INK)
        body.pack(fill="both", expand=True, padx=S5, pady=(0, S3))

        nav = tk.Frame(body, bg=INK, width=150)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self._build_nav(nav)

        self.pages_holder = tk.Frame(body, bg=INK)
        self.pages_holder.pack(side="left", fill="both", expand=True, padx=(S4, S4))

        side = tk.Frame(body, bg=INK, width=430)
        side.pack(side="right", fill="y")
        side.pack_propagate(False)
        self._build_side(side)

        self.pages = {}
        self.pages["Artwork"] = self._page_artwork()
        self.pages["Caption"] = self._page_caption()
        self.pages["Interface"] = self._page_interface()
        self.pages["Sounds"] = self._page_sounds()
        self.pages["About"] = self._page_about()
        self.show_page("Artwork")

        self._build_actions()
        self._build_status()
        self._build_log()

    def _build_header(self):
        header = tk.Frame(self.root, bg=INK)
        header.pack(fill="x", padx=S5, pady=(S4, S3))

        title_row = tk.Frame(header, bg=INK)
        title_row.pack(fill="x")
        tk.Label(title_row, text=APP_TITLE, bg=INK, fg=TEXT,
                 font=font(17, "bold", display=True)).pack(side="left")
        tk.Label(title_row, text=" " + __version__, bg=INK, fg=TEXT_FAINT,
                 font=font(9)).pack(side="left", pady=(8, 0))

        badges = tk.Frame(title_row, bg=INK)
        badges.pack(side="right", pady=(6, 0))
        self._badge(badges, "administrator" if is_admin() else "standard user",
                    GOOD if is_admin() else WARN)
        self._badge(badges, "drag and drop" if HAS_DND else "no drag and drop",
                    GOOD if HAS_DND else TEXT_FAINT)

        picker = tk.Frame(header, bg=INK)
        picker.pack(fill="x", pady=(S3, 0))
        tk.Label(picker, text="After Effects install", bg=INK, fg=TEXT_DIM,
                 font=font(8)).pack(anchor="w")
        row = tk.Frame(picker, bg=INK)
        row.pack(fill="x", pady=(2, 0))
        self.install_box = ttk.Combobox(row, state="readonly", font=font(9))
        self.install_box.pack(side="left", fill="x", expand=True)
        self.install_box.bind("<<ComboboxSelected>>", lambda e: self._install_changed())
        self._button(row, "Rescan", self.refresh_installs, "ghost").pack(side="left", padx=(S2, 0))

        self.install_note = tk.Label(header, text="", bg=INK, fg=TEXT_DIM,
                                     font=font(8), anchor="w", justify="left")
        self.install_note.pack(fill="x", pady=(S2, 0))

    @staticmethod
    def _badge(master, text, colour):
        chip = tk.Label(master, text=text, bg=SURFACE_2, fg=colour, font=font(8),
                        padx=S2, pady=2)
        chip.pack(side="left", padx=(S2, 0))
        return chip

    def _build_nav(self, master):
        self.nav_buttons = {}
        for name in PAGES:
            button = tk.Label(master, text=name, bg=INK, fg=TEXT_DIM, font=font(10),
                              anchor="w", padx=S3, pady=S2 + 2, cursor="hand2")
            button.pack(fill="x", pady=(0, 2))
            button.bind("<Button-1>", lambda e, n=name: self.show_page(n))
            button.bind("<Enter>", lambda e, b=button: b.configure(
                bg=SURFACE if self._current_page != b["text"] else SURFACE_2))
            button.bind("<Leave>", lambda e, b=button: b.configure(
                bg=INK if self._current_page != b["text"] else SURFACE_2))
            self.nav_buttons[name] = button
        self._current_page = None

    def show_page(self, name):
        self._current_page = name
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(bg=SURFACE_2 if active else INK,
                             fg=TEXT if active else TEXT_DIM,
                             font=font(10, "bold" if active else "normal"))
        for key, page in getattr(self, "pages", {}).items():
            page.pack_forget()
        if getattr(self, "pages", None):
            self.pages[name].pack(fill="both", expand=True)

    def _build_side(self, master):
        preview_card = Card(master, "Preview", "Exactly what the splash will look like.")
        preview_card.pack(fill="x")
        self.preview_label = tk.Label(preview_card.body, bg=SURFACE, fg=TEXT_FAINT,
                                      text="Drop an image on the Artwork page.",
                                      font=font(9), height=11, wraplength=360)
        self.preview_label.pack(fill="x")
        self.preview_caption = tk.Label(preview_card.body, text="", bg=SURFACE,
                                        fg=TEXT_FAINT, font=font(8), anchor="w",
                                        justify="left", wraplength=380)
        self.preview_caption.pack(fill="x", pady=(S2, 0))

        summary_card = Card(master, "What will change")
        summary_card.pack(fill="both", expand=True, pady=(S3, 0))
        self.summary_label = tk.Label(summary_card.body, text="Nothing selected yet.",
                                      bg=SURFACE, fg=TEXT_DIM, font=font(9),
                                      anchor="nw", justify="left", wraplength=370)
        self.summary_label.pack(fill="both", expand=True)

    def _build_actions(self):
        bar = tk.Frame(self.root, bg=INK)
        bar.pack(fill="x", padx=S5, pady=(0, S2))
        self.apply_button = self._button(bar, "Apply skin", self.on_apply, "primary")
        self.apply_button.pack(side="left")
        self._button(bar, "Dry run", lambda: self.on_apply(dry=True), "ghost").pack(side="left", padx=(S2, 0))
        self._button(bar, "Export PNGs", self.on_export, "ghost").pack(side="left", padx=(S2, 0))
        self._button(bar, "Restore\u2026", self.on_restore, "ghost").pack(side="right")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=170)

    def _build_log(self):
        holder = tk.Frame(self.root, bg=INK, height=150)
        holder.pack(fill="x", padx=S5, pady=(0, S2))
        holder.pack_propagate(False)   # the body would otherwise squeeze this flat
        head = tk.Frame(holder, bg=INK)
        head.pack(fill="x")
        self.log_toggle = tk.Label(head, text="\u25be  Activity", bg=INK, fg=TEXT_DIM,
                                   font=font(8), cursor="hand2")
        self.log_toggle.pack(side="left")
        self.log_toggle.bind("<Button-1>", lambda e: self._toggle_log())
        tk.Label(head, text="backups: %s" % pretty_path(backup.STORE), bg=INK,
                 fg=TEXT_FAINT, font=font(7)).pack(side="right")

        self.log_frame = tk.Frame(holder, bg=INK)
        self.log_frame.pack(fill="both", expand=True, pady=(S1, 0))
        self.log = tk.Text(self.log_frame, height=9, bg="#0A0810", fg=TEXT_DIM,
                           relief="flat", font=("Cascadia Mono", 8), wrap="word",
                           insertbackground=TEXT, highlightthickness=1,
                           highlightbackground=LINE, padx=S3, pady=S2)
        self.log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(self.log_frame, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        for tag, colour in (("info", TEXT_DIM), ("good", GOOD), ("warn", WARN),
                            ("bad", BAD), ("head", TEXT)):
            self.log.tag_configure(tag, foreground=colour)
        self._log_open = True

    def _toggle_log(self):
        self._log_open = not self._log_open
        if self._log_open:
            self.log_frame.pack(fill="both", pady=(S1, 0))
            self.log_toggle.configure(text="\u25be  Activity")
        else:
            self.log_frame.pack_forget()
            self.log_toggle.configure(text="\u25b8  Activity")

    def _build_status(self):
        bar = tk.Frame(self.root, bg=SURFACE, height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status = tk.Label(bar, text="Ready.", bg=SURFACE, fg=TEXT_DIM,
                               font=font(8), anchor="w", padx=S5)
        self.status.pack(side="left", fill="both", expand=True)
        tk.Label(bar, text="Not affiliated with Adobe  \u00b7  MIT licensed",
                 bg=SURFACE, fg=TEXT_FAINT, font=font(8), padx=S5).pack(side="right")

    def set_status(self, text, colour=TEXT_DIM):
        self.status.configure(text=text, fg=colour)

    # ------------------------------------------------------------------ pages

    def _page_artwork(self):
        page = tk.Frame(self.pages_holder, bg=INK)
        card = Card(page, "Artwork",
                    "Your image is fitted to every slot the install actually has, at that "
                    "slot's exact pixel size.")
        card.pack(fill="x")

        self.splash_zone = DropZone(card.body, "Splash screen",
                                    "Drop an image, or click to browse",
                                    IMAGE_TYPES, on_change=lambda p: self.queue_preview(True))
        self.splash_zone.pack(fill="x", pady=(0, S2))
        self.about_zone = DropZone(card.body, "About window",
                                   "Optional \u2014 leave empty to keep Adobe's",
                                   IMAGE_TYPES, on_change=lambda p: self.refresh_summary())
        self.about_zone.pack(fill="x")

        options = Card(page, "Fitting")
        options.pack(fill="x", pady=(S3, 0))
        row = tk.Frame(options.body, bg=SURFACE)
        row.pack(fill="x")
        self.fit_var = tk.StringVar(value="cover")
        holder, _ = labelled_combo(row, "Fit", ("cover", "contain", "stretch"),
                                   self.fit_var, 12, lambda: self.queue_preview(True))
        holder.pack(side="left", padx=(0, S4))
        self.focus_var = tk.StringVar(value="center")
        holder, _ = labelled_combo(row, "Crop bias",
                                   ("center", "left", "right", "top", "bottom", "top-left",
                                    "top-right", "bottom-left", "bottom-right"),
                                   self.focus_var, 14, lambda: self.queue_preview(True))
        holder.pack(side="left")

        self.keep_mask_var = tk.BooleanVar(value=True)
        self._check(options.body, self.keep_mask_var,
                    "Keep After Effects' rounded corners",
                    "Reuses the original slot's alpha channel.",
                    lambda: self.queue_preview(True)).pack(fill="x", pady=(S3, 0))
        self.include_small_var = tk.BooleanVar(value=False)
        self._check(options.body, self.include_small_var,
                    "Also replace icon-sized art",
                    "Usually leave off \u2014 it smears your image onto small badges.",
                    self.refresh_summary).pack(fill="x", pady=(S2, 0))

        self.slot_label = tk.Label(page, text="", bg=INK, fg=TEXT_FAINT, font=font(8),
                                   anchor="w", justify="left", wraplength=520)
        self.slot_label.pack(fill="x", pady=(S3, 0))
        return page

    def _check(self, master, variable, title, subtitle=None, command=None, bg=SURFACE):
        holder = tk.Frame(master, bg=bg)
        box = tk.Label(holder, width=2, bg=bg, fg=ACCENT, font=font(10, "bold"), cursor="hand2")
        box.pack(side="left", anchor="n")
        stack = tk.Frame(holder, bg=bg)
        stack.pack(side="left", fill="x", expand=True)
        label = tk.Label(stack, text=title, bg=bg, fg=TEXT, font=font(9),
                         anchor="w", cursor="hand2")
        label.pack(fill="x")
        if subtitle:
            tk.Label(stack, text=subtitle, bg=bg, fg=TEXT_FAINT, font=font(8),
                     anchor="w", justify="left", wraplength=430).pack(fill="x")

        def paint():
            box.configure(text="\u2611" if variable.get() else "\u2610",
                          fg=ACCENT if variable.get() else TEXT_FAINT)

        def toggle(_=None):
            variable.set(not variable.get())
            paint()
            if command:
                command()

        for widget in (box, label):
            widget.bind("<Button-1>", toggle)
        variable.trace_add("write", lambda *a: paint())
        paint()
        return holder

    def _page_caption(self):
        page = tk.Frame(self.pages_holder, bg=INK)
        card = Card(page, "Caption",
                    "Text drawn over the artwork. Leave the fields blank for none.")
        card.pack(fill="x")

        self.caption_vars = {}
        for key, label, hint in (
            ("title", "Title", "NOW ENTERING"),
            ("subtitle", "Subtitle", "ZT'S WORLD"),
            ("footer", "Footer", "\u00a9 2026 ZT. All rights reserved."),
        ):
            row = tk.Frame(card.body, bg=SURFACE)
            row.pack(fill="x", pady=(0, S2))
            tk.Label(row, text=label, bg=SURFACE, fg=TEXT_DIM, font=font(8),
                     width=9, anchor="w").pack(side="left", anchor="n", pady=(S1 + 2, 0))
            var = tk.StringVar()
            entry = ttk.Entry(row, textvariable=var, font=font(9))
            entry.pack(side="left", fill="x", expand=True)
            # Debounced: the trace fires per keystroke, the render does not.
            var.trace_add("write", lambda *a: self.queue_preview())
            self.caption_vars[key] = var
            tk.Label(row, text=hint, bg=SURFACE, fg=TEXT_FAINT, font=font(7),
                     width=22, anchor="w").pack(side="left", padx=(S2, 0))

        colours = tk.Frame(card.body, bg=SURFACE)
        colours.pack(fill="x", pady=(S2, 0))
        self.title_colour = Swatch(colours, "Title", "#FFFFFF", lambda v: self.queue_preview())
        self.title_colour.pack(side="left", padx=(0, S4))
        self.subtitle_colour = Swatch(colours, "Subtitle", "#FFFFFF", lambda v: self.queue_preview())
        self.subtitle_colour.pack(side="left", padx=(0, S4))
        self.footer_colour = Swatch(colours, "Footer", "#B9AECF", lambda v: self.queue_preview())
        self.footer_colour.pack(side="left")

        layout = tk.Frame(card.body, bg=SURFACE)
        layout.pack(fill="x", pady=(S4, 0))
        self.align_var = tk.StringVar(value="left")
        holder, _ = labelled_combo(layout, "Align", ("left", "center", "right"),
                                   self.align_var, 10, self.queue_preview)
        holder.pack(side="left", padx=(0, S4))
        self.anchor_var = tk.StringVar(value="bottom")
        holder, _ = labelled_combo(layout, "Position", ("top", "middle", "bottom"),
                                   self.anchor_var, 10, self.queue_preview)
        holder.pack(side="left")

        self.shadow_var = tk.BooleanVar(value=True)
        self._check(card.body, self.shadow_var, "Drop shadow", None,
                    self.queue_preview).pack(fill="x", pady=(S3, 0))
        return page

    def _page_interface(self):
        page = tk.Frame(self.pages_holder, bg=INK)
        card = Card(page, "Interface theme",
                    "Recolours AE's panels, tabs, fields and accents.")
        card.pack(fill="both", expand=True)

        self.theme_on = tk.BooleanVar(value=False)
        self._check(card.body, self.theme_on, "Recolour the interface", None,
                    self.refresh_summary).pack(fill="x")

        self.theme_warning = tk.Label(card.body, text="", bg=SURFACE, fg=WARN,
                                      font=font(8), anchor="w", justify="left",
                                      wraplength=470)
        self.theme_warning.pack(fill="x", pady=(S2, 0))

        row = tk.Frame(card.body, bg=SURFACE)
        row.pack(fill="x", pady=(S3, 0))
        names = sorted(list_themes())
        self.theme_var = tk.StringVar(value="zinktools" if "zinktools" in names
                                      else (names[0] if names else ""))
        holder, _ = labelled_combo(row, "Preset", names, self.theme_var, 18,
                                   self._load_theme_preset)
        holder.pack(side="left", padx=(0, S4))
        self.accent_swatch = Swatch(row, "Accent", ACCENT, lambda v: (self._draw_ramp(),
                                                                      self.refresh_summary()))
        self.accent_swatch.pack(side="left")

        tint = tk.Frame(card.body, bg=SURFACE)
        tint.pack(fill="x", pady=(S3, 0))
        tk.Label(tint, text="Hue strength on greys", bg=SURFACE, fg=TEXT_DIM,
                 font=font(8)).pack(anchor="w")
        scale_row = tk.Frame(tint, bg=SURFACE)
        scale_row.pack(fill="x", pady=(2, 0))
        self.tint_var = tk.DoubleVar(value=0.40)
        ttk.Scale(scale_row, from_=0.0, to=1.0, variable=self.tint_var, length=240,
                  command=lambda v: self._tint_changed()).pack(side="left")
        self.tint_readout = tk.Label(scale_row, text="0.40", bg=SURFACE, fg=TEXT,
                                     font=font(8), width=6)
        self.tint_readout.pack(side="left", padx=(S2, 0))
        tk.Label(tint, text="0 keeps greys neutral. Higher pushes them toward the accent hue.",
                 bg=SURFACE, fg=TEXT_FAINT, font=font(7)).pack(anchor="w")

        tk.Label(card.body, text="Chrome ramp  \u2014  darkest to lightest, click a stop to change it",
                 bg=SURFACE, fg=TEXT_DIM, font=font(8)).pack(anchor="w", pady=(S4, S1))
        self.ramp_canvas = tk.Canvas(card.body, height=40, bg=SURFACE,
                                     highlightthickness=1, highlightbackground=LINE)
        self.ramp_canvas.pack(fill="x")
        self.ramp_canvas.bind("<Button-1>", self._edit_ramp_stop)
        self.ramp_canvas.bind("<Configure>", lambda e: self._draw_ramp())

        row = tk.Frame(card.body, bg=SURFACE)
        row.pack(fill="x", pady=(S4, 0))
        tk.Label(row, text="AE themes to recolour", bg=SURFACE, fg=TEXT_DIM,
                 font=font(8)).pack(anchor="w")
        self.theme_targets_var = tk.StringVar(value="Darkest,Dark,Middark,Medium")
        ttk.Entry(row, textvariable=self.theme_targets_var, font=font(9)).pack(fill="x", pady=(2, 0))

        self.prefs_var = tk.BooleanVar(value=True)
        self._check(card.body, self.prefs_var,
                    "Set UI brightness to darkest and enable theme colourising",
                    "Edits the preferences files, not the program.").pack(fill="x", pady=(S3, 0))
        self.dvaui_var = tk.BooleanVar(value=True)
        self._check(card.body, self.dvaui_var, "Also patch dvaui.dll",
                    "Shared Adobe widgets.").pack(fill="x", pady=(S2, 0))

        self._load_theme_preset()
        return page

    def _tint_changed(self):
        self.tint_readout.configure(text="%.2f" % self.tint_var.get())
        self.refresh_summary()

    def _page_sounds(self):
        page = tk.Frame(self.pages_holder, bg=INK)
        card = Card(page, "Sounds",
                    "Replaces the chimes After Effects plays when a render ends.")
        card.pack(fill="both", expand=True)
        self.sound_zone = DropZone(card.body, "Sound file",
                                   "Drop a .wav \u2014 any format if ffmpeg is installed",
                                   AUDIO_TYPES, on_change=lambda p: self.refresh_summary())
        self.sound_zone.pack(fill="x")

        tk.Label(card.body, text="Files in this install", bg=SURFACE, fg=TEXT_DIM,
                 font=font(8)).pack(anchor="w", pady=(S4, S1))
        self.sound_list = tk.Listbox(card.body, selectmode="multiple", height=8,
                                     bg=FIELD, fg=TEXT, relief="flat", font=("Cascadia Mono", 8),
                                     highlightthickness=1, highlightbackground=LINE,
                                     selectbackground=ACCENT, selectforeground=ACCENT_INK,
                                     activestyle="none")
        self.sound_list.pack(fill="x")
        self.sound_list.bind("<<ListboxSelect>>", lambda e: self.refresh_summary())
        tk.Label(card.body, text="Select none to replace every sound.",
                 bg=SURFACE, fg=TEXT_FAINT, font=font(8)).pack(anchor="w", pady=(S1, 0))
        return page

    def _page_about(self):
        page = tk.Frame(self.pages_holder, bg=INK)
        card = Card(page, "About %s %s" % (APP_TITLE, __version__))
        card.pack(fill="both", expand=True)

        tk.Label(card.body, justify="left", anchor="w", bg=SURFACE, fg=TEXT_DIM,
                 font=font(9), wraplength=470, text=(
                     "Reskins the splash screen, About window, interface colours and render "
                     "sounds of any After Effects install on this machine.\n\n"
                     "AE Skinner is not affiliated with, endorsed by, or connected to Adobe. "
                     "It ships no Adobe code: it rewrites resources inside the copy of After "
                     "Effects already installed here. Modifying an installed application may "
                     "conflict with its licence agreement \u2014 that is your call to make.\n\n"
                     "Every change is backed up first and can be undone from Restore."
                 )).pack(fill="x")

        row = tk.Frame(card.body, bg=SURFACE)
        row.pack(fill="x", pady=(S4, 0))
        for label, filename in (("Licence (MIT)", "LICENSE"),
                                ("Third-party notices", "THIRD-PARTY-NOTICES.md"),
                                ("Read me", "README.md")):
            self._button(row, label, lambda f=filename: self._open_doc(f), "ghost").pack(
                side="left", padx=(0, S2))

        tk.Label(card.body, justify="left", anchor="w", bg=SURFACE, fg=TEXT_FAINT,
                 font=font(8), wraplength=470, pady=S3, text=(
                     "Backups   %s\n"
                     "Themes    %s\n"
                     "Settings  %s"
                     % (backup.STORE, ", ".join(sorted(list_themes())) or "none", SETTINGS_PATH)
                 )).pack(fill="x", pady=(S4, 0))
        return page

    def _open_doc(self, filename):
        for candidate in (app_root() / filename, Path(__file__).parent / filename):
            if candidate.exists():
                webbrowser.open(candidate.as_uri())
                return
        messagebox.showinfo(APP_TITLE, "%s is not next to the application." % filename)

    # ------------------------------------------------------------------ install state

    def refresh_installs(self):
        images.clear_caches()
        self.installs = find_installs()
        labels = ["%s   \u2014   %s" % (i.label, i.root) for i in self.installs]
        self.install_box.configure(values=labels)
        if labels:
            self.install_box.current(0)
        self._install_changed()
        self.write("Found %d After Effects install(s)." % len(self.installs),
                   "good" if self.installs else "warn")
        if not self.installs:
            self.write("Is After Effects installed on this machine?", "warn")

    @property
    def install(self):
        index = self.install_box.current()
        return self.installs[index] if 0 <= index < len(self.installs) else None

    def _install_changed(self):
        install = self.install
        if not install:
            self.install_note.configure(text="No install selected.", fg=WARN)
            return
        themable = install.themable
        self.install_note.configure(
            text=("Native interface theming is supported on this version."
                  if themable else
                  "%s ignores interface theme colour \u2014 panels stay grey. "
                  "Splash, About and sounds still work." % install.label),
            fg=TEXT_DIM if themable else WARN)
        if hasattr(self, "theme_warning"):
            self.theme_warning.configure(
                text="" if themable else
                "Adobe removed interface theme colouring in After Effects 2025. The "
                "resources will still be patched and verified, but this version ignores "
                "them. Use AE 2024 or older for a fully recoloured interface.")
        if hasattr(self, "sound_list"):
            self.sound_list.delete(0, "end")
            for wav in sounds.discover(install):
                self.sound_list.insert("end", wav.name)
        self.queue_preview(True)   # this also kicks the slot scan

    def _describe_slots(self):
        """Scan the install's artwork slots on a worker thread.

        Parsing a 58 MB PE resource tree takes about five seconds the first
        time. Doing that inline froze the window before it had painted, so the
        scan runs off the UI thread and posts its result back with after().
        """
        install = self.install
        lib = install.binaries.get("AfterFXLib.dll") if install else None
        if not lib:
            self.slot_label.configure(text="")
            return

        self._scan_generation += 1
        generation = self._scan_generation
        include_small = self.include_small_var.get()
        self.slot_label.configure(text="Scanning %s\u2026" % lib.name, fg=TEXT_FAINT)
        self.set_status("Scanning %s\u2026" % lib.name, ACCENT)

        def worker():
            try:
                found = images.discover_cached(lib, include_small=include_small)
                problem = None
            except Exception as failure:
                found, problem = [], failure
            self.root.after(0, lambda: done(found, problem))

        def done(slots, error):
            if generation != self._scan_generation:
                return                       # a newer scan superseded this one
            if error is not None:
                self.slot_label.configure(
                    text="Could not read %s: %s" % (lib.name, error), fg=BAD)
                self.set_status("Could not read %s" % lib.name, BAD)
                return
            sizes = sorted({"%d\u00d7%d" % (s.width, s.height) for s in slots})
            splash = sum(1 for s in slots if s.group == "splash")
            self.slot_label.configure(
                text="This install exposes %d splash and %d about slot(s):  %s"
                     % (splash, len(slots) - splash, "   ".join(sizes)), fg=TEXT_FAINT)
            self.set_status("Ready.")
            self.refresh_summary()

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ theme helpers

    def _load_theme_preset(self):
        try:
            theme = theming.Theme.load(self.theme_var.get())
        except Exception as error:
            self.write("could not load theme %r: %s" % (self.theme_var.get(), error), "bad")
            theme = theming.Theme()
        self.ramp = list(theme.ramp)
        self.accent_swatch.set(theme.accent)
        self.tint_var.set(theme.tint_max_sat)
        self.tint_readout.configure(text="%.2f" % theme.tint_max_sat)
        self.theme_targets_var.set(",".join(theme.themes))
        self._draw_ramp()
        self.refresh_summary()

    def _draw_ramp(self):
        canvas = self.ramp_canvas
        canvas.delete("all")
        width = canvas.winfo_width() or 400
        count = len(self.ramp)
        step = width / count
        for index, colour in enumerate(self.ramp):
            canvas.create_rectangle(index * step, 0, (index + 1) * step, 40,
                                    fill=colour, outline="")
        canvas.create_line(0, 0, width, 0, fill=LINE)

    def _edit_ramp_stop(self, event):
        if not self.ramp:
            return
        width = max(self.ramp_canvas.winfo_width(), 1)
        index = min(len(self.ramp) - 1, int(event.x / (width / len(self.ramp))))
        chosen = colorchooser.askcolor(color=self.ramp[index],
                                       title="Ramp stop %d of %d" % (index + 1, len(self.ramp)))[1]
        if chosen:
            self.ramp[index] = chosen
            self._draw_ramp()
            self.refresh_summary()

    # ------------------------------------------------------------------ job assembly

    def current_theme(self):
        return theming.Theme(
            name=self.theme_var.get() or "Custom",
            ramp=list(self.ramp),
            accent=self.accent_swatch.value,
            tint_max_sat=float(self.tint_var.get()),
            themes=[t.strip() for t in self.theme_targets_var.get().split(",") if t.strip()],
        )

    def current_caption(self):
        block = images.TextBlock(
            title=self.caption_vars["title"].get(),
            subtitle=self.caption_vars["subtitle"].get(),
            footer=self.caption_vars["footer"].get(),
            title_color=self.title_colour.value,
            subtitle_color=self.subtitle_colour.value,
            footer_color=self.footer_colour.value,
            align=self.align_var.get(),
            anchor=self.anchor_var.get(),
            shadow=self.shadow_var.get(),
        )
        return None if block.is_empty() else block

    def current_job(self):
        install = self.install
        if not install:
            raise RuntimeError("Pick an After Effects install first.")
        selected = [self.sound_list.get(i) for i in self.sound_list.curselection()]
        return Job(
            install=install,
            splash_image=self.splash_zone.path,
            about_image=self.about_zone.path,
            fit=self.fit_var.get(),
            focus=images.parse_focus(self.focus_var.get()),
            keep_mask=self.keep_mask_var.get(),
            text=self.current_caption(),
            include_small=self.include_small_var.get(),
            theme=self.current_theme() if self.theme_on.get() else None,
            theme_binaries=("AfterFXLib.dll", "dvaui.dll") if self.dvaui_var.get()
            else ("AfterFXLib.dll",),
            set_prefs=self.prefs_var.get(),
            sound=self.sound_zone.path,
            sound_targets=selected or None,
            note="AE Skinner GUI",
        )

    # ------------------------------------------------------------------ preview

    def queue_preview(self, invalidate=False):
        """Debounce: cancel the pending render and schedule a fresh one."""
        if invalidate:
            self.refresh_summary()
            self._describe_slots()
        if self._preview_job is not None:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(DEBOUNCE_MS, self._render_preview)

    def _render_preview(self):
        """Render on a worker, hand the finished image back to the UI thread.

        Warm renders are about 2 ms, but the first one after picking an install
        pays the PE scan, so it cannot run inline. Tk objects are not thread
        safe, so PhotoImage is only ever constructed in `done`.
        """
        self._preview_job = None
        source_path = self.splash_zone.path
        install = self.install
        if not source_path or not install:
            self.preview_label.configure(image="", text="Drop an image on the Artwork page.",
                                         height=11)
            self.preview_caption.configure(text="")
            self.preview_photo = None
            return
        lib = install.binaries.get("AfterFXLib.dll")
        if not lib:
            return

        self._preview_generation += 1
        generation = self._preview_generation
        settings = dict(
            mode=self.fit_var.get(), focus=images.parse_focus(self.focus_var.get()),
            keep_mask=self.keep_mask_var.get(), corner_radius=12,
            text=self.current_caption(),
        )

        def worker():
            try:
                slots = images.discover_cached(lib, groups=("splash",))
                if not slots:
                    self.root.after(0, lambda: self.preview_caption.configure(
                        text="This install has no splash slots."))
                    return
                slot = slots[0]
                source = images.load_source_cached(source_path)
                rendered = images.render_preview(slot, source, images._stamp(source_path),
                                                 max_size=(380, 300), **settings)
                composed = Image.alpha_composite(checkerboard(rendered.size), rendered)
                self.root.after(0, lambda: done(slot, composed, len(slots), None))
            except Exception as failure:
                self.root.after(0, lambda: done(None, None, 0, failure))

        def done(slot, composed, count, error):
            if generation != self._preview_generation:
                return
            if error is not None:
                message = (str(error) if isinstance(error, security.UnsafeInput)
                           else "Preview failed: %s" % error)
                self.preview_label.configure(image="", text=message, height=11)
                self.preview_photo = None
                if isinstance(error, security.UnsafeInput):
                    self.write(message, "bad")
                return
            self.preview_photo = ImageTk.PhotoImage(composed)
            self.preview_label.configure(image=self.preview_photo, text="", height=0)
            self.preview_caption.configure(
                text="%s  \u00b7  %d\u00d7%d  \u00b7  %d more slot(s) get the same art"
                     % (slot.resource.name, slot.width, slot.height, count - 1))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_summary(self):
        install = self.install
        if not install:
            self.summary_label.configure(text="No install selected.", fg=WARN)
            return
        lines = []
        # Read the cache only; a cold scan belongs on the worker thread.
        slots = []
        lib = install.binaries.get("AfterFXLib.dll")
        if lib:
            try:
                key = (images._stamp(lib), self.include_small_var.get(), ("splash", "about"))
                slots = images._slot_cache.get(key) or []
            except OSError:
                slots = []
        if self.splash_zone.path:
            n = sum(1 for s in slots if s.group == "splash")
            lines.append("•  Splash artwork — %s"
                         % ("%d slot(s)" % n if slots else "every splash slot"))
        if self.about_zone.path:
            n = sum(1 for s in slots if s.group == "about")
            lines.append("•  About artwork — %s"
                         % ("%d slot(s)" % n if slots else "every about slot"))
        if self.current_caption() is not None:
            lines.append("\u2022  Caption drawn over the artwork")
        if self.theme_on.get():
            targets = "AfterFXLib.dll" + (" + dvaui.dll" if self.dvaui_var.get() else "")
            lines.append("\u2022  Interface theme \u2014 %s, accent %s"
                         % (targets, self.accent_swatch.value))
            if self.prefs_var.get():
                lines.append("\u2022  Preferences \u2014 darkest brightness, colourising on")
            if not install.themable:
                lines.append("   \u26a0 this version ignores theme colour")
        if self.sound_zone.path:
            picked = self.sound_list.curselection()
            lines.append("\u2022  Sounds \u2014 %s"
                         % ("%d selected" % len(picked) if picked else "all files"))

        if not lines:
            self.summary_label.configure(
                text="Nothing selected yet.\n\nDrop an image on the Artwork page, or turn on "
                     "the interface theme.", fg=TEXT_FAINT)
        else:
            lines.append("")
            lines.append("A backup is taken before anything is written.")
            self.summary_label.configure(text="\n".join(lines), fg=TEXT_DIM)
        if hasattr(self, "apply_button"):
            self._set_enabled(self.apply_button, bool(lines))

    # ------------------------------------------------------------------ logging

    def write(self, message="", tag="info"):
        self.messages.put((str(message), tag))

    def _drain(self):
        try:
            while True:
                message, tag = self.messages.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", message + "\n", tag)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(60, self._drain)

    # ------------------------------------------------------------------ actions

    def _run(self, work, label="Working\u2026"):
        if self.busy:
            return
        self.busy = True
        self._set_enabled(self.apply_button, False)
        self.progress.pack(side="left", padx=(S4, 0))
        self.progress.start(12)
        self.set_status(label, ACCENT)

        def finish(status, colour):
            self.busy = False
            self.progress.stop()
            self.progress.pack_forget()
            self.set_status(status, colour)
            self.refresh_summary()

        def wrapper():
            try:
                work()
                self.root.after(0, lambda: finish("Done.", GOOD))
            except Exception as error:
                self.write("ERROR: %s" % error, "bad")
                self.write(traceback.format_exc(), "bad")
                self.root.after(0, lambda: finish("Failed \u2014 see the activity log.", BAD))

        threading.Thread(target=wrapper, daemon=True).start()

    def on_apply(self, dry=False):
        try:
            job = self.current_job()
        except Exception as error:
            messagebox.showerror(APP_TITLE, str(error))
            return
        if not (job.splash_image or job.about_image or job.theme or job.sound):
            messagebox.showinfo(APP_TITLE, "Nothing selected. Drop an image, turn on the "
                                           "interface theme, or pick a sound.")
            return
        if not dry and not messagebox.askyesno(
            APP_TITLE,
            "Patch %s?\n\n"
            "After Effects must be closed.\n"
            "Everything touched is backed up first and can be undone from Restore."
            % job.install.label, icon="warning",
        ):
            return

        def work():
            self.write("")
            self.write("\u2500" * 58, "head")
            self.write(job.install.describe(), "head")
            self.write("planning\u2026")
            plan = build_plan(job, log=self.write)
            for warning in plan.warnings:
                self.write("!  %s" % warning, "warn")
            if plan.is_empty():
                self.write("nothing to change.", "warn")
                return
            self.write("plan:")
            self.write(plan.summary())
            if dry:
                problems = preflight(plan)
                for problem in problems:
                    self.write("BLOCKED: %s" % problem, "bad")
                if not problems:
                    self.write("dry run passed \u2014 nothing was written.", "good")
                return
            snapshot = execute(plan, log=self.write)
            self.write("done \u2014 backup at %s" % snapshot.directory, "good")
            self.write("Restart After Effects to see it.", "good")
            images.clear_caches()

        self._run(work, "Patching\u2026" if not dry else "Dry run\u2026")

    def on_export(self):
        try:
            job = self.current_job()
        except Exception as error:
            messagebox.showerror(APP_TITLE, str(error))
            return
        if not (job.splash_image or job.about_image):
            messagebox.showinfo(APP_TITLE, "Drop a splash or about image first.")
            return
        folder = filedialog.askdirectory(title="Where should the PNGs go?")
        if not folder:
            return
        job.theme = None
        job.sound = None

        def work():
            from aeskin.apply import preview as render_preview
            plan = build_plan(job, log=lambda *a: None)
            written = render_preview(plan, Path(folder), log=self.write)
            self.write("exported %d PNG(s) to %s" % (len(written), folder), "good")

        self._run(work, "Exporting\u2026")

    def on_restore(self):
        install = self.install
        if not install:
            return
        snapshots = backup.listing(install.id)
        if not snapshots:
            messagebox.showinfo(APP_TITLE, "No backups for %s yet." % install.label)
            return
        RestoreDialog(self.root, install, snapshots, self.write, self)

    # ------------------------------------------------------------------ settings

    def _load_settings(self):
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _apply_settings(self):
        data = self._settings
        if not data:
            self.refresh_summary()
            return
        try:
            for key, var in (("fit", self.fit_var), ("focus", self.focus_var),
                             ("align", self.align_var), ("anchor", self.anchor_var),
                             ("theme", self.theme_var),
                             ("theme_targets", self.theme_targets_var)):
                if key in data:
                    var.set(data[key])
            for key, var in (("keep_mask", self.keep_mask_var),
                             ("include_small", self.include_small_var),
                             ("shadow", self.shadow_var), ("prefs", self.prefs_var),
                             ("dvaui", self.dvaui_var), ("theme_on", self.theme_on)):
                if key in data:
                    var.set(bool(data[key]))
            for key in ("title", "subtitle", "footer"):
                if key in data:
                    self.caption_vars[key].set(data[key])
            for key, swatch in (("title_colour", self.title_colour),
                                ("subtitle_colour", self.subtitle_colour),
                                ("footer_colour", self.footer_colour),
                                ("accent", self.accent_swatch)):
                if key in data and security.HEX_COLOUR.match(str(data[key])):
                    swatch.set(data[key])
            if isinstance(data.get("ramp"), list) and data["ramp"]:
                self.ramp = [c for c in data["ramp"] if security.HEX_COLOUR.match(str(c))]
                self._draw_ramp()
            if "tint" in data:
                self.tint_var.set(float(data["tint"]))
                self.tint_readout.configure(text="%.2f" % float(data["tint"]))
            wanted = data.get("install")
            for index, install in enumerate(self.installs):
                if install.id == wanted:
                    self.install_box.current(index)
                    self._install_changed()
                    break
        except Exception:
            pass
        self.refresh_summary()

    def _save_settings(self):
        data = {
            "fit": self.fit_var.get(), "focus": self.focus_var.get(),
            "align": self.align_var.get(), "anchor": self.anchor_var.get(),
            "keep_mask": self.keep_mask_var.get(),
            "include_small": self.include_small_var.get(),
            "shadow": self.shadow_var.get(), "prefs": self.prefs_var.get(),
            "dvaui": self.dvaui_var.get(), "theme_on": self.theme_on.get(),
            "theme": self.theme_var.get(), "theme_targets": self.theme_targets_var.get(),
            "accent": self.accent_swatch.value, "ramp": list(self.ramp),
            "tint": float(self.tint_var.get()),
            "title": self.caption_vars["title"].get(),
            "subtitle": self.caption_vars["subtitle"].get(),
            "footer": self.caption_vars["footer"].get(),
            "title_colour": self.title_colour.value,
            "subtitle_colour": self.subtitle_colour.value,
            "footer_colour": self.footer_colour.value,
            "install": self.install.id if self.install else None,
        }
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
            APP_TITLE, "A patch is still running. Quit anyway?", icon="warning"):
            return
        self._save_settings()
        self.root.destroy()


class RestoreDialog(tk.Toplevel):
    def __init__(self, master, install, snapshots, log, app):
        super().__init__(master)
        self.title("Restore \u2014 %s" % install.label)
        self.configure(bg=INK)
        self.geometry("720x420")
        self.transient(master)
        self.grab_set()
        self.snapshots = snapshots
        self.install = install
        self.log = log
        self.app = app

        tk.Label(self, text="Restore a backup", bg=INK, fg=TEXT,
                 font=font(13, "bold", display=True)).pack(anchor="w", padx=S5, pady=(S4, 0))
        tk.Label(self, bg=INK, fg=TEXT_DIM, font=font(8), justify="left", anchor="w",
                 text="Oldest first. The first entry is the closest thing to a stock install.\n"
                      "Files are re-hashed and their destinations re-checked before anything "
                      "is written."
                 ).pack(anchor="w", padx=S5, pady=(S1, S3))

        self.listbox = tk.Listbox(self, bg=FIELD, fg=TEXT, relief="flat",
                                  highlightthickness=1, highlightbackground=LINE,
                                  selectbackground=ACCENT, selectforeground=ACCENT_INK,
                                  font=("Cascadia Mono", 8), activestyle="none")
        self.listbox.pack(fill="both", expand=True, padx=S5)
        for snapshot in snapshots:
            self.listbox.insert("end", "  " + snapshot.describe())
        self.listbox.selection_set(0)

        row = tk.Frame(self, bg=INK)
        row.pack(fill="x", padx=S5, pady=S4)
        app._button(row, "Restore selected", self._restore, "primary").pack(side="left")
        app._button(row, "Cancel", self.destroy, "ghost").pack(side="right")

    def _restore(self):
        selection = self.listbox.curselection()
        if not selection:
            return
        snapshot = self.snapshots[selection[0]]
        from aeskin.util import running_afterfx
        running = running_afterfx()
        if running:
            messagebox.showerror(
                APP_TITLE, "Close After Effects first (%s)."
                % ", ".join("%s PID %s" % (n, p) for n, p in running))
            return
        if not messagebox.askyesno(APP_TITLE, "Restore the backup from %s?" % snapshot.stamp,
                                   icon="warning"):
            return
        count = backup.restore(
            snapshot, log=self.log,
            allowed_roots=security.allowed_restore_roots([self.install]))
        images.clear_caches()
        self.log("restored %d file(s) from %s" % (count, snapshot.stamp), "good")
        messagebox.showinfo(APP_TITLE, "Restored %d file(s).\n\nRestart After Effects." % count)
        self.destroy()


def selftest():
    """Build the whole window, exercise it, and exit. Verifies a packaged build."""
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    root.withdraw()
    app = App(root)
    root.update_idletasks()
    root.update()
    for name in PAGES:
        app.show_page(name)
        root.update()
    report = [
        "drag and drop : %s" % ("yes" if HAS_DND else "NO (tkinterdnd2 missing)"),
        "installs      : %d" % len(app.installs),
        "themes        : %s" % ", ".join(sorted(list_themes())),
        "pages         : %s" % ", ".join(PAGES),
        "install picked: %s" % (app.install.label if app.install else "none"),
    ]
    root.destroy()
    print("AE Skinner selftest")
    for line in report:
        print("  " + line)
    print("OK")
    return 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
