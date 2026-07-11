#!/usr/bin/env python3
"""Consolidate the Android APK's third-party license material into one asset.

Writes ``android/app/src/main/assets/third_party_licenses.txt`` from three
explicit sources and nothing else:

1. an allowlist of public project license files (LICENSE, parser notices);
2. the *actually installed* Chaquopy Python requirements (numpy and its
   support wheels), read from the Gradle pip output via importlib.metadata,
   so the collected notices always match what ships in the APK;
3. static texts under ``LICENSES/android/`` for bundled runtime components
   that have no dist-info (CPython, the Chaquopy runtime, OpenSSL, SQLite,
   AndroidX/Kotlin).

Like collect_licenses.py, this script never walks the repository and refuses
to read anything under ``private/``.  Re-run it after changing the Chaquopy
``pip { install(...) }`` block or bumping the Chaquopy/Python version, then
commit the regenerated asset.  The Gradle pip directory only exists after a
build (``gradle.bat -p android :app:assembleRelease``).
"""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import re
import sys
from typing import List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Explicit inputs.  Keep every list literal: license collection must never
# turn into a broad repository copy that could pick up gitignored keys.
# --------------------------------------------------------------------------

# Project material relevant to the APK (desktop-only entries such as
# PyOpenGL/Tcl/Tk are deliberately absent).
PROJECT_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("Thermetery Boardview", "LICENSE"),
    ("Third-party notices (parsers)", "THIRD_PARTY_NOTICES.md"),
    ("OpenBoardView (format research)", "LICENSES/OpenBoardView-MIT.txt"),
    ("dhuertas DES (XZZ decryption)", "LICENSES/dhuertas-DES-MIT.txt"),
)

# Chaquopy-installed distributions that ship inside the APK.  All are
# required: if one disappears from the pip output, the requirements block
# in android/app/build.gradle.kts changed and this list must be revisited.
REQUIRED_PIP_DISTRIBUTIONS: Tuple[str, ...] = (
    "numpy",
    "chaquopy-openblas",
    "chaquopy-libgfortran",
    "chaquopy-libcxx",
)

# Bundled runtime components without dist-info metadata.  The version notes
# were verified against the built APK's native libraries (grep of
# libcrypto_python.so / libsqlite3_python.so); re-verify when bumping the
# Chaquopy plugin version.
STATIC_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("CPython 3.13 (embedded via Chaquopy) - PSF License 2.0",
     "LICENSES/android/CPython-PSF-2.0.txt"),
    ("Chaquopy runtime - MIT License",
     "LICENSES/android/Chaquopy-MIT.txt"),
    ("OpenSSL 3.0 (libcrypto/libssl, bundled by Chaquopy) - Apache License 2.0",
     "LICENSES/android/Apache-2.0.txt"),
    ("SQLite (libsqlite3, bundled by Chaquopy) - Public Domain",
     "LICENSES/android/SQLite-blessing.txt"),
    ("AndroidX (core-ktx, activity-ktx) and the Kotlin standard library - "
     "Apache License 2.0",
     "LICENSES/android/Apache-2.0.txt"),
)

DEFAULT_PIP_SUBDIR = "android/app/build/python/pip/release/common"
DEFAULT_OUTPUT_SUBDIR = "android/app/src/main/assets/third_party_licenses.txt"

_LICENSE_BASENAME = re.compile(
    r"^(?:licen[cs]e|copying|copyright|notice)(?:s_bundled)?(?:[._-].*)?$",
    re.IGNORECASE,
)

_RULE = "=" * 72


class CollectionError(RuntimeError):
    """A release-blocking problem with required license collection."""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_project_file(repo_root: Path, relative: str) -> str:
    source = repo_root.joinpath(*relative.split("/"))
    if source.is_symlink():
        raise CollectionError(f"license source must not be a symlink: {relative}")
    if not source.is_file():
        raise CollectionError(f"missing required license material: {relative}")
    resolved = source.resolve()
    if not _is_relative_to(resolved, repo_root):
        raise CollectionError(
            f"license source resolves outside the repository: {relative}"
        )
    if _is_relative_to(resolved, (repo_root / "private").resolve()):
        raise CollectionError(f"refusing to read license material from private/: {relative}")
    return resolved.read_text(encoding="utf-8", errors="replace")


