#!/usr/bin/env python3
"""Generate a PyInstaller-compatible Windows version-info file."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


COMPANY_NAME = "GrizzlyOne95"
FILE_DESCRIPTION = "Battlezone Lobby Monitor"
INTERNAL_NAME = "BZLobbyMonitor"
ORIGINAL_FILENAME = "BZLobbyMonitor.exe"
PRODUCT_NAME = "Battlezone Modding Tools"


def normalize_version(value: str) -> tuple[tuple[int, int, int, int], str]:
    display = value.strip()
    if display.lower().startswith("v"):
        display = display[1:]

    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", display)
    if not match:
        return (0, 0, 0, 0), display or "0.0.0"

    parts = [int(part) if part is not None else 0 for part in match.groups(default="0")]
    return tuple(parts), display


def build_version_info(version: str) -> str:
    numeric, display = normalize_version(version)
    tuple_text = ", ".join(str(part) for part in numeric)

    return f"""# UTF-8\nVSVersionInfo(\n  ffi=FixedFileInfo(\n    filevers=({tuple_text}),\n    prodvers=({tuple_text}),\n    mask=0x3f,\n    flags=0x0,\n    OS=0x40004,\n    fileType=0x1,\n    subtype=0x0,\n    date=(0, 0)\n  ),\n  kids=[\n    StringFileInfo([\n      StringTable(\n        u'040904B0',\n        [\n          StringStruct(u'CompanyName', u'{COMPANY_NAME}'),\n          StringStruct(u'FileDescription', u'{FILE_DESCRIPTION}'),\n          StringStruct(u'FileVersion', u'{display}'),\n          StringStruct(u'InternalName', u'{INTERNAL_NAME}'),\n          StringStruct(u'OriginalFilename', u'{ORIGINAL_FILENAME}'),\n          StringStruct(u'ProductName', u'{PRODUCT_NAME}'),\n          StringStruct(u'ProductVersion', u'{display}')\n        ]\n      )\n    ]),\n    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])\n  ]\n)\n"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="Release version, for example 1.2.4 or v1.2.4")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_version_info(args.version), encoding="utf-8")


if __name__ == "__main__":
    main()
