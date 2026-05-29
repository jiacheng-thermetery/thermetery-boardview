# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Fuzzy signal-name matcher for power-sequencing rules → schematic /
boardview signals.

The rules YAML refers to signals by canonical
short names; real schematics and boardview net tables decorate them with
vendor-specific prefixes/suffixes ("+3VSB_EC", "BCLK_N", "CPU_VTT_AON").
Direct exact-match resolves only ~10% of rule signals. This module
implements two normalization tiers that bring the rate up substantially:

  Tier 1 (canonical normalization). Strip:
    - leading +/- and P-on-power-rail (P12V → 12V)
    - trailing # / + / -
    - trailing underscored polarity / pair suffixes (_N, _L, _P, _POS, ...)
    - trailing bare P or N when preceded by alphanumeric (BCLKN → BCLK)
    - all internal underscores (PWR_OK → PWROK)

  Tier 2 (substring match). After both sides are normalized:
    - rule contained in schematic signal  (CPU_VTT → CPU_VTT_AON)
    - schematic signal contained in rule  (+3VSB_EC → 3VSB)
    Confidence weighted by length ratio so partial matches don't beat
    exact matches.

Out of scope (yet):
  - Tier 3: voltage-rail equivalence classes (3.3V == 3V3 == P3V3)
  - Tier 4: Levenshtein edit distance for typos
  - Tier 5: category dictionaries for descriptive rules (power-good,
            reset, sleep-state)

Public API:
  normalize(token) -> str
  tokenize_rule_entry(rule_string) -> list[str]
  build_match_index(signals) -> Dict[str, List[str]]
  find_signal_candidates(rule_token, signals_or_index) -> List[MatchCandidate]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Union


# ---- Normalization ------------------------------------------------------

# Vendor net-name prefixes added by some EDA exports (Gigabyte Z490 is
# the confirmed case, observed on 228+ nets — 52 with `N_-`, 176 with
# bare `N_`, plus `N_+` variants). They decorate the "real" signal name
# with a routing/polarity marker:
#   N_-XXX  active-low / negative net      (e.g. N_-RTCRST, N_-CPURST)
#   N_+XXX  active-high / differential pos (e.g. N_+USBP1)
#   N_XXX   generic "net" prefix
# Stripping these makes RTCRST (rule) match N_-RTCRST (board net).
# Listed longest-first so the `N_-` and `N_+` variants match before
# the bare `N_` strip would over-eagerly trim them.
_VENDOR_PREFIXES = ("N_-", "N_+", "N_")

# Vowels (uppercase). Used by the bare trailing P/N strip below to
# distinguish polarity markers (preceded by a consonant — CLKP, BCLKP)
# from English word endings (CAP, STOP, CHIP).
_VOWELS = frozenset("AEIOU")

# Underscored polarity / pair / direction suffixes. Order matters: we
# match the LONGER ones first so "_NEG" beats "_N", "_POS" beats "_P".
# `_N`/`_L`/`_P` go last so they don't grab tail-end matches that
# belong to a longer canonical form.
_SUFFIXES_BY_LEN: tuple = (
    "_NEG", "_POS", "_NOT", "_INV",
    "_LO", "_HI",
    "_N", "_L", "_P",
)

# Voltage prefix patterns — the three ways a rail's voltage is written
# at the start of a signal token. We canonicalize all three to a single
# form so 3.3V / 3V3 / +3V3 / P3V3 all match, and 3.3VSB / 3V3SB / 3VSB
# / +3VSB also match. The leading sign / P-prefix is stripped earlier
# in normalize(); these patterns assume that's already happened.
_VOLT_FRAC_RE = re.compile(r"^(\d+)\.(\d+)V(.*)$")   # e.g. 3.3V, 1.8VSB
_VOLT_XVY_RE  = re.compile(r"^(\d+)V(\d+)(.*)$")     # e.g. 3V3, 1V8, 0V85
_VOLT_INT_RE  = re.compile(r"^(\d+)V([A-Z].*)?$")    # e.g. 5V, 12V, 3VSB

# Schematic exports often write decimals using underscore as the
# separator (Gigabyte Z490 has `1_05V_OV1`, `3_3V_IN2`) because the
# PCB EDA tool can't put a period in a net name. We rewrite these to
# the XVY form (1V05, 3V3) BEFORE the generic underscore strip so the
# voltage canon sees them as voltages, not opaque integers.
_DECIMAL_UNDERSCORE_RE = re.compile(r"(\d)_(\d+)V")


