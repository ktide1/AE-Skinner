"""Safety rails.

Three jobs:

* refuse to patch anything that is not genuinely an Adobe After Effects binary
* refuse to restore a backup to somewhere outside a known After Effects install
* open user-supplied images and theme files defensively

Guidance followed here:
  Pillow security handbook  https://pillow.readthedocs.io/en/stable/handbook/security.html
  WinVerifyTrust            https://learn.microsoft.com/en-us/windows/win32/api/wintrust/nf-wintrust-winverifytrust
"""
from __future__ import annotations

import ctypes
import os
import re
import warnings
from ctypes import wintypes
from pathlib import Path

import pefile
from PIL import Image

# --------------------------------------------------------------------------- images

# Pillow's own default is 89,478,485 px. A splash screen is under 2 MP, so a
# much tighter ceiling costs us nothing and closes the decompression-bomb door.
# Never set this to None -- that is the one thing Pillow's handbook calls out.
MAX_IMAGE_PIXELS = 64_000_000
MAX_IMAGE_BYTES = 256 * 1024 * 1024

# Allowlist, passed to Image.open(formats=...). EPS is deliberately absent: it
# shells out to Ghostscript, which has a history of sandbox escapes.
ALLOWED_IMAGE_FORMATS = ("PNG", "JPEG", "BMP", "WEBP", "TIFF", "GIF")


class UnsafeInput(Exception):
    """A user-supplied file failed a safety check."""


def open_image(path) -> Image.Image:
    """Image.open with the bomb guards on and the format list pinned."""
    path = Path(path)
    if not path.is_file():
        raise UnsafeInput("not a file: %s" % path)
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise UnsafeInput("%s is %.0f MB; the limit is %.0f MB"
                          % (path.name, size / 1e6, MAX_IMAGE_BYTES / 1e6))

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            # Treat the bomb warning as an error rather than letting it scroll past.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                image = Image.open(path, formats=list(ALLOWED_IMAGE_FORMATS))
                image.load()
            except Image.DecompressionBombWarning as error:
                raise UnsafeInput("%s looks like a decompression bomb: %s" % (path.name, error))
            except Image.DecompressionBombError as error:
                raise UnsafeInput("%s is too large to decode safely: %s" % (path.name, error))
            except Exception as error:
                raise UnsafeInput("could not read %s as an image (%s)" % (path.name, error))
        return image.convert("RGBA")
    finally:
        Image.MAX_IMAGE_PIXELS = previous


# --------------------------------------------------------------------------- themes

HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
THEME_NAME = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
AE_THEME_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

MAX_RAMP_STOPS = 64
MAX_THEME_TARGETS = 32


def _colour(value, field, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not HEX_COLOUR.match(value.strip()):
        raise UnsafeInput("%s must be a hex colour like #7C5CFC, got %r" % (field, value))
    return value.strip()


def _unit(value, field, low=0.0, high=1.0, default=None):
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnsafeInput("%s must be a number between %s and %s, got %r" % (field, low, high, value))
    if not (low <= float(value) <= high):
        raise UnsafeInput("%s must be between %s and %s, got %r" % (field, low, high, value))
    return float(value)


def validate_theme(data: dict) -> dict:
    """Whitelist and bounds-check a theme file before it reaches the patcher.

    A theme is just data, but it drives string substitution into a binary, so
    every field is checked rather than trusted.
    """
    if not isinstance(data, dict):
        raise UnsafeInput("a theme file must contain a JSON object")

    clean = {}

    name = data.get("name", "Custom")
    if not isinstance(name, str) or not THEME_NAME.match(name):
        raise UnsafeInput("theme name must be 1-64 plain characters, got %r" % (name,))
    clean["name"] = name

    ramp = data.get("ramp")
    if ramp is not None:
        if not isinstance(ramp, list) or not (2 <= len(ramp) <= MAX_RAMP_STOPS):
            raise UnsafeInput("ramp must be a list of 2 to %d hex colours" % MAX_RAMP_STOPS)
        clean["ramp"] = [_colour(c, "ramp[%d]" % i) for i, c in enumerate(ramp)]

    for field in ("accent", "accent_soft"):
        if field in data:
            clean[field] = _colour(data[field], field)
    if "guide_color" in data:
        clean["guide_color"] = _colour(data["guide_color"], "guide_color", allow_none=True)

    if data.get("tint_hue") is not None:
        clean["tint_hue"] = _unit(data["tint_hue"], "tint_hue", 0.0, 360.0)
    for field, low, high in (("tint_max_sat", 0.0, 1.0),
                             ("tint_value_ceiling", 0.0, 1.0),
                             ("tint_value_floor", 0.0, 1.0)):
        if field in data and data[field] is not None:
            clean[field] = _unit(data[field], field, low, high)

    if "themes" in data:
        targets = data["themes"]
        if not isinstance(targets, list) or not (1 <= len(targets) <= MAX_THEME_TARGETS):
            raise UnsafeInput("themes must be a list of 1 to %d names" % MAX_THEME_TARGETS)
        for item in targets:
            if not isinstance(item, str) or not AE_THEME_ID.match(item):
                raise UnsafeInput("bad AE theme name %r" % (item,))
        clean["themes"] = list(targets)

    floor = clean.get("tint_value_floor")
    ceiling = clean.get("tint_value_ceiling")
    if floor is not None and ceiling is not None and floor > ceiling:
        raise UnsafeInput("tint_value_floor must not exceed tint_value_ceiling")

    unknown = set(data) - set(clean) - {"tint_hue"}
    if unknown:
        # Not fatal -- forward compatibility -- but the caller may want to say so.
        clean.setdefault("_ignored", sorted(unknown))
    return clean


# --------------------------------------------------------------------------- targets

ADOBE_MARKERS = ("adobe", "after effects")


def binary_identity(path) -> dict:
    """CompanyName / ProductName / FileDescription out of the PE VERSIONINFO."""
    info = {}
    try:
        binary = pefile.PE(str(path), fast_load=True)
    except Exception:
        return info
    try:
        binary.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        for entry in getattr(binary, "FileInfo", []) or []:
            for item in (entry if isinstance(entry, list) else [entry]):
                for table in getattr(item, "StringTable", []) or []:
                    for key, value in table.entries.items():
                        info[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    except Exception:
        pass
    finally:
        binary.close()
    return info


def is_after_effects_binary(path) -> tuple:
    """(ok, why). Guards against the tool being aimed at an unrelated DLL."""
    path = Path(path)
    if not path.is_file():
        return False, "%s does not exist" % path
    info = binary_identity(path)
    haystack = " ".join(
        str(info.get(key, "")) for key in
        ("CompanyName", "ProductName", "FileDescription", "LegalCopyright", "InternalName")
    ).lower()
    if not haystack.strip():
        return False, "%s carries no version information; refusing to patch it" % path.name
    if not any(marker in haystack for marker in ADOBE_MARKERS):
        return False, ("%s does not identify itself as Adobe software (CompanyName=%r); "
                       "refusing to patch it" % (path.name, info.get("CompanyName", "")))
    return True, info.get("ProductName") or info.get("CompanyName") or "Adobe binary"


# --------------------------------------------------------------------------- authenticode

_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_CHOICE_FILE = 1
_WTD_STATE_ACTION_VERIFY = 1
_WTD_STATE_ACTION_CLOSE = 2

_TRUST_E_NOSIGNATURE = 0x800B0100
_TRUST_E_BAD_DIGEST = 0x80096010
_TRUST_E_SUBJECT_FORM_UNKNOWN = 0x800B0003
_CERT_E_UNTRUSTEDROOT = 0x800B0109
_TRUST_E_EXPLICIT_DISTRUST = 0x800B0111


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _WinTrustFileInfo(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
                ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.POINTER(_GUID))]


class _WinTrustData(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WinTrustFileInfo)), ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPWSTR),
        ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


