# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Probe driver for signal_match.py. Loads `private/rules.yaml` plus an
extracted schematic index, runs the matcher, and prints:

  - per-tier hit counts (exact / normalized / substring)
  - overall match rate vs the naive baseline
  - a sample of newly-matched rule tokens (with their best candidates)
  - the remaining unmatched rule tokens, grouped by best confidence

Usage:
    python signal_match_probe.py                 # default B550/Z490/MS-7680
    python signal_match_probe.py PDF...          # custom schematic set
    python signal_match_probe.py --rules R.yaml  # custom rules file
    python signal_match_probe.py --sample N      # show N matched samples
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import yaml

from schematic_text import extract_index, SchematicIndex
from signal_match import (
    MatchCandidate,
    build_match_index,
    find_signal_candidates,
    normalize,
    tokenize_rule_entry,
)


DEFAULT_PDFS = [
    "schematics/B550_AORUS_PRO_AC_REV1.0.pdf",
    "schematics/Z490 VISION G Rev10.pdf",
    "schematics/MSI_MS-7680_r10.pdf",
]
DEFAULT_RULES = "private/rules.yaml"


def _harvest_rule_signals(rules_obj) -> Set[str]:
    """Walk the rules YAML and pull out every string that's tagged as
    a signal/net/probe/rail entry. Naming inconsistency in the YAML
    means we cast a wide net via key-name match."""
    keys = {"signal", "signals", "net", "nets",
            "probe", "probes", "rail", "rails"}
    out: Set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys and isinstance(v, (str, list)):
                    items = [v] if isinstance(v, str) else v
                    for s in items:
                        if isinstance(s, str):
                            out.add(s)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(rules_obj)
    return out


def _classify(candidates: List[MatchCandidate]) -> str:
    """Return the best tier the candidate list reaches, for stats."""
    if not candidates:
        return "miss"
    return candidates[0].kind


def _format_candidate(c: MatchCandidate) -> str:
    return f"{c.match!r} ({c.kind}, conf={c.confidence:.2f})"


def _print_board_report(
    label: str,
    pdf_path: Path,
    rule_tokens: Set[str],
    sample_n: int,
) -> None:
    idx: SchematicIndex = extract_index(pdf_path)
    if not idx.has_text:
        print(f"{label}: SKIP -- image-only PDF")
        return

    schem = list(idx.pages_by_signal.keys())
    schem_set = set(schem)
    match_index = build_match_index(schem)

    print(f"=== {label}  ({pdf_path.name}) ===")
    print(f"  schematic tokens : {len(schem)}")
    print(f"  rule tokens      : {len(rule_tokens)}")

    # Naive baseline: exact original-string match against the raw
    # schematic signal pool. Same metric we showed in the audit.
    naive_hits = rule_tokens & schem_set
    naive_pct = 100 * len(naive_hits) / len(rule_tokens) if rule_tokens else 0
    print(f"  naive exact-match: "
          f"{len(naive_hits):>4} ({naive_pct:.1f}%)")

    # Tiered match via signal_match.
    kinds: Counter = Counter()
    newly_matched: List[Tuple[str, MatchCandidate]] = []
    misses: List[str] = []
    for tok in sorted(rule_tokens):
        cands = find_signal_candidates(tok, match_index)
        kind = _classify(cands)
        kinds[kind] += 1
        if cands and tok not in naive_hits:
            newly_matched.append((tok, cands[0]))
        if not cands:
            misses.append(tok)

    n_hit = len(rule_tokens) - kinds["miss"]
    pct = 100 * n_hit / len(rule_tokens) if rule_tokens else 0
    print(f"  fuzzy match total: "
          f"{n_hit:>4} ({pct:.1f}%)   "
          f"gain: +{pct - naive_pct:.1f}pp")
    print(f"    exact          : {kinds['exact']:>4}")
    print(f"    normalized     : {kinds['normalized']:>4}")
    print(f"    substring      : {kinds['substring']:>4}")
    print(f"    substring_rev  : {kinds['substring_rev']:>4}")
    print(f"    miss           : {kinds['miss']:>4}")

    if sample_n > 0 and newly_matched:
        print(f"  newly-matched sample (top {sample_n} by rule alpha):")
        newly_matched.sort(key=lambda x: x[0])
        for tok, cand in newly_matched[:sample_n]:
            print(f"    {tok!r:<28} -> {_format_candidate(cand)}")
    if sample_n > 0 and misses:
        print(f"  remaining-miss sample (first {min(sample_n, len(misses))}):")
        for m in misses[:sample_n]:
            print(f"    {m!r}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("pdfs", nargs="*", default=DEFAULT_PDFS,
                    help="Schematic PDFs to evaluate (default: B550/Z490/MS-7680)")
    ap.add_argument("--rules", default=DEFAULT_RULES,
                    help=f"Rules YAML path (default: {DEFAULT_RULES})")
    ap.add_argument("--sample", type=int, default=15,
                    help="Show N newly-matched + N missed examples per board")
    args = ap.parse_args()

    rules_path = Path(args.rules)
    if not rules_path.exists():
        print(f"rules file not found: {rules_path}", file=sys.stderr)
        sys.exit(1)
    with open(rules_path, encoding="utf-8") as f:
        rules = yaml.safe_load(f)

    # Harvest every signal/net/probe/rail string in the YAML, then
    # tokenize each entry into the components a matcher should try.
    raw_entries = _harvest_rule_signals(rules)
    rule_tokens: Set[str] = set()
    for entry in raw_entries:
        rule_tokens.update(tokenize_rule_entry(entry))

    print(f"Rules YAML        : {rules_path}")
    print(f"Raw entries       : {len(raw_entries)}")
    print(f"Tokenized signals : {len(rule_tokens)}")
    print()

    for pdf_str in args.pdfs:
        pdf = Path(pdf_str)
        if not pdf.exists():
            print(f"SKIP missing: {pdf}", file=sys.stderr)
            continue
        label = pdf.stem[:34]
        _print_board_report(label, pdf, rule_tokens, args.sample)


if __name__ == "__main__":
    main()