def _canonicalize_voltage(t: str) -> str:
    """If `t` begins with a voltage-shaped prefix, canonicalize it so
    that format variants of the same rail match.

    Convention:
      - Suffix-free voltages keep the fractional in XVY form:
            3.3V → 3V3,   1.8V → 1V8,   0.85V → 0V85,   5V → 5V
      - Suffix-bearing voltages DROP the fractional (vendor convention
        — every schematic writes "3VSB", never "3.3VSB"):
            3.3VSB → 3VSB,   3V3SB → 3VSB,   1.8VSB → 1VSB,
            3.3VAUX → 3VAUX

    Non-voltage tokens pass through unchanged. The patterns require
    the V to be at a precise position so e.g. `VCCSA` (V at start)
    doesn't match `_VOLT_FRAC_RE`."""
    m = _VOLT_FRAC_RE.match(t)
    if m:
        i, f, suf = m.groups()
        return f"{i}V{suf}" if suf else f"{i}V{f}"
    m = _VOLT_XVY_RE.match(t)
    if m:
        i, f, suf = m.groups()
        return f"{i}V{suf}" if suf else f"{i}V{f}"
    m = _VOLT_INT_RE.match(t)
    if m:
        i, suf = m.groups()
        return f"{i}V{suf or ''}"
    return t


def normalize(token: str) -> str:
    """Strip standard signal-name decoration to a canonical form.

    Goal: every common decoration variant of the same logical signal
    yields the same string. So `BCLK#`, `BCLK_N`, `BCLKN`, `BCLK-`,
    `+BCLK` all return `BCLK`; `PWR_OK` and `PWROK` both return `PWROK`;
    `3.3V`/`3V3`/`+3V3`/`P3V3` all return `3V3`; `3.3VSB`/`3V3SB`/`3VSB`
    all return `3VSB`.

    Returns "" if the input is empty or all decoration (e.g. the token
    `+` alone, or `___`). Caller should treat empty as "unmatchable"."""
    t = token.upper().strip()
    if not t:
        return ""

    # Vendor net-name prefix (Gigabyte's N_- / N_+ / N_). Done first so
    # the cleaned token feeds the remaining steps as if it were a plain
    # rule signal. The list is longest-first so `N_-RTCRST` strips the
    # full `N_-` prefix, not just `N_` (which would leave a stray `-`).
    for prefix in _VENDOR_PREFIXES:
        if t.startswith(prefix) and len(t) > len(prefix):
            t = t[len(prefix):]
            break

    # Leading sign or P-on-power-rail. The P-prefix is restricted to
    # cases where the next char is a digit ("P12V", "P3V3") so we
    # don't strip the P off PCH, PWR, PROC etc.
    if t and t[0] in "+-":
        t = t[1:]
    elif t and t[0] == "P" and len(t) >= 2 and t[1].isdigit():
        t = t[1:]

    # Trailing punctuation — # is the canonical active-low marker;
    # trailing +/- show up on differential-pair members in some
    # vendor styles.
    while t and t[-1] in "#+-":
        t = t[:-1]

    # Trailing underscored suffix. Single pass — multiple suffix
    # stripping (`SIG_N_AUX`) is rare and we'd rather under-strip
    # than over-strip.
    for suf in _SUFFIXES_BY_LEN:
        if t.endswith(suf) and len(t) > len(suf):
            t = t[:-len(suf)]
            break

    # Bare trailing P/N (differential-pair / polarity marker without
    # an underscore separator). Constraints:
    #   - base ≥ 4 chars so short tokens like RPN aren't mangled
    #   - preceded by alphanumeric (not an underscore we already
    #     stripped via _P / _N suffix above)
    #   - the preceding character is a consonant or digit. Differential
    #     pair members tend to look like CLKP/BCLKP/CLK24P/USBN/D0P
    #     — consonant or digit before the polarity letter. Vowel-before
    #     names (CAP, STOP, CHIP, LOOP) are usually English word
    #     fragments, not differential markers, so we leave them alone.
    #     False positives still possible on RAMP / JUMP / PUMP-style
    #     signal IDs but those are rare in PCB net naming.
    if (len(t) >= 4 and t[-1] in "PN" and t[-2].isalnum()
            and t[-2] != "_" and t[-2] not in _VOWELS):
        t = t[:-1]

    # Underscore-as-decimal-separator: rewrite `\d+_\d+V` to `\d+V\d+`
    # before the generic strip below. Catches schematic-export quirks
    # like `1_05V_OV1` → `1V05_OV1`, so the voltage canon downstream
    # recognises them as voltages instead of opaque integers.
    t = _DECIMAL_UNDERSCORE_RE.sub(r"\1V\2", t)

    # All internal underscores. Done before voltage canon so the canon
    # regex sees a flat form ("3V3_SB" → "3V3SB" → "3VSB" via canon).
    t = t.replace("_", "")

    # Voltage prefix canonicalization. Catches the 3.3V vs 3V3 wars
    # plus the SB/AUX/DUAL suffix conventions.
    t = _canonicalize_voltage(t)

    # Any remaining periods are non-voltage decimals — rule entries
    # like "CK_38.4M_NSCCCLKN" where the rule writer kept the
    # frequency-style "38.4M" notation that the schematic dropped to
    # "384M" or "38_4M". Stripping periods after the underscore pass
    # makes both forms collapse.
    if "." in t:
        t = t.replace(".", "")

    return t


