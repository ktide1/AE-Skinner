"""Rebuild After Effects' splash / about artwork from an arbitrary image.

Sizes are never assumed: we read the exact pixel dimensions out of each PNG
resource already in the binary and regenerate at that size, reusing the
original's alpha so AE's rounded corners and cut-outs survive.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import pe, security
from .util import hex_to_rgb

SPLASH_HINTS = ("SPLASH",)
ABOUT_HINTS = ("ABOUT",)
# Small badges that share the ABOUT prefix but are not the card.
ABOUT_EXCLUDE = ("RENDERENGINE", "RENDER_ENGINE")

MIN_DIMENSION = 200  # ignore icon-sized art unless the caller says otherwise


@dataclass
class ArtSlot:
    resource: pe.Resource
    width: int
    height: int
    group: str

    def label(self) -> str:
        return "%s  %dx%d  (%s)" % (self.resource.name, self.width, self.height, self.group)


@dataclass
class TextBlock:
    """Optional caption drawn over the artwork."""
    title: str = ""
    subtitle: str = ""
    footer: str = ""
    title_color: str = "#FFFFFF"
    subtitle_color: str = "#FFFFFF"
    footer_color: str = "#B9AECF"
    align: str = "left"                 # left | center | right
    anchor: str = "bottom"              # top | middle | bottom
    margin: float = 0.07                # fraction of the short edge
    shadow: bool = True
    font_files: list = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.title or self.subtitle or self.footer)


def _is_art_name(name: str) -> bool:
    upper = name.upper()
    return any(h in upper for h in SPLASH_HINTS) or any(h in upper for h in ABOUT_HINTS)


def discover(binary: Path, include_small: bool = False, groups=("splash", "about")):
    """Every splash/about PNG the binary actually contains."""
    slots = []
    for res in pe.iter_resources(binary, {"PNG"}, name_filter=_is_art_name):
        upper = res.name.upper()
        if any(hint in upper for hint in SPLASH_HINTS):
            group = "splash"
        elif any(hint in upper for hint in ABOUT_HINTS):
            if any(bad in upper for bad in ABOUT_EXCLUDE):
                continue
            group = "about"
        else:
            continue
        if group not in groups:
            continue
        size = pe.png_size(res.data)
        if not size:
            continue
        width, height = size
        if not include_small and min(width, height) < MIN_DIMENSION:
            continue
        slots.append(ArtSlot(res, width, height, group))
    slots.sort(key=lambda s: (s.group, -(s.width * s.height), s.resource.name))
    return slots


# --------------------------------------------------------------------------- fitting

def fit_image(src: Image.Image, size, mode: str = "cover", focus=(0.5, 0.5),
              background: str = "#000000") -> Image.Image:
    """Scale `src` into `size`. 'cover' crops, 'contain' letterboxes, 'stretch' distorts."""
    target_w, target_h = size
    src = src.convert("RGBA")
    src_w, src_h = src.size
    if mode == "stretch":
        return src.resize((target_w, target_h), Image.Resampling.LANCZOS)

    if mode == "contain":
        scale = min(target_w / src_w, target_h / src_h)
        new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
        resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (target_w, target_h), hex_to_rgb(background) + (255,))
        canvas.alpha_composite(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
        return canvas

    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(target_w, round(src_w * scale)), max(target_h, round(src_h * scale))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = int(round((new_w - target_w) * min(max(focus[0], 0.0), 1.0)))
    top = int(round((new_h - target_h) * min(max(focus[1], 0.0), 1.0)))
    return resized.crop((left, top, left + target_w, top + target_h))


def _original_alpha(original_png: bytes, size):
    """The alpha channel AE ships for this slot (rounded corners, masks)."""
    try:
        im = Image.open(BytesIO(original_png)).convert("RGBA")
    except Exception:
        return None
    if im.size != size:
        im = im.resize(size, Image.Resampling.LANCZOS)
    alpha = im.split()[-1]
    if alpha.getextrema() == (255, 255):
        return None  # fully opaque, nothing worth preserving
    return alpha


def rounded_mask(size, radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


# --------------------------------------------------------------------------- text

_FONT_DIRS = [Path(r"C:\Windows\Fonts")]
_FONT_PREFS = ["seguisb.ttf", "segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf"]


def _load_font(size: int, extra=(), bold_first: bool = True):
    names = list(extra) + (_FONT_PREFS if bold_first else list(reversed(_FONT_PREFS)))
    for name in names:
        candidate = Path(name)
        paths = [candidate] if candidate.is_absolute() else [d / name for d in _FONT_DIRS]
        for path in paths:
            try:
                if path.exists():
                    return ImageFont.truetype(str(path), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_line(draw, xy, text, font, fill, align, shadow):
    box = draw.textbbox((0, 0), text, font=font)
    width = box[2] - box[0]
    x, y = xy
    if align == "center":
        x -= width / 2
    elif align == "right":
        x -= width
    # Pull the glyphs up by the top bearing so `y` is the visual top of the
    # line; otherwise the stacked block drifts down past its margin.
    y -= box[1]
    if shadow:
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 150))
    draw.text((x, y), text, font=font, fill=fill)
    return box[3] - box[1]


def draw_text(image: Image.Image, block: TextBlock) -> Image.Image:
    if block.is_empty():
        return image
    width, height = image.size
    short = min(width, height)
    margin = int(short * block.margin)
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    title_font = _load_font(max(14, int(short * 0.085)), block.font_files)
    subtitle_font = _load_font(max(12, int(short * 0.055)), block.font_files)
    footer_font = _load_font(max(9, int(short * 0.026)), block.font_files, bold_first=False)

    lines = []
    if block.title:
        lines.append((block.title, title_font, block.title_color, int(short * 0.012)))
    if block.subtitle:
        lines.append((block.subtitle, subtitle_font, block.subtitle_color, int(short * 0.022)))
    if block.footer:
        lines.append((block.footer, footer_font, block.footer_color, 0))

    heights = []
    for text, font, _, gap in lines:
        box = draw.textbbox((0, 0), text, font=font)
        heights.append(box[3] - box[1] + gap)
    total = sum(heights)

    if block.anchor == "top":
        y = margin
    elif block.anchor == "middle":
        y = (height - total) / 2
    else:
        y = height - margin - total

    if block.align == "center":
        x = width / 2
    elif block.align == "right":
        x = width - margin
    else:
        x = margin

    for (text, font, colour, gap), line_height in zip(lines, heights):
        _draw_line(draw, (x, y), text, font, hex_to_rgb(colour) + (255,), block.align, block.shadow)
        y += line_height

    return Image.alpha_composite(image.convert("RGBA"), layer)


# --------------------------------------------------------------------------- render

def render_slot(slot: ArtSlot, source: Image.Image, mode: str = "cover",
                focus=(0.5, 0.5), keep_mask: bool = True, corner_radius=None,
                text: TextBlock | None = None, background: str = "#000000") -> bytes:
    size = (slot.width, slot.height)
    art = fit_image(source, size, mode=mode, focus=focus, background=background)
    if text is not None:
        art = draw_text(art, text)

    mask = None
    if keep_mask:
        mask = _original_alpha(slot.resource.data, size)
    if mask is None and corner_radius:
        scaled = max(2, int(round(corner_radius * slot.width / 766.0)))
        mask = rounded_mask(size, scaled)

    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(art, (0, 0))
    if mask is not None:
        # Keep whatever transparency the user's own image carries, then cut it
        # down by AE's mask so the rounded corners stay rounded.
        out.putalpha(ImageChops.multiply(out.split()[-1], mask))

    buffer = BytesIO()
    out.save(buffer, format="PNG", optimize=True, compress_level=9)
    return buffer.getvalue()


def load_source(path: Path) -> Image.Image:
    """Open a user-supplied image with the decompression-bomb guards on."""
    return security.open_image(path)


def parse_focus(value: str):
    """'0.22,0.5' or a keyword like 'left' / 'top-right' / 'center'."""
    keywords = {
        "center": (0.5, 0.5), "centre": (0.5, 0.5),
        "left": (0.0, 0.5), "right": (1.0, 0.5),
        "top": (0.5, 0.0), "bottom": (0.5, 1.0),
        "top-left": (0.0, 0.0), "top-right": (1.0, 0.0),
        "bottom-left": (0.0, 1.0), "bottom-right": (1.0, 1.0),
    }
    key = value.strip().lower()
    if key in keywords:
        return keywords[key]
    match = re.match(r"^\s*([0-9.]+)\s*[,x ]\s*([0-9.]+)\s*$", value)
    if not match:
        raise ValueError("focus must be 'x,y' in 0..1 or a keyword like 'left'")
    return (float(match.group(1)), float(match.group(2)))


# --------------------------------------------------------------------------- caches
#
# The GUI re-renders the preview on every keystroke in the caption fields.
# Without these, each keystroke re-parsed a 58 MB PE resource tree (~5.5 s),
# re-decoded the source image, and re-encoded a 1500x1000 PNG at compress
# level 9. Everything below is keyed on (path, mtime, size) so a patched or
# swapped file invalidates itself.

_MAX_CACHE = 8
_slot_cache = OrderedDict()
_source_cache = OrderedDict()
_alpha_cache = OrderedDict()
_base_cache = OrderedDict()


def _stamp(path):
    stat = Path(path).stat()
    return (str(Path(path).resolve()).lower(), stat.st_mtime_ns, stat.st_size)


def _remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _MAX_CACHE:
        cache.popitem(last=False)
    return value


def clear_caches():
    for cache in (_slot_cache, _source_cache, _alpha_cache, _base_cache):
        cache.clear()


def discover_cached(binary: Path, include_small: bool = False, groups=("splash", "about")):
    """discover() memoised on the binary's identity. This is the 5.5 s one."""
    key = (_stamp(binary), include_small, tuple(groups))
    if key in _slot_cache:
        _slot_cache.move_to_end(key)
        return _slot_cache[key]
    return _remember(_slot_cache, key, discover(binary, include_small, groups))