def _dist_info_license_texts(dist_info: Path) -> List[Tuple[str, str]]:
    """(filename, text) for each license file in a dist-info dir, PEP 639 aware.

    Chaquopy's pip output writes no RECORD, so ``Distribution.files`` is None
    and the directory is listed directly instead.
    """
    candidates: List[Path] = [
        entry for entry in dist_info.iterdir()
        if entry.is_file() and _LICENSE_BASENAME.match(entry.name)
    ]
    licenses_dir = dist_info / "licenses"
    if licenses_dir.is_dir():
        candidates.extend(
            entry for entry in licenses_dir.rglob("*") if entry.is_file()
        )
    texts: List[Tuple[str, str]] = []
    for source in sorted(candidates, key=lambda entry: entry.name.casefold()):
        if source.is_symlink():
            continue
        if not _is_relative_to(source.resolve(), dist_info.resolve().parent):
            continue
        texts.append(
            (source.name, source.read_text(encoding="utf-8", errors="replace"))
        )
    return texts


def _collect_pip_sections(pip_dir: Path) -> List[Tuple[str, str]]:
    if not pip_dir.is_dir():
        raise CollectionError(
            f"Chaquopy pip output not found: {pip_dir}\n"
            "  run a build first: gradle.bat -p android :app:assembleRelease"
        )
    install_root = pip_dir.resolve()
    found = {}
    for entry in sorted(install_root.iterdir()):
        if not entry.is_dir() or not entry.name.lower().endswith(".dist-info"):
            continue
        dist = metadata.Distribution.at(entry)
        name = (dist.metadata.get("Name") or "").lower().replace("_", "-")
        found[name] = (dist, entry)
    sections: List[Tuple[str, str]] = []
    for name in REQUIRED_PIP_DISTRIBUTIONS:
        pair = found.get(name.lower())
        if pair is None:
            raise CollectionError(
                f"required APK distribution {name!r} is not in {pip_dir}; "
                "the Chaquopy requirements changed - update "
                "REQUIRED_PIP_DISTRIBUTIONS to match build.gradle.kts"
            )
        dist, dist_info = pair
        texts = _dist_info_license_texts(dist_info)
        if not texts:
            raise CollectionError(
                f"required APK distribution {name!r} ({dist.version}) "
                "ships no license files in its dist-info"
            )
        body = "\n\n".join(
            f"--- {filename} ---\n\n{text.strip()}" for filename, text in texts
        )
        sections.append((f"{dist.metadata['Name']} {dist.version}", body))
    return sections


def _render(sections: Sequence[Tuple[str, str]]) -> str:
    out = [
        "Third-party licenses for Thermetery Boardview (Android)",
        "",
        "Generated by packaging/collect_android_licenses.py - do not edit.",
        "Sources: project license files, the Chaquopy-installed Python",
        "requirements that ship in this APK, and LICENSES/android/.",
        "",
    ]
    for title, body in sections:
        out += [_RULE, title, _RULE, "", body.strip(), "", ""]
    return "\n".join(out)


def collect(repo_root: Path, pip_dir: Path, output: Path) -> int:
    repo_root = repo_root.expanduser().resolve()
    if not repo_root.is_dir():
        raise CollectionError(f"repository root is not a directory: {repo_root}")
    output = output.expanduser().resolve()
    if _is_relative_to(output, (repo_root / "private").resolve()):
        raise CollectionError("output must not be inside the repository's private/")

    sections: List[Tuple[str, str]] = []
    for title, relative in PROJECT_SECTIONS:
        sections.append((title, _read_project_file(repo_root, relative)))
    sections.extend(_collect_pip_sections(pip_dir))
    for title, relative in STATIC_SECTIONS:
        sections.append((title, _read_project_file(repo_root, relative)))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render(sections), encoding="utf-8", newline="\n")
    return len(sections)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument(
        "--pip-dir",
        type=Path,
        default=None,
        help=f"Chaquopy pip output (default: <repo-root>/{DEFAULT_PIP_SUBDIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"asset to write (default: <repo-root>/{DEFAULT_OUTPUT_SUBDIR})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root
    pip_dir = args.pip_dir or repo_root / Path(*DEFAULT_PIP_SUBDIR.split("/"))
    output = args.output or repo_root / Path(*DEFAULT_OUTPUT_SUBDIR.split("/"))
    try:
        count = collect(repo_root, pip_dir, output)
    except (CollectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {count} license sections to {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