# ---- Rule-side tokenization --------------------------------------------

# Strip CJK characters from rule entries. Source rules often annotate
# signals with Chinese descriptions ("AZ_RST# 音频芯片" — meaning
# "audio chip"). The Chinese text is noise from a matching perspective.
_CJK_RE = re.compile(r"[一-鿿]+")

# Strip angle-bracket placeholder forms like "<VCORE>" or "<VDDNB>".
# These appear in some rule entries to indicate variable substitution
# and the brackets aren't part of the signal name.
_ANGLE_RE = re.compile(r"[<>]")

# Split on the punctuation/separators a rule entry might use to pack
# multiple aliases into one string ("APU_PWROK/PWROK", "3VSB/5VSB",
# "BCLK/BCLK#"). Includes whitespace and brackets.
_SPLIT_RE = re.compile(r"[\s/()\[\]:,\\\\]+")

# Rejects tokens that look like step IDs ("10", "11"), frequencies
# ("38M", "100M", "32.768KHZ"), or pure numerics in general. A real
# signal name always contains a letter, and a real signal name with
# digits has them MIXED IN (e.g. "VCC3", "3VSB"), not as a frequency
# suffix ("100M", "1M") or unit ("32.768KHZ"). Heuristic but robust.
_FREQ_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:K|M|G|KHZ|MHZ|GHZ|HZ|V|MV|A|MA)?$",
    re.IGNORECASE,
)


def tokenize_rule_entry(entry: str) -> List[str]:
    """Break a raw rule-YAML signal entry into individual candidate
    tokens. Strips CJK annotations, splits on the common alias
    separators, drops fragments shorter than 2 chars, and rejects
    obvious non-signal tokens (step IDs, frequencies, pure numbers,
    angle-bracket placeholders)."""
    if not entry:
        return []
    stripped = _ANGLE_RE.sub("", _CJK_RE.sub("", entry))
    out: List[str] = []
    for part in _SPLIT_RE.split(stripped):
        part = part.strip()
        if len(part) < 2:
            continue
        if _FREQ_RE.match(part):
            # Step numbers, frequencies, voltage values without a "rail
            # name" wrapper. They might be referenced elsewhere in the
            # rule (e.g. "P12V at 12V"), so dropping them here is safe.
            continue
        if not any(c.isalpha() for c in part):
            # Pure-numeric fragments after the freq filter shouldn't
            # exist, but belt-and-braces in case the regex misses one.
            continue
        out.append(part)
    return out


# ---- Match candidate ----------------------------------------------------

@dataclass(frozen=True)
class MatchCandidate:
    """One ranked match between a rule signal and a schematic / board
    signal. Renderers consume these to show "rule X → schematic Y
    (confidence Z, by `kind`)" hints to the user."""
    match: str       # the original (un-normalized) schematic signal
    confidence: float
    kind: str        # "exact" | "normalized" | "substring" | "substring_rev"