def signature_status(path) -> tuple:
    """(code, human readable) from WinVerifyTrust.

    Informational only, never a gate: patching a DLL invalidates Adobe's
    signature by design, so a modified install reports a digest mismatch and
    that is the expected, correct outcome.
    """
    path = Path(path)
    try:
        wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    except OSError:
        return None, "wintrust unavailable"

    action = _GUID(0x00AAC56B, 0xCD44, 0x11D0,
                   (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = _WinTrustFileInfo(ctypes.sizeof(_WinTrustFileInfo), str(path), None, None)
    data = _WinTrustData()
    ctypes.memset(ctypes.byref(data), 0, ctypes.sizeof(data))
    data.cbStruct = ctypes.sizeof(_WinTrustData)
    data.dwUIChoice = _WTD_UI_NONE
    data.fdwRevocationChecks = _WTD_REVOKE_NONE
    data.dwUnionChoice = _WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = _WTD_STATE_ACTION_VERIFY

    wintrust.WinVerifyTrust.argtypes = [wintypes.HANDLE, ctypes.POINTER(_GUID), ctypes.c_void_p]
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))

    data.dwStateAction = _WTD_STATE_ACTION_CLOSE
    wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))

    code = result & 0xFFFFFFFF
    meanings = {
        0: "signed and trusted (stock Adobe file)",
        _TRUST_E_NOSIGNATURE: "not signed",
        _TRUST_E_BAD_DIGEST: "signed but modified (expected after skinning)",
        _TRUST_E_SUBJECT_FORM_UNKNOWN: "not signed",
        _CERT_E_UNTRUSTEDROOT: "signed by an untrusted root",
        _TRUST_E_EXPLICIT_DISTRUST: "signature explicitly distrusted",
    }
    return code, meanings.get(code, "unverified (0x%08X)" % code)


def looks_stock(path) -> bool:
    code, _ = signature_status(path)
    return code == 0


# --------------------------------------------------------------------------- filesystem

def allowed_restore_roots(installs=None):
    """Directories a backup is permitted to write into."""
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        adobe = Path(appdata) / "Adobe"
        if adobe.is_dir():
            roots.append(adobe.resolve())
    for install in (installs or []):
        try:
            roots.append(Path(install.root).resolve())
        except OSError:
            continue
    return roots


def is_allowed_target(path, roots) -> bool:
    """True when `path` sits inside one of `roots`.

    The manifest inside a snapshot holds absolute paths. We wrote them, but the
    file is plain JSON on disk and could be edited, so restore re-checks rather
    than trusting it.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except (ValueError, OSError):
            continue
    return False


def free_space(path) -> int:
    import shutil as _shutil
    target = Path(path)
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return _shutil.disk_usage(target).free
    except OSError:
        return 0


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Write via a sibling temp file + os.replace so a crash cannot truncate."""
    path = Path(path)
    temp = path.with_name(path.name + ".aeskin-tmp")
    temp.write_text(text, encoding=encoding)
    os.replace(temp, path)
