# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Cross-reference rules.yaml signals against a GENCAD boardview, producing
"linked" probe instructions for the walker.

Each rule signal is resolved to a boardview net (with fuzzy matching) and
then to the list of (refdes, pin) pairs on that net, with each pair enriched
by the component's board location.

Usage:
    python linker.py <rules.yaml> <board.cad> <platform_prefix>

The platform_prefix is matched against the first platform key in rules.yaml
that starts with it (so you can pass `Intel 6X-7X` without typing the
Chinese suffix).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from boardview import BoardModel, parse


_NET_NAME_RE = re.compile(r"^[A-Za-z0-9_#/+\-]+$")


# Intel chipset signals → known aliases used on real boardviews. Rules use
# generic Intel reference names; the board's net list often uses a brand or
# design-specific variant. Add entries here as we encounter them.
INTEL_ALIASES: Dict[str, List[str]] = {
    "VCCRTC":     ["VBAT", "VCC_RTC", "RTC_VCC", "VCCRTC_BAT"],
    "VCCDSW3_3":  ["VCCSUS3_3", "VCC3_DSW", "VCC_DSW_3P3", "VCCDSW_3P3", "DCPSUS"],
    "VCCSUS3_3":  ["VCCSUS_3P3", "VCC_SUS", "DCPSUS", "5VREF_SUS"],
    "DCPRTC":     ["DCPSUS"],
    "PS_ON#":     ["PSON#", "PSON", "ATX_PSON#"],
    "PWRBTN_IN":  ["PWRBTIN", "PWRBTN", "PWRBTN_IN_R"],
    "SUSCLK":     ["SUS_CLK", "SUSCLK#"],
    "PWROK":      ["SYS_PWROK", "ATX_PWR_OK", "PCH_PWROK", "H_PWRGD"],
    "PLT_RST#":   ["PLTRST#", "PLTRST"],
    "BATLOW":     ["BATLOW#", "BATLOW_N"],
    "PCH_MEM_PWRGD": ["MEM_PWRGD", "MEM_PWRGD_R"],
}

# Suffixes seen on the MSI MS-7680 boardview where a single Intel signal is
# split between PCH side and Super-I/O side via a 0Ω resistor.
_BOARD_SUFFIXES = ["_CP", "_SIO", "_R", "_PCH", "_CPU"]


def _expand_with_aliases(name: str) -> List[str]:
    """Generate Intel-alias candidates for `name`."""
    keys_to_try = [name, name.upper(), name.rstrip("#"), name.rstrip("#").upper()]
    out = []
    for k in keys_to_try:
        out.extend(INTEL_ALIASES.get(k, []))
    return out


def _underscore_variants(name: str) -> List[str]:
    """SUSCLK → SUS_CLK; SLP_SUS → SLPSUS. Common in Intel reference vs
    board-specific naming."""
    out = [name.replace("_", "")]
    # Insert underscore after well-known prefixes
    for prefix in ("SUS", "SLP", "PWR", "VCC", "VTT", "VDD", "DCP", "INT", "RTC"):
        if (name.startswith(prefix)
                and not name.startswith(prefix + "_")
                and len(name) > len(prefix)):
            out.append(name[:len(prefix)] + "_" + name[len(prefix):])
    return out


def _looks_like_net_name(s: str) -> bool:
    """Reject descriptive/procedural cells masquerading as signals.
    Real net names are short, ASCII, no whitespace, no Chinese chars,
    and don't start with a continuation arrow."""
    if not s:
        return False
    s = s.strip()
    if not s or s.startswith("->"):
        return False
    if any("一" <= c <= "鿿" for c in s):
        return False
    return bool(_NET_NAME_RE.match(s))


