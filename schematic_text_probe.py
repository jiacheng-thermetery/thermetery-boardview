# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Probe driver for schematic_text.py.

Runs extract_index() on every PDF in `schematics/` (or the directory
given on the CLI) and prints a summary table. Useful for sanity-checking
the extractor across vendors and spotting regressions when the
classifier changes.

Usage:
    python schematic_text_probe.py                 # default: ./schematics
    python schematic_text_probe.py path/to/dir
    python schematic_text_probe.py path/to/file.pdf
    python schematic_text_probe.py path/to/file.pdf --titles    # dump per-page titles
    python schematic_text_probe.py path/to/file.pdf --signals N # show top-N signals
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List

from schematic_text import extract_index, SchematicIndex


def _summarise(idx: SchematicIndex) -> str:
    """One-line summary: pages, signals, power-sequence pages, time."""
    pwr = idx.power_sequence_pages
    pwr_str = ",".join(str(p) for p in pwr) if pwr else "-"
    return (f"{idx.page_count:>3}p, "
            f"{idx.signal_count:>5} signals, "
            f"pwr-seq pages: {pwr_str}, "
            f"text={'yes' if idx.has_text else 'IMAGE-ONLY'}")


def _print_titles(idx: SchematicIndex) -> None:
    """Page-by-page title dump. Useful when debugging the title parser
    against a new vendor PDF — you can immediately see which sheets
    were classified vs left blank."""
    for p in idx.pages:
        title = p.title or "(no title parsed)"
        n_sig = len(p.signals)
        print(f"  p.{p.num:>3}  ({n_sig:>3} signals)  {title}")


def _print_top_signals(idx: SchematicIndex, n: int) -> None:
    """Top N signals by page count. Common power rails (VCC3, +12V,
    GND, etc.) tend to dominate; skim past them to find the meatier
    domain-specific signals (PM_PWROK, SLP_S3-, AVDDP_EN, ...)"""
    counts: Counter = Counter()
    for sig, pages in idx.pages_by_signal.items():
        counts[sig] = len(pages)
    print(f"  Top {n} signals by page count:")
    for sig, c in counts.most_common(n):
        print(f"    {sig:<24} {c:>3} pages")


def _resolve_targets(arg: str) -> List[Path]:
    """Accept a directory, a single .pdf file, or a glob; return a
    sorted list of PDFs we'll probe."""
    p = Path(arg)
    if p.is_file() and p.suffix.lower() == ".pdf":
        return [p]
    if p.is_dir():
        return sorted(p.glob("*.pdf"))
    # Treat as glob
    out = sorted(Path(".").glob(arg))
    return [x for x in out if x.suffix.lower() == ".pdf"]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Probe schematic_text.py on one or many PDFs.")
    ap.add_argument("target", nargs="?", default="schematics",
                    help="Directory, single .pdf, or glob (default: schematics)")
    ap.add_argument("--titles", action="store_true",
                    help="Dump per-page titles for each PDF")
    ap.add_argument("--signals", type=int, metavar="N", default=0,
                    help="Show top-N signals by page count for each PDF")
    args = ap.parse_args()

    targets = _resolve_targets(args.target)
    if not targets:
        print(f"No PDFs found at: {args.target}", file=sys.stderr)
        sys.exit(1)

    print(f"Probing {len(targets)} PDF(s):")
    print()

    for path in targets:
        t0 = time.perf_counter()
        try:
            idx = extract_index(path)
        except Exception as exc:
            print(f"{path.name}: FAILED — {exc}")
            continue
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"{path.name}")
        print(f"  {_summarise(idx)} ({elapsed:.0f} ms)")
        if args.titles:
            _print_titles(idx)
        if args.signals > 0:
            _print_top_signals(idx, args.signals)
        print()


if __name__ == "__main__":
    main()