def load_source_cached(path: Path) -> Image.Image:
    key = _stamp(path)
    if key in _source_cache:
        _source_cache.move_to_end(key)
        return _source_cache[key]
    return _remember(_source_cache, key, load_source(path))


def _alpha_cached(slot: ArtSlot, size):
    key = (slot.resource.name, slot.resource.lang, size, len(slot.resource.data))
    if key in _alpha_cache:
        _alpha_cache.move_to_end(key)
        return _alpha_cache[key]
    return _remember(_alpha_cache, key, _original_alpha(slot.resource.data, size))


def render_preview(slot: ArtSlot, source: Image.Image, source_key, max_size=(400, 340),
                   mode: str = "cover", focus=(0.5, 0.5), keep_mask: bool = True,
                   corner_radius=None, text: TextBlock | None = None,
                   background: str = "#000000") -> Image.Image:
    """A display-sized preview. No PNG round trip, and the artwork underneath
    the caption is cached, so typing only costs a text redraw.

    draw_text() scales every font off the short edge, so the small preview is a
    faithful proportional match for the full-size render.
    """
    scale = min(max_size[0] / slot.width, max_size[1] / slot.height, 1.0)
    size = (max(1, round(slot.width * scale)), max(1, round(slot.height * scale)))

    key = (source_key, slot.resource.name, size, mode, tuple(focus), keep_mask, corner_radius)
    base = _base_cache.get(key)
    if base is None:
        art = fit_image(source, size, mode=mode, focus=focus, background=background)
        mask = _alpha_cached(slot, size) if keep_mask else None
        if mask is None and corner_radius:
            mask = rounded_mask(size, max(2, int(round(corner_radius * size[0] / 766.0))))
        base = Image.new("RGBA", size, (0, 0, 0, 0))
        base.paste(art, (0, 0))
        if mask is not None:
            base.putalpha(ImageChops.multiply(base.split()[-1], mask))
        _remember(_base_cache, key, base)
    else:
        _base_cache.move_to_end(key)

    return draw_text(base.copy(), text) if text is not None else base.copy()
