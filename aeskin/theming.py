"""Recolour After Effects' embedded colour-theme resources.

Everything is discovery driven. We never assume a theme name, a ramp length, a
resource language, or that a ramp is still in Adobe's stock entity form -- an
install that has already been skinned must stay re-skinnable.
"""
from __future__ import annotations

import colorsys
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import security
from .util import find_theme, hex_to_hsv, hex_to_rgb, is_neutral, resample_ramp, rgb_to_hex

# Adobe's UI accent sits in this hue band; anything saturated inside it is
# chrome we are allowed to recolour.
ACCENT_BAND = (190.0, 232.0)
ACCENT_MIN_SAT = 0.20

# Spectrum colour families that carry meaning (status, labels, media types).
# We only ever touch GRAY (the chrome ramp) and BLUE (the accent).
SPECTRUM_RAMP_FAMILY = "GRAY"
SPECTRUM_ACCENT_FAMILIES = ("BLUE",)

# Key names whose colour means something specific to the user.
SEMANTIC_KEY = re.compile(
    r"(LABEL|WARNING|ERROR|CAUTION|ALERT|CACHE|AUDIO|VIDEO|MISSING|RENDER"
    r"|_RED|_GREEN|_YELLOW|_ORANGE|_MAGENTA|_CELERY|_CHARTREUSE|_FUCHSIA"
    r"|_SEAFOAM|_PURPLE|_INDIGO|_TURQUOISE)",
    re.IGNORECASE,
)

THEME_BLOCK = re.compile(r'<Theme\s+t="&([^;"]+);"\s*>')
KEYFRAME = re.compile(r"<KeyFrame\b(?P<body>[^>]*?)/?>")
ATTR = re.compile(r'(?P<key>[A-Za-z_][\w.-]*)\s*=\s*"(?P<val>[^"]*)"')


@dataclass
class Theme:
    name: str = "Custom"
    ramp: list = field(default_factory=lambda: [
        "#07050C", "#0F0C18", "#120A1C", "#161320", "#1A1028",
        "#2A2140", "#3B2460", "#665C7A", "#C7A8FF", "#F2EEFA",
    ])
    accent: str = "#7C5CFC"
    accent_soft: str = "#C7A8FF"
    tint_hue: float | None = None       # defaults to the accent's hue
    tint_max_sat: float = 0.40
    tint_value_ceiling: float = 0.55    # only neutrals darker than this get tinted
    tint_value_floor: float = 0.015     # leave pure black alone
    themes: list = field(default_factory=lambda: ["Darkest", "Dark", "Middark", "Medium"])
    guide_color: str | None = None      # defaults to the accent

    @classmethod
    def load(cls, source) -> "Theme":
        path = Path(source)
        if not path.exists():
            builtin = find_theme(source)
            if builtin is None:
                raise FileNotFoundError(f"no such theme: {source}")
            path = builtin
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise security.UnsafeInput("%s is not valid JSON: %s" % (path.name, error))
        clean = security.validate_theme(data)
        clean.pop("_ignored", None)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in clean.items() if k in known})

    def save(self, path) -> None:
        payload = {k: getattr(self, k) for k in self.__dataclass_fields__}
        Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @property
    def hue(self) -> float:
        return self.tint_hue if self.tint_hue is not None else hex_to_hsv(self.accent)[0]

    @property
    def accent_hue(self) -> float:
        return hex_to_hsv(self.accent)[0]

    def ramp_for(self, count: int):
        return resample_ramp(self.ramp, count)

    def tint_neutral(self, h: float, s: float, v: float):
        """Give a grey some of the theme's hue; None means 'leave it alone'."""
        if s > 0.08:
            return None
        if not (self.tint_value_floor <= v <= self.tint_value_ceiling):
            return None
        strength = 1.0 - (v / self.tint_value_ceiling)
        return self.hue, round(self.tint_max_sat * strength, 4), v


# --------------------------------------------------------------------------- helpers

def _attrs(body: str) -> dict:
    return {m.group("key"): m.group("val") for m in ATTR.finditer(body)}


def _set_attrs(body: str, new: dict) -> str:
    """Rewrite h/s/v attributes on a KeyFrame body, keeping everything else."""
    out = body
    for key, value in new.items():
        pattern = re.compile(r'(\b' + re.escape(key) + r'\s*=\s*")[^"]*(")')
        if pattern.search(out):
            out = pattern.sub(r"\g<1>" + value + r"\g<2>", out, count=1)
        else:
            anchor = re.search(r'(name\s*=\s*"[^"]*")', out)
            if anchor:
                out = out[:anchor.end()] + ' ' + key + '="' + value + '"' + out[anchor.end():]
            else:
                out = out.rstrip() + ' ' + key + '="' + value + '"'
    return out


def _spectrum_parts(name: str):
    m = re.match(r"^&?SPECTRUM_GLOBAL_COLOR_([A-Z]+)_(\d+);?$", name)
    return (m.group(1), int(m.group(2))) if m else None


