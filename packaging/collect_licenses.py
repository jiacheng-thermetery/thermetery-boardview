#!/usr/bin/env python3
"""Collect release license material into a deterministic directory.

Only an explicit allowlist of public project files and installed runtime
distributions is inspected.  In particular, this script never walks the
repository, and it refuses to use ``private/`` as its destination.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Iterable, Optional, Sequence, Tuple


# Keep this list explicit: release collection must never turn into a broad
# repository copy that could pick up gitignored keys or customer data.
REQUIRED_PROJECT_LICENSES: Tuple[str, ...] = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "LICENSES/README.md",
    "LICENSES/OpenBoardView-MIT.txt",
    "LICENSES/dhuertas-DES-MIT.txt",
    "LICENSES/PyOpenGL-BSD.txt",
    "LICENSES/Tcl-8.6.txt",
    "LICENSES/Tk-8.6.txt",
)

# These distributions are part of the frozen runtime (or contribute its
# bootloader), so release collection fails if their notices are unavailable.
REQUIRED_RUNTIME_DISTRIBUTIONS: Tuple[str, ...] = (
    "numpy",
    "skia-python",
    "pyopengltk",
    "tkinterdnd2",
    # PyInstaller is a build dependency, but its bootloader is part of the
    # generated executable, so its notices are required too.
    "PyInstaller",
)

_LICENSE_BASENAME = re.compile(
    r"^(?:licen[cs]e|copying|copyright|notice)(?:[._-].*)?$",
    re.IGNORECASE,
)
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
# Use 1980-01-02 UTC rather than the exact ZIP epoch. In time zones west of
# UTC, Compress-Archive converts midnight UTC to an unrepresentable 1979 local
# timestamp.
_DEFAULT_EPOCH = 315619200


class CollectionError(RuntimeError):
    """A release-blocking problem with required license collection."""


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return _DEFAULT_EPOCH
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise CollectionError(
            f"SOURCE_DATE_EPOCH must be an integer, got {raw!r}"
        ) from exc
    if value < 0:
        raise CollectionError("SOURCE_DATE_EPOCH must not be negative")
    # Keep one full day clear of the ZIP epoch so local-time conversion remains
    # representable in every time zone.
    return max(value, _DEFAULT_EPOCH)


def _required_project_sources(repo_root: Path) -> Tuple[Tuple[str, Path], ...]:
    missing = []
    result = []
    for relative in REQUIRED_PROJECT_LICENSES:
        source = repo_root.joinpath(*relative.split("/"))
        if source.is_symlink():
            raise CollectionError(
                f"required project license must not be a symlink: {relative}"
            )
        if not source.is_file():
            missing.append(relative)
            continue
        resolved = source.resolve()
        if not _is_relative_to(resolved, repo_root):
            raise CollectionError(
                f"required project license resolves outside the repository: "
                f"{relative}"
            )
        result.append((relative, resolved))

    if missing:
        formatted = "\n  - ".join(missing)
        raise CollectionError(
            "missing required project license material:\n  - " + formatted
        )
    return tuple(result)


def _safe_component(value: str) -> str:
    component = _SAFE_COMPONENT.sub("-", value.strip()).strip(".-")
    return component or "unknown"


def _distribution_license_tail(item: metadata.PackagePath) -> Optional[Tuple[str, ...]]:
    """Return a safe path below a distribution's license output directory.

    Only files stored directly in ``*.dist-info`` under a recognized license
    basename, or files below the PEP 639 ``*.dist-info/licenses`` directory,
    are accepted.  Package source trees and unrelated data are ignored.
    """

    parts = tuple(item.parts)
    if any(part in ("", ".", "..") for part in parts):
        return None

    dist_info_index = next(
        (i for i, part in enumerate(parts) if part.lower().endswith(".dist-info")),
        None,
    )
    if dist_info_index is None:
        return None

    remainder = parts[dist_info_index + 1 :]
    if not remainder:
        return None
    if remainder[0].lower() == "licenses":
        tail = remainder[1:]
        return tail or None
    if len(remainder) == 1 and _LICENSE_BASENAME.match(remainder[0]):
        return remainder
    return None


def _copy_file(source: Path, target: Path, epoch: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    os.utime(target, (epoch, epoch))


def _collect_distribution(
    distribution_name: str,
    staging: Path,
    epoch: int,
    required: bool = False,
) -> int:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError:
        if required:
            raise CollectionError(
                f"required runtime distribution {distribution_name!r} is not installed"
            )
        _warn(
            f"optional runtime distribution {distribution_name!r} is not "
            "installed; no package license was collected"
        )
        return 0

    canonical_name = distribution.metadata.get("Name") or distribution_name
    package_dir = staging / "runtime-packages" / _safe_component(canonical_name)
    install_root = Path(distribution.locate_file("")).resolve()

    candidates = []
    for item in sorted(distribution.files or (), key=lambda value: str(value).casefold()):
        tail = _distribution_license_tail(item)
        if tail is not None:
            candidates.append((item, tail))

    if not candidates:
        if required:
            raise CollectionError(
                f"required runtime distribution {canonical_name!r} "
                f"({distribution.version}) exposes no license files"
            )
        _warn(
            f"installed runtime distribution {canonical_name!r} "
            f"({distribution.version}) exposes no license files in its "
            "package metadata"
        )
        return 0

    copied = 0
    seen_targets = set()
    for item, tail in candidates:
        source = Path(distribution.locate_file(item))
        if source.is_symlink():
            _warn(f"skipping symlinked package license: {source}")
            continue
        if not source.is_file():
            _warn(f"declared package license is missing: {source}")
            continue

        resolved = source.resolve()
        if not _is_relative_to(resolved, install_root):
            _warn(f"skipping package license outside its install root: {source}")
            continue

        target = package_dir.joinpath(*tail)
        target_key = target.relative_to(staging).as_posix().casefold()
        if target_key in seen_targets:
            _warn(f"skipping duplicate package license target: {target}")
            continue
        seen_targets.add(target_key)
        _copy_file(resolved, target, epoch)
        copied += 1

    if copied == 0:
        if required:
            raise CollectionError(
                f"no safe license files could be copied for required runtime "
                f"distribution {canonical_name!r} ({distribution.version})"
            )
        _warn(
            f"no safe license files could be copied for installed runtime "
            f"distribution {canonical_name!r} ({distribution.version})"
        )
    return copied


def _runtime_license_candidates() -> Iterable[Tuple[str, Path, Path]]:
    base = Path(sys.base_prefix).resolve()
    yield "CPython", base / "LICENSE.txt", Path("runtime") / "CPython-LICENSE.txt"

    try:
        import tkinter

        version = f"{tkinter.TclVersion:.1f}"
    except (ImportError, AttributeError):
        version = "8.6"

    tcl_root = base / "tcl"
    yield (
        "Tcl",
        tcl_root / f"tcl{version}" / "license.terms",
        Path("runtime") / "Tcl-license.terms",
    )
    yield (
        "Tk",
        tcl_root / f"tk{version}" / "license.terms",
        Path("runtime") / "Tk-license.terms",
    )


def _collect_runtime_licenses(staging: Path, epoch: int) -> int:
    copied = 0
    base = Path(sys.base_prefix).resolve()
    for label, source, target_relative in _runtime_license_candidates():
        if source.is_symlink():
            if label == "CPython":
                raise CollectionError(f"required CPython license is a symlink: {source}")
            _warn(f"skipping symlinked optional {label} license: {source}")
            continue
        if not source.is_file():
            if label == "CPython":
                raise CollectionError(f"required CPython license was not found at {source}")
            _warn(f"optional {label} runtime license was not found at {source}")
            continue
        resolved = source.resolve()
        if not _is_relative_to(resolved, base):
            if label == "CPython":
                raise CollectionError("required CPython license is outside the Python install")
            _warn(f"skipping optional {label} license outside the Python install")
            continue
        _copy_file(resolved, staging / target_relative, epoch)
        copied += 1
    return copied


def _write_manifest(staging: Path, epoch: int) -> None:
    rows = []
    for path in sorted(
        (candidate for candidate in staging.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(staging).as_posix().casefold(),
    ):
        relative = path.relative_to(staging).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {relative}")
    manifest = staging / "SHA256SUMS.txt"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    os.utime(manifest, (epoch, epoch))


def _normalize_timestamps(root: Path, epoch: int) -> None:
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        # The staging tree is created by this process and never contains
        # symlinks.  Avoid follow_symlinks=False here because os.utime does
        # not implement that option on every supported Windows Python build.
        os.utime(path, (epoch, epoch))
    os.utime(root, (epoch, epoch))


def collect_licenses(repo_root: Path, destination: Path) -> int:
    repo_root = repo_root.expanduser().resolve()
    if not repo_root.is_dir():
        raise CollectionError(f"repository root is not a directory: {repo_root}")

    project_sources = _required_project_sources(repo_root)
    destination = destination.expanduser().resolve()
    private_root = (repo_root / "private").resolve()
    if _is_relative_to(destination, private_root):
        raise CollectionError("destination must not be inside the repository's private/")
    for _, source in project_sources:
        if _is_relative_to(source, destination):
            raise CollectionError(
                "destination would contain and overwrite required project licenses: "
                f"{destination}"
            )
    if destination.is_symlink():
        raise CollectionError(f"destination must not be a symlink: {destination}")
    if destination.exists() and not destination.is_dir():
        raise CollectionError(f"destination exists and is not a directory: {destination}")

    epoch = _source_date_epoch()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.licenses-", dir=destination.parent)
    )

    copied = 0
    try:
        for relative, source in project_sources:
            _copy_file(source, staging.joinpath(*relative.split("/")), epoch)
            copied += 1

        copied += _collect_runtime_licenses(staging, epoch)
        for distribution_name in REQUIRED_RUNTIME_DISTRIBUTIONS:
            copied += _collect_distribution(
                distribution_name, staging, epoch, required=True
            )

        _write_manifest(staging, epoch)
        _normalize_timestamps(staging, epoch)

        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return copied


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="project repository root containing LICENSE and LICENSES/",
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        help="distribution license directory to replace deterministically",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        copied = collect_licenses(args.repo_root, args.destination)
    except (CollectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"collected {copied} license files into {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