def _candidate_net_names(net: str) -> List[str]:
    """Expand 'BCLK/BCLK#' or 'VDDQ/VCC_DDR' into [BCLK, BCLK#] / [VDDQ, VCC_DDR].
    Returns the cleaned base first, then any slash-split parts."""
    if not net:
        return []
    base = net.strip().rstrip(":")
    if not base:
        return []
    out = [base]
    # Only split on `/` if both sides look like net names (avoids splitting
    # things like "I/O" or "1.5V/0V").
    if "/" in base:
        parts = [p.strip() for p in base.split("/") if p.strip()]
        if all(_looks_like_net_name(p) for p in parts):
            out.extend(parts)
    return out


@dataclass
class ProbePoint:
    refdes: str
    pin: str
    component_x: float
    component_y: float
    layer: str
    device: str


@dataclass
class LinkedSignal:
    raw: Optional[str] = None
    net: Optional[str] = None
    expected_voltage: Optional[str] = None
    resistance_to_ground: Optional[str] = None
    semantic: Optional[str] = None
    note: Optional[str] = None
    step: Optional[str] = None
    rule_probe_at: List[Dict[str, str]] = field(default_factory=list)
    boardview_net: Optional[str] = None
    probe_candidates: List[ProbePoint] = field(default_factory=list)


def _resolve_to_board(net_name: Optional[str], board: BoardModel) -> Optional[str]:
    """Resolve a rule's signal name to a canonical board net, trying:
        1. exact + fuzzy (#-toggle, case, etc., from BoardModel.find_signal)
        2. slash-split variants (BCLK/BCLK# → BCLK, BCLK#)
        3. Intel alias table (VCCRTC → VBAT)
        4. _CP/_SIO/_R suffix variants (MSI splits signals at 0Ω resistors)
        5. underscore insertion/removal (SUSCLK ↔ SUS_CLK)
    """
    if not net_name:
        return None

    for candidate in _candidate_net_names(net_name):
        if not _looks_like_net_name(candidate):
            continue

        # 1. Direct fuzzy match
        hit = board.find_signal(candidate, fuzzy=True)
        if hit:
            return hit

        # Build the full set of variants to try
        variants: List[str] = [candidate]
        variants.extend(_expand_with_aliases(candidate))
        variants.extend(_underscore_variants(candidate))

        # 2-3-5. Try each variant directly
        for v in variants:
            hit = board.find_signal(v, fuzzy=True)
            if hit:
                return hit

        # 4. Try board-side suffixes on every variant
        for v in variants:
            for suffix in _BOARD_SUFFIXES:
                hit = board.find_signal(v + suffix, fuzzy=False)
                if hit:
                    return hit
                # Also try if v ends with #, suffix goes after the # base
                if v.endswith("#"):
                    base = v.rstrip("#")
                    hit = board.find_signal(base + suffix, fuzzy=False)
                    if hit:
                        return hit
                    hit = board.find_signal(base + suffix + "#", fuzzy=False)
                    if hit:
                        return hit

    # 5. signal_match fuzzy fallback. The board may decorate the "real"
    # signal name with a vendor prefix (Gigabyte Z490 uses `N_-RTCRST`
    # for `RTCRST`, `N_-CPURST` for `CPURST`, etc.) that the variant
    # gymnastics above don't undo. Normalize both sides through
    # signal_match and look for a high-confidence match.
    #
    # Threshold 0.85: only `exact` and `normalized` matches qualify.
    # We deliberately exclude substring-level matches here — the linker
    # picks ONE canonical net to drive the probe display, so a wrong
    # auto-pick can mislead the technician. Substring candidates are
    # still surfaced as schematic-page hints elsewhere, where the
    # confidence is shown alongside.
    hit = _signal_match_resolve(net_name, board, min_confidence=0.85)
    if hit:
        return hit

    return None