def _gray_index(name: str):
    m = re.match(r"^&?kColor_Gray_(\d+);?$", name)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- XML

def _patch_block(block: str, theme: Theme):
    """Recolour one <Theme> block (or a whole themeless document)."""
    changes = 0

    # Pass 1 -- discover how many stops each indexed ramp actually has here.
    gray_indices = set()
    spectrum_levels = set()
    for match in KEYFRAME.finditer(block):
        name = _attrs(match.group("body")).get("name", "")
        idx = _gray_index(name)
        if idx is not None:
            gray_indices.add(idx)
        parts = _spectrum_parts(name)
        if parts and parts[0] == SPECTRUM_RAMP_FAMILY:
            spectrum_levels.add(parts[1])

    gray_order = sorted(gray_indices)
    gray_map = dict(zip(gray_order, theme.ramp_for(len(gray_order)))) if gray_order else {}
    spectrum_order = sorted(spectrum_levels)
    spectrum_map = dict(zip(spectrum_order, theme.ramp_for(len(spectrum_order)))) if spectrum_order else {}

    accent_h = theme.accent_hue

    def rewrite(match):
        nonlocal changes
        whole = match.group(0)
        body = match.group("body")
        attrs = _attrs(body)
        name = attrs.get("name", "")
        closing = "/>" if whole.rstrip().endswith("/>") else ">"

        def emit(new_body):
            spacer = "" if new_body.endswith((" ", "\t")) else " "
            return "<KeyFrame" + new_body + spacer + closing

        # --- indexed chrome ramps: authoritative, overrides entity or explicit form
        target_hex = None
        idx = _gray_index(name)
        if idx is not None and idx in gray_map:
            target_hex = gray_map[idx]
        else:
            parts = _spectrum_parts(name)
            if parts:
                family, level = parts
                if family == SPECTRUM_RAMP_FAMILY and level in spectrum_map:
                    target_hex = spectrum_map[level]
                elif family in SPECTRUM_ACCENT_FAMILIES:
                    if "h" in attrs:
                        changes += 1
                        return emit(_set_attrs(body, {"h": "%.2f" % accent_h}))
                    return whole
                else:
                    return whole  # semantic Spectrum family, hands off

        if target_hex is not None:
            h, s, v = hex_to_hsv(target_hex)
            changes += 1
            return emit(_set_attrs(body, {
                "h": "%.2f" % h, "s": "%.4f" % s, "v": "%.4f" % v,
            }))

        if SEMANTIC_KEY.search(name):
            return whole

        # --- explicit h/s/v keyframes
        if "h" in attrs and "v" in attrs:
            try:
                h = float(attrs["h"])
                s = float(attrs.get("s", "0"))
                v = float(attrs["v"])
            except ValueError:
                return whole
            if ACCENT_BAND[0] <= h <= ACCENT_BAND[1] and s >= ACCENT_MIN_SAT:
                changes += 1
                return emit(_set_attrs(body, {"h": "%.2f" % accent_h}))
            tinted = theme.tint_neutral(h, s, v)
            if tinted:
                th, ts, tv = tinted
                changes += 1
                return emit(_set_attrs(body, {
                    "h": "%.2f" % th, "s": "%.4f" % ts, "v": "%.4f" % tv,
                }))
            return whole

        # --- value-only keyframes (grey by construction): give them a hue
        raw_v = attrs.get("v", "")
        if raw_v and not raw_v.startswith("&"):
            try:
                v = float(raw_v)
            except ValueError:
                return whole
            tinted = theme.tint_neutral(0.0, 0.0, v)
            if tinted:
                th, ts, tv = tinted
                changes += 1
                return emit(_set_attrs(body, {
                    "h": "%.2f" % th, "s": "%.4f" % ts, "v": "%.4f" % tv,
                }))
        return whole

    return KEYFRAME.sub(rewrite, block), changes


def _patch_entities(xml: str, theme: Theme):
    """Retarget the DTD's hue entities (kColor_Blue_xx_H, kFocusColor_xxH, ...)."""
    changes = 0
    accent_h = theme.accent_hue

    def rewrite(match):
        nonlocal changes
        name, value = match.group("name"), match.group("val")
        if SEMANTIC_KEY.search(name):
            return match.group(0)
        try:
            hue = float(value)
        except ValueError:
            return match.group(0)
        if not (ACCENT_BAND[0] <= hue <= ACCENT_BAND[1]):
            return match.group(0)
        changes += 1
        return '<!ENTITY ' + name + ' "%.2f">' % accent_h

    # Only entities that are named like a hue -- an uppercase trailing H on a
    # colour/focus/indicator name. A loose match would eat sizes ("...Width").
    xml = re.sub(
        r'<!ENTITY\s+(?P<name>[\w.-]*(?:[Cc]olor|[Cc]olour|Focus|Hue|Indicator|Accent|Highlight)[\w.-]*H)'
        r'\s+"(?P<val>[0-9.]+)"\s*>',
        rewrite, xml,
    )
    return xml, changes