# Tunable confidence floors per kind. Tweaked so that:
#   - exact always wins
#   - normalized beats any substring
#   - substring with longer overlap beats one with shorter
_CONF_EXACT = 1.00
_CONF_NORM = 0.90
_CONF_SUB_MAX = 0.85   # rule contained in schematic; weighted by len ratio
_CONF_SUB_REV_MAX = 0.75  # schematic contained in rule; lower weight


# ---- Index builder ------------------------------------------------------

def build_match_index(signals: Iterable[str]) -> Dict[str, List[str]]:
    """Build a `{normalized -> [original, ...]}` map. O(1) lookups
    for exact and normalized matches; substring search still scans
    the normalized keys (typically 5-10× smaller than the originals
    because suffix decoration collapses)."""
    out: Dict[str, List[str]] = {}
    for s in signals:
        n = normalize(s)
        if not n:
            continue
        out.setdefault(n, []).append(s)
    return out


# ---- Match query --------------------------------------------------------

def find_signal_candidates(
    rule_token: str,
    signals_or_index: Union[Iterable[str], Mapping[str, List[str]]],
    *,
    max_candidates: int = 5,
    min_confidence: float = 0.30,
) -> List[MatchCandidate]:
    """Return ranked candidate matches for `rule_token` against the
    pool of schematic / board signals.

    `signals_or_index` accepts either:
      - a flat iterable of original signal strings (we'll build a
        one-shot index), or
      - a pre-built index from `build_match_index()` for reuse across
        many queries (recommended when matching many rule tokens).

    Returns at most `max_candidates`, sorted by descending confidence.
    Filters out matches below `min_confidence` so callers can default
    to "show top candidate only if reasonably confident"."""
    # Coerce to an index if we got a flat list.
    if isinstance(signals_or_index, Mapping):
        index = signals_or_index
    else:
        index = build_match_index(signals_or_index)

    rule_norm = normalize(rule_token)
    if not rule_norm:
        return []

    out: List[MatchCandidate] = []

    # Tier 0: exact match against an original signal. The index buckets
    # by normalized form; an original-string equality requires checking
    # whether the literal rule_token sits in any bucket. Doing it as a
    # quick membership check on every original is O(N); we approximate
    # via "is rule_token == any original in the rule_norm bucket?" so
    # we only inspect a handful of strings.
    exact_bucket = index.get(rule_norm, [])
    for orig in exact_bucket:
        if orig == rule_token:
            out.append(MatchCandidate(orig, _CONF_EXACT, "exact"))
            break

    # Tier 1: normalized match. Everything else in the rule_norm bucket
    # is a normalized-form sibling of the rule. Skip duplicates already
    # taken by the exact match above.
    for orig in exact_bucket:
        if orig != rule_token:
            out.append(MatchCandidate(orig, _CONF_NORM, "normalized"))

    # Tier 2: substring match. Two directions:
    #   forward — rule_norm is a substring of some schematic_norm
    #             (e.g. CPU_VTT → CPU_VTT_AON's `CPUVTTAON`)
    #   reverse — schematic_norm is a substring of rule_norm
    #             (e.g. rule `+3VSB_EC` → `3VSBEC`; schematic `3VSB`)
    # We confine the scan to normalized keys (smaller pool) and return
    # only the FIRST original in each matched bucket — others get the
    # same confidence and would just duplicate noise in the candidate
    # list. Caller can re-query by bucket if it wants every member.
    rn_len = len(rule_norm)
    for sn, originals in index.items():
        if sn == rule_norm:
            continue  # already handled above
        sn_len = len(sn)
        if rule_norm in sn:
            # forward — favour matches that consume more of the schematic
            conf = _CONF_SUB_MAX * rn_len / sn_len
            if conf >= min_confidence:
                out.append(MatchCandidate(originals[0], conf, "substring"))
        elif sn in rule_norm:
            # reverse — only meaningful if the schematic side is long
            # enough to carry signal. 4-char minimum drops the noise
            # class (`CLK`, `RST`, `OK`, `EN`, `BAT`, `ATX`) while
            # keeping the real wins (`3VSB`, `VBAT`, `12V`, `PWROK`).
            # Was 3 in the first cut; empirically too generous.
            if sn_len < 4:
                continue
            conf = _CONF_SUB_REV_MAX * sn_len / rn_len
            if conf >= min_confidence:
                out.append(MatchCandidate(
                    originals[0], conf, "substring_rev"))

    out.sort(key=lambda c: (-c.confidence, c.match))
    return out[:max_candidates]