# Per-board cache of the signal_match normalized index. Built lazily on
# first fallback query and reused for the rest of the linking run; we
# only invalidate when the board object itself changes (by attribute,
# not by mutating signals — assumed stable for the lifetime of a load).
def _get_board_match_index(board: BoardModel):
    idx = getattr(board, "_signal_match_idx", None)
    if idx is not None:
        return idx
    try:
        from signal_match import build_match_index
    except Exception:
        return None
    idx = build_match_index(board.signals.keys())
    # Cache directly on the board. Slight monkey-patch, but BoardModel
    # is a plain dataclass and the alternative (a module-level WeakDict
    # keyed by id(board)) loses entries to GC unpredictably.
    board._signal_match_idx = idx
    return idx


def _signal_match_resolve(
    net_name: str, board: BoardModel, *, min_confidence: float,
) -> Optional[str]:
    """Run the signal_match fuzzy matcher against the board's net pool.
    Returns the top candidate's canonical net name if it clears
    `min_confidence`, else None. Used as the last-resort fallback in
    `_resolve_to_board`."""
    try:
        from signal_match import find_signal_candidates
    except Exception:
        return None
    idx = _get_board_match_index(board)
    if idx is None:
        return None
    cands = find_signal_candidates(
        net_name, idx, max_candidates=3, min_confidence=min_confidence,
    )
    for c in cands:
        if c.match in board.signals:
            return c.match
    return None


def link_signals(entries: List[Dict[str, Any]], board: BoardModel) -> List[LinkedSignal]:
    out: List[LinkedSignal] = []
    for entry in entries:
        sig = entry.get("signal") or {}
        ls = LinkedSignal(
            raw=sig.get("raw"),
            net=sig.get("net"),
            expected_voltage=entry.get("expected_voltage"),
            resistance_to_ground=entry.get("resistance_to_ground"),
            semantic=entry.get("semantic"),
            note=entry.get("note"),
            step=entry.get("step"),
            rule_probe_at=sig.get("probe_at") or [],
        )
        canonical = _resolve_to_board(sig.get("net") or sig.get("raw"), board)
        if canonical:
            ls.boardview_net = canonical
            for refdes, pin in board.signals[canonical]:
                comp = board.components.get(refdes)
                if comp:
                    ls.probe_candidates.append(ProbePoint(
                        refdes=refdes,
                        pin=pin,
                        component_x=comp.x,
                        component_y=comp.y,
                        layer=comp.layer,
                        device=comp.device,
                    ))
        out.append(ls)
    return out


def _resolve_platform_key(rules_data: Dict[str, Any], prefix: str) -> str:
    candidates = [k for k in rules_data["platforms"] if k.startswith(prefix)]
    if not candidates:
        avail = list(rules_data["platforms"].keys())
        raise SystemExit(f"No platform starts with {prefix!r}. Available: {avail}")
    if len(candidates) > 1:
        raise SystemExit(f"Prefix {prefix!r} is ambiguous: {candidates}")
    return candidates[0]


def link_platform(rules_path: Path, board_path: Path, platform_prefix: str) -> Dict[str, Any]:
    rules_data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    platform_key = _resolve_platform_key(rules_data, platform_prefix)
    plat = rules_data["platforms"][platform_key]
    board = parse(board_path)

    sections_out = []
    for section in plat.get("sections", []):
        stages_out = []
        for stage in section.get("stages", []):
            linked = link_signals(stage.get("signals", []), board)
            stages_out.append({
                "label": stage.get("label"),
                "signals": [
                    {
                        "raw": ls.raw,
                        "net": ls.net,
                        "expected_voltage": ls.expected_voltage,
                        "resistance_to_ground": ls.resistance_to_ground,
                        "semantic": ls.semantic,
                        "note": ls.note,
                        "step": ls.step,
                        "rule_probe_at": ls.rule_probe_at,
                        "boardview_net": ls.boardview_net,
                        "probe_candidates": [
                            {
                                "refdes": p.refdes,
                                "pin": p.pin,
                                "x": p.component_x,
                                "y": p.component_y,
                                "layer": p.layer,
                                "device": p.device,
                            }
                            for p in ls.probe_candidates[:8]  # cap to keep output tame
                        ],
                    }
                    for ls in linked
                ],
            })
        sections_out.append({
            "id": section.get("id"),
            "diagnosis_summary": section.get("diagnosis_summary"),
            "stages": stages_out,
        })

    return {
        "platform": platform_key,
        "rules_source": plat.get("source"),
        "board_source": board_path.name,
        "board_summary": {
            "components": len(board.components),
            "signals": len(board.signals),
        },
        "metadata": plat.get("metadata", {}),
        "sections": sections_out,
    }