def patch_color_xml(raw: bytes, theme: Theme):
    xml = raw.decode("utf-8", errors="replace")
    xml, changes = _patch_entities(xml, theme)

    blocks = list(THEME_BLOCK.finditer(xml))
    if not blocks:
        body, count = _patch_block(xml, theme)
        return body.encode("utf-8"), changes + count

    wanted = set(name.lower() for name in theme.themes)
    pieces = [xml[:blocks[0].start()]]
    for index, match in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(xml)
        block = xml[match.start():end]
        if match.group(1).lower() in wanted:
            block, count = _patch_block(block, theme)
            changes += count
        pieces.append(block)
    return "".join(pieces).encode("utf-8"), changes


# --------------------------------------------------------------------------- JSON / CSS

RGB_FUNC = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([0-9.]+))?\s*\)")
HEX_COLOR = re.compile(r"#([0-9a-fA-F]{6})\b")


def _recolour_rgb(theme: Theme, r: int, g: int, b: int):
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    hue = h * 360.0
    if ACCENT_BAND[0] <= hue <= ACCENT_BAND[1] and s >= ACCENT_MIN_SAT:
        rr, gg, bb = colorsys.hsv_to_rgb(theme.accent_hue / 360.0, s, v)
        return int(rr * 255), int(gg * 255), int(bb * 255)
    if not is_neutral(r, g, b):
        return r, g, b
    tinted = theme.tint_neutral(hue, s, v)
    if not tinted:
        return r, g, b
    th, ts, tv = tinted
    rr, gg, bb = colorsys.hsv_to_rgb(th / 360.0, ts, tv)
    return int(rr * 255), int(gg * 255), int(bb * 255)


def _recolour_text(text: str, theme: Theme):
    changes = 0

    def rgb_repl(match):
        nonlocal changes
        r, g, b = (int(match.group(i)) for i in (1, 2, 3))
        nr, ng, nb = _recolour_rgb(theme, r, g, b)
        if (nr, ng, nb) == (r, g, b):
            return match.group(0)
        changes += 1
        if match.group(4) is not None:
            return "rgba(%d, %d, %d, %s)" % (nr, ng, nb, match.group(4))
        return "rgb(%d, %d, %d)" % (nr, ng, nb)

    def hex_repl(match):
        nonlocal changes
        r, g, b = hex_to_rgb(match.group(1))
        nr, ng, nb = _recolour_rgb(theme, r, g, b)
        if (nr, ng, nb) == (r, g, b):
            return match.group(0)
        changes += 1
        return rgb_to_hex((nr, ng, nb)).lower()

    text = RGB_FUNC.sub(rgb_repl, text)
    text = HEX_COLOR.sub(hex_repl, text)
    return text, changes


def patch_color_json(raw: bytes, theme: Theme):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw, 0
    wanted = set(name.lower() for name in theme.themes)
    changes = 0

    def walk(node):
        nonlocal changes
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            new, count = _recolour_text(node, theme)
            changes += count
            return new
        return node

    if isinstance(data, dict) and any(k.lower() in wanted for k in data):
        for key in list(data):
            if key.lower() in wanted:
                data[key] = walk(data[key])
    else:
        data = walk(data)
    return (json.dumps(data, indent="\t") + "\n").encode("utf-8"), changes


def patch_color_css(raw: bytes, theme: Theme):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, 0
    text, changes = _recolour_text(text, theme)
    return text.encode("utf-8"), changes


def patch_blob(name: str, raw: bytes, theme: Theme):
    """Dispatch on content, not on the resource's name."""
    head = raw.lstrip()[:1]
    if head == b"<":
        return patch_color_xml(raw, theme)
    if head == b"{":
        return patch_color_json(raw, theme)
    return patch_color_css(raw, theme)


# --------------------------------------------------------------------------- preferences

BRIGHTNESS = re.compile(r'("User Interface Brightness \(\d+\) \[0\.0\.\.1\.0\]"\s*=\s*)"[^"]*"')
GUIDE = re.compile(r'("Pref_GUIDE_COLOR"\s*=\s*)"[^"]*"')
COLORIZING = re.compile(r"(?m)^Enable_Theme_Colorizing\s+\S+.*$")


def patch_general_prefs(text: str, theme: Theme, brightness: str = "0.000000"):
    changes = 0
    text, n = BRIGHTNESS.subn(r'\g<1>"' + brightness + '"', text)
    changes += n
    guide = theme.guide_color or theme.accent
    r, g, b = hex_to_rgb(guide)
    text, n = GUIDE.subn(r'\g<1>"0xff%02x%02x%02x"' % (r, g, b), text)
    changes += n
    return text, changes


def patch_debug_database(text: str):
    """Turn on Enable_Theme_Colorizing, adding the row if the build lacks it."""
    text, count = COLORIZING.subn("Enable_Theme_Colorizing\ttrue\t", text)
    if count:
        return text, count
    if not text.endswith("\n"):
        text += "\n"
    return text + "Enable_Theme_Colorizing\ttrue\t\n", 1
