#!/usr/bin/env python3
"""Generate the Windows ICO and PyInstaller version resource."""

from __future__ import annotations

import argparse
from pathlib import Path
import re

from PIL import Image


SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def _write_icon(source: Path, destination: Path) -> None:
    image = Image.open(source).convert("RGBA")
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(image, ((side - image.width) // 2, (side - image.height) // 2))
    destination.parent.mkdir(parents=True, exist_ok=True)
    square.save(
        destination,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


def _write_version(version: str, destination: Path) -> None:
    match = SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported semantic version: {version!r}")
    numbers = tuple(int(value) for value in match.groups()) + (0,)
    dotted = ", ".join(str(value) for value in numbers)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({dotted}),
    prodvers=({dotted}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Thermetery Technology LLC'),
        StringStruct('FileDescription', 'PCB boardview viewer'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'ThermeteryBoardviewer'),
        StringStruct('LegalCopyright', 'Copyright (C) 2026 Thermetery Technology LLC'),
        StringStruct('OriginalFilename', 'ThermeteryBoardviewer.exe'),
        StringStruct('ProductName', 'Thermetery Boardviewer'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--icon-source", required=True, type=Path)
    parser.add_argument("--icon-output", required=True, type=Path)
    parser.add_argument("--version-output", required=True, type=Path)
    args = parser.parse_args()
    _write_icon(args.icon_source, args.icon_output)
    _write_version(args.version, args.version_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