def _signal_match_stats(linked: Dict[str, Any]) -> Dict[str, int]:
    """Count match rate over signals that *could* plausibly match — skipping
    notes, procedural steps, descriptive labels, and continuation rows."""
    total = 0
    matched = 0
    skipped_non_signal = 0
    for sec in linked["sections"]:
        for stg in sec["stages"]:
            for sig in stg["signals"]:
                if sig.get("note") or sig.get("step"):
                    continue
                raw = sig.get("raw") or ""
                net = sig.get("net") or ""
                # Use whichever field is more net-like
                primary = net if net and _looks_like_net_name(net) else raw
                if not _looks_like_net_name(primary):
                    skipped_non_signal += 1
                    continue
                total += 1
                if sig.get("boardview_net"):
                    matched += 1
    return {"total": total, "matched": matched, "skipped_non_signal": skipped_non_signal}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        sys.exit(__doc__)

    rules_path = Path(sys.argv[1])
    board_path = Path(sys.argv[2])
    prefix = sys.argv[3]

    linked = link_platform(rules_path, board_path, prefix)
    stats = _signal_match_stats(linked)

    print(f"Platform:    {linked['platform']}")
    print(f"Rules from:  {linked['rules_source']}")
    print(f"Board:       {linked['board_source']} "
          f"({linked['board_summary']['components']} components, "
          f"{linked['board_summary']['signals']} nets)")
    print(f"Match rate:  {stats['matched']}/{stats['total']} "
          f"({100 * stats['matched'] // max(stats['total'], 1)}%)")
    print()

    # Show the first non-empty section's first stage as a worked example
    for sec in linked["sections"]:
        if any(stg.get("signals") for stg in sec["stages"]):
            print(f"Sample section: {sec['id']}")
            print(f"  diagnosis: {sec['diagnosis_summary']}")
            stage = next(s for s in sec["stages"] if s.get("signals"))
            print(f"  stage: {stage['label']}")
            for sig in stage["signals"]:
                if sig.get("note"):
                    print(f"    NOTE: {sig['note']}")
                    continue
                if sig.get("step"):
                    print(f"    STEP: {sig['step']}")
                    continue
                bv = sig.get("boardview_net") or "—"
                cands = sig.get("probe_candidates") or []
                cand_str = (
                    f"{len(cands)} probe pt(s) — first: {cands[0]['refdes']}.{cands[0]['pin']} "
                    f"on {cands[0]['layer']} ({cands[0]['x']:.0f}, {cands[0]['y']:.0f})"
                ) if cands else "(no board match)"
                v = sig.get("expected_voltage") or ""
                r = sig.get("resistance_to_ground") or ""
                print(f"    {sig.get('raw', ''):30s}  V={v:<18s} R={r:<22s} -> {bv}: {cand_str}")
            break

    # Show the unmatched-but-expected signals so we know what we'd need to alias
    print()
    unmatched = []
    for sec in linked["sections"]:
        for stg in sec["stages"]:
            for sig in stg["signals"]:
                if sig.get("note") or sig.get("step"):
                    continue
                if sig.get("raw") and not sig.get("boardview_net"):
                    unmatched.append(sig.get("raw"))
    if unmatched:
        print(f"Unmatched rule signals ({len(unmatched)}):")
        for r in unmatched[:30]:
            print(f"  {r}")
        if len(unmatched) > 30:
            print(f"  … and {len(unmatched) - 30} more")
