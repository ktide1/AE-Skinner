"""Read and rewrite named resources inside a PE binary (AfterFXLib.dll etc).

Nothing here knows anything about After Effects. It enumerates whatever the
binary actually contains, which is what lets one tool cover every AE version.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import pefile

# Numeric resource types that have well-known names.
STANDARD_TYPES = {
    1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG", 6: "STRING",
    7: "FONTDIR", 8: "FONT", 9: "ACCELERATOR", 10: "RCDATA", 11: "MESSAGETABLE",
    12: "GROUP_CURSOR", 14: "GROUP_ICON", 16: "VERSION", 17: "DLGINCLUDE",
    19: "PLUGPLAY", 20: "VXD", 21: "ANICURSOR", 22: "ANIICON", 23: "HTML",
    24: "MANIFEST",
}


@dataclass(frozen=True)
class Resource:
    type_name: str          # "PNG", "XML", "RCDATA", ...
    type_id: int | None     # numeric id when the type is not a string
    name: str               # "AE_BETA_SPLASH" or "103"
    name_is_string: bool
    lang: int
    data: bytes

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.type_name, self.name, self.lang)

    def label(self) -> str:
        return f"{self.type_name}/{self.name}/{self.lang}"


def iter_resources(path: Path, types: set[str] | None = None, name_filter=None):
    """Yield resources, optionally filtered by type name and by entry name.

    `name_filter` is applied before the resource bytes are read. That matters:
    AfterFXLib.dll carries ~2,400 PNG resources totalling tens of megabytes,
    and a splash scan only wants a dozen of them.
    """
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    try:
        if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
            return
        for type_entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if type_entry.name is not None:
                type_name, type_id = str(type_entry.name), None
            else:
                type_id = type_entry.id
                type_name = STANDARD_TYPES.get(type_id, str(type_id))
            if types is not None and type_name not in types:
                continue
            if not hasattr(type_entry, "directory"):
                continue
            for entry in type_entry.directory.entries:
                name_is_string = entry.name is not None
                name = str(entry.name) if name_is_string else str(entry.id)
                if name_filter is not None and not name_filter(name):
                    continue
                if not hasattr(entry, "directory"):
                    continue
                for lang in entry.directory.entries:
                    try:
                        data = pe.get_data(lang.data.struct.OffsetToData, lang.data.struct.Size)
                    except Exception:
                        continue
                    yield Resource(type_name, type_id, name, name_is_string, lang.id, data)
    finally:
        pe.close()


def read_resources(path: Path, types: set[str] | None = None) -> dict[tuple[str, str, int], Resource]:
    return {res.key: res for res in iter_resources(path, types)}


# --------------------------------------------------------------------------- writing

def _kernel32():
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    lib.BeginUpdateResourceW.restype = wintypes.HANDLE
    lib.UpdateResourceW.argtypes = [
        wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.WORD, wintypes.LPVOID, wintypes.DWORD,
    ]
    lib.UpdateResourceW.restype = wintypes.BOOL
    lib.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    lib.EndUpdateResourceW.restype = wintypes.BOOL
    return lib


def _res_arg(value: str, is_string: bool):
    """Resource type/name argument: a string, or MAKEINTRESOURCE for numeric ids."""
    if is_string:
        return ctypes.c_wchar_p(value)
    return ctypes.cast(ctypes.c_void_p(int(value)), wintypes.LPCWSTR)


def write_resources(path: Path, updates: list[tuple[Resource, bytes]], log=print) -> None:
    """Replace resources in place. `updates` pairs an existing Resource with new bytes."""
    if not updates:
        return
    lib = _kernel32()
    handle = lib.BeginUpdateResourceW(str(path), False)
    if not handle:
        err = ctypes.get_last_error()
        hint = " (file is open, or needs administrator rights)" if err in (5, 32) else ""
        raise OSError(f"BeginUpdateResource failed on {path.name}: error {err}{hint}")
    try:
        for res, data in updates:
            buffer = ctypes.create_string_buffer(data, len(data))
            ok = lib.UpdateResourceW(
                handle,
                _res_arg(res.type_name if res.type_id is None else str(res.type_id), res.type_id is None),
                _res_arg(res.name, res.name_is_string),
                res.lang, buffer, len(data),
            )
            if not ok:
                raise OSError(f"UpdateResource failed for {res.label()}: error {ctypes.get_last_error()}")
            log(f"    queued {res.label()} ({len(data)} bytes)")
        if not lib.EndUpdateResourceW(handle, False):
            raise OSError(f"EndUpdateResource failed on {path.name}: error {ctypes.get_last_error()}")
        handle = None
    finally:
        if handle:
            lib.EndUpdateResourceW(handle, True)  # discard the whole batch


def fix_checksum(path: Path) -> None:
    """Recompute the PE header checksum so Windows keeps loading the binary."""
    pe = pefile.PE(str(path))
    try:
        pe.OPTIONAL_HEADER.CheckSum = pe.generate_checksum()
        pe.write(str(path))
    finally:
        pe.close()


# --------------------------------------------------------------------------- misc

def png_size(data: bytes) -> tuple[int, int] | None:
    """Width/height straight out of the IHDR, without decoding the image."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])  # type: ignore[return-value]


def file_version(path: Path) -> str | None:
    """Dotted FileVersion from the binary's VERSIONINFO, e.g. '24.6.1'."""
    try:
        pe = pefile.PE(str(path), fast_load=True)
    except Exception:
        return None
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        info = getattr(pe, "VS_FIXEDFILEINFO", None)
        if info:
            entry = info[0]
            return "{}.{}.{}".format(
                entry.FileVersionMS >> 16,
                entry.FileVersionMS & 0xFFFF,
                entry.FileVersionLS >> 16,
            )
    except Exception:
        return None
    finally:
        pe.close()
    return None
