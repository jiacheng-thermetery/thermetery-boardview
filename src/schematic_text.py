# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Schematic PDF → structured text index.

Extracts per-page sub-circuit titles, signal-like tokens, and the
cross-reference graph (signal -> pages where it appears) from a
boardview's accompanying PDF schematic. Designed as a feeder for the
walker's signal-resolution step: when the rule says "check VCCSA" and
the board net is named "VCCSA_CPU_VR", an alias extracted from the
schematic can bridge the two.

Vendor coverage today (verified on real boards):
  * Gigabyte (B550 AORUS PRO, X570 GAMING X, Z490 VISION G)
  * MSI      (MS-7680, MS-7A12, B550M BAZOOKA)

Image-only schematics (e.g. some PS4 service manuals) return an empty
index — caller can detect that via `SchematicIndex.has_text` and either
skip or fall back to OCR.

Public API:
  extract_index(pdf_path) -> SchematicIndex
  SchematicIndex.power_sequence_pages -> list[int]  (1-indexed)
  SchematicIndex.pages_for_signal(name) -> list[int]
  SchematicIndex.title_for_page(num)   -> Optional[str]

This module is intentionally walker-agnostic — no Tk, no boardview
imports. The integration glue lives elsewhere (e.g. in the linker).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import fitz  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:
    fitz = None  # type: ignore
    _PYMUPDF_AVAILABLE = False


# ----- Token classification ----------------------------------------------

# A "signal-like" token: optional leading sign for power rails (+12V,
# +5V, -12V), then ALL-CAPS / digit identifier, optional trailing #
# (active-low) or + / - (differential pair). Underscores allowed
# mid-token. We allow a digit start (3VSB, 5VDUAL — common power
# rails). The body length cap keeps us out of full-line garbage while
# accommodating the longest real signals (~25-30 chars). A separate
# "must contain a letter" check below filters out pure-numeric tokens
# that the regex would otherwise pass.
_SIGNAL_RE = re.compile(r"^[+\-]?[A-Z0-9_]{2,32}[#+\-]?$")

# Page-number cross-reference list, e.g. "7,19,38,43,46" on the line
# right after a signal name. Bracketed form "[37]" appears in some
# Gigabyte sheets — handled separately in _parse_xrefs.
_XREF_LIST_RE = re.compile(r"^[\d,\s]+$")
_XREF_BRACKET_RE = re.compile(r"\[(\d+)\]")

# Tokens we never want to classify as signals. The title block is
# extremely repetitive across vendors — caching the boilerplate set
# as a module-level frozenset is faster than re-checking per word.
_TITLE_BLOCK_BOILERPLATE: frozenset = frozenset({
    # Common across MSI and Gigabyte
    "SIZE", "REV", "SHEET", "OF", "DATE", "CUSTOM", "TITLE",
    # Vendor names
    "MSI", "GIGABYTE", "ASUS", "ASROCK",
    "MICRO-STAR", "INT'L", "CO.,LTD",
    # Calendar — caught by name not regex; only ALL-CAPS forms here
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
    "SATURDAY", "SUNDAY",
    # Known noise
    "NC", "NA", "TBD", "DNI",
})


def _looks_like_signal(token: str) -> bool:
    """True iff `token` matches the signal-name shape AND isn't a
    title-block boilerplate word.

    Length cap excludes the obvious "WAKE#15,16,26" failures (those
    don't match the regex anyway because of the comma) and keeps the
    classifier fast — 99% of tokens decide on the regex alone. The
    final any-letter check rejects pure-numeric tokens like "12345"
    or "+99" that the relaxed regex would otherwise pass."""
    if len(token) < 2 or len(token) > 33:
        return False
    if token in _TITLE_BLOCK_BOILERPLATE:
        return False
    if not _SIGNAL_RE.match(token):
        return False
    return any(c.isalpha() for c in token)


# ----- Title-block parsing -----------------------------------------------

# The page title (sub-circuit name) lives in the bottom-right title
# block. Two vendor patterns we've verified:
#
#   MSI:        "... Custom LAN - RTL8111E Thursday, October 21, 2010"
#   Gigabyte:   "... 1.0 POWER SEQUENCE Custom 29 57 Monday, April 20, 2020"
#
# Both bracket the title between fixed labels: MSI puts it AFTER "Custom"
# and BEFORE the day-name; Gigabyte puts it AFTER the rev (e.g. "1.0")
# and BEFORE "Custom". Both end with a date.
_DAY_NAME_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"
)

# Vendor company-name lines that sometimes sit BETWEEN the real title
# and "Custom" in the title block (Gigabyte Z490 does this; Gigabyte
# B550 does not). When we see one of these in the candidate slot we
# step back another line to find the actual sub-circuit name. Comparison
# is case-insensitive and substring-based to tolerate slight variants
# ("Gigabyte Technology Inc.", "MICRO-STAR INT'L CO.,LTD", etc.).
_COMPANY_BOILERPLATE = (
    "gigabyte technology",
    "micro-star",
    "asustek",
    "asrock",
    "asus",
)


def _is_company_line(line: str) -> bool:
    low = line.lower()
    return any(c in low for c in _COMPANY_BOILERPLATE)


def _extract_page_title(page_text: str) -> Optional[str]:
    """Heuristically pull the sub-circuit name out of a page's text.

    The title block is repeated 2-3× per page (MSI does this on every
    sheet) so we only need to find one good occurrence. Anchor on
    "Custom" + the date line that follows it; the *gap* between the
    two indices tells us which vendor layout we're looking at:

      MSI (gap = 2):
          [i  ] 'Custom'
          [i+1] '<TITLE>'
          [i+2] '<day-name>, ...'

      Gigabyte (gap = 3):
          [i-1] '<TITLE>'
          [i  ] 'Custom'
          [i+1] '<sheet num>'
          [i+2] '<total sheets>'
          [i+3] '<day-name>, ...'

    Anything else is ambiguous (cover sheets, custom title-block
    layouts) — return None so the caller can fall back to "(no title
    parsed)" instead of a misclassified sheet number."""
    lines = [ln.strip() for ln in page_text.splitlines()]
    lines = [ln for ln in lines if ln]

    n = len(lines)
    for i, ln in enumerate(lines):
        if ln != "Custom":
            continue
        # Find the first day-name within a small window after Custom.
        date_offset = None
        for off in (2, 3):
            j = i + off
            if j < n and _DAY_NAME_RE.search(lines[j]):
                date_offset = off
                break
        if date_offset == 2:
            # MSI layout — title is the line right after Custom.
            cand = lines[i + 1] if i + 1 < n else ""
            return cand or None
        if date_offset == 3 and i >= 1:
            # Gigabyte layout — title is the line right before Custom.
            # Z490 sneaks a "Gigabyte Technology" line in between the
            # real title and "Custom"; step back one more if we hit it.
            cand = lines[i - 1]
            if _is_company_line(cand) and i >= 2:
                cand = lines[i - 2]
            return cand or None
        # No matching layout for this Custom occurrence; keep looking
        # in case the page has multiple title blocks (MSI repeats 3×).
    return None


# ----- Cross-reference parsing -------------------------------------------

def _parse_xrefs(lines: List[str]) -> Dict[str, Set[int]]:
    """Walk a page's text lines and pair each signal-like token with
    the page numbers listed immediately after it (Gigabyte/MSI form),
    or with bracketed page numbers on the same or next line (Z490
    form). Returns a {signal_name: {page1, page2, ...}} dict.

    This isn't strict parsing — there's no schema, just a layout
    convention that most schematic exports honour. The caller
    accumulates results across all pages for the SchematicIndex's
    signal -> pages map."""
    out: Dict[str, Set[int]] = {}
    n = len(lines)
    i = 0
    while i < n:
        ln = lines[i].strip()
        if _looks_like_signal(ln):
            # Form A: signal on line i, comma-list of pages on line i+1
            if i + 1 < n and _XREF_LIST_RE.match(lines[i + 1].strip()):
                pages = {int(p.strip()) for p in lines[i + 1].split(",")
                         if p.strip().isdigit()}
                if pages:
                    out.setdefault(ln, set()).update(pages)
                    i += 2
                    continue
            # Form B: signal followed by bracketed page on the same
            # or next line, e.g. "VREF" then "[37]"
            target = lines[i + 1] if i + 1 < n else ""
            for m in _XREF_BRACKET_RE.finditer(target):
                out.setdefault(ln, set()).add(int(m.group(1)))
        i += 1
    return out


# ----- Public dataclasses ------------------------------------------------

@dataclass
class PageInfo:
    """Per-page extraction. `num` is 1-indexed to match the title-block
    sheet number; PyMuPDF is 0-indexed internally."""
    num: int
    title: Optional[str]
    signals: Set[str] = field(default_factory=set)


@dataclass
class SchematicIndex:
    """Whole-PDF index. The two reverse maps (`pages_by_signal`,
    `signals_by_page`) are precomputed during build because lookups
    happen on every linker query and dict access dominates the API."""
    pdf_path: Path
    page_count: int
    has_text: bool  # False for image-only PDFs
    pages: List[PageInfo] = field(default_factory=list)
    pages_by_signal: Dict[str, Set[int]] = field(default_factory=dict)
    signals_by_page: Dict[int, Set[str]] = field(default_factory=dict)

    def title_for_page(self, num: int) -> Optional[str]:
        for p in self.pages:
            if p.num == num:
                return p.title
        return None

    def pages_for_signal(self, name: str) -> List[int]:
        """Return all page numbers (1-indexed) where `name` appears.
        Combines tokens-found-on-page with cross-reference targets."""
        return sorted(self.pages_by_signal.get(name, set()))

    @property
    def power_sequence_pages(self) -> List[int]:
        """Pages whose title looks like a power-sequencing diagram.
        Most useful as an entry point for walker rule resolution —
        these are the pages a tech checks first when triaging power."""
        kw = ("POWER SEQUENCE", "POWER SEQ", "PWR SEQ", "POWER ON",
              "POWER UP", "PWR_OK", "POWERSEQ")
        out: List[int] = []
        for p in self.pages:
            if not p.title:
                continue
            t = p.title.upper()
            if any(k in t for k in kw):
                out.append(p.num)
        return out

    @property
    def signal_count(self) -> int:
        return len(self.pages_by_signal)


# ----- Build entry point -------------------------------------------------

def extract_index(pdf_path: Path) -> SchematicIndex:
    """Open `pdf_path` with PyMuPDF and build the SchematicIndex.

    Cheap on embedded-text PDFs (~tens of ms per page). For image-only
    PDFs returns an index with `has_text=False` and empty maps — the
    caller can route to OCR if needed."""
    if not _PYMUPDF_AVAILABLE:
        raise RuntimeError(
            "PyMuPDF (fitz) is required for schematic text extraction; "
            "install with: pip install pymupdf"
        )

    doc = fitz.open(str(pdf_path))
    n_pages = len(doc)
    pages: List[PageInfo] = []
    pages_by_signal: Dict[str, Set[int]] = {}
    signals_by_page: Dict[int, Set[str]] = {}
    total_chars = 0

    try:
        for i in range(n_pages):
            text = doc[i].get_text()
            total_chars += len(text)
            page_num = i + 1
            title = _extract_page_title(text)

            # Collect raw lines for both signal extraction and xref
            # parsing — both want the same line-broken view.
            lines = text.splitlines()
            page_signals: Set[str] = set()
            for ln in lines:
                tok = ln.strip()
                if _looks_like_signal(tok):
                    page_signals.add(tok)

            # Cross-references discovered on this page extend the
            # signal -> pages map (the signal lives on this page AND
            # on the listed targets).
            xrefs = _parse_xrefs(lines)
            for sig, target_pages in xrefs.items():
                pages_by_signal.setdefault(sig, set()).update(target_pages)
                pages_by_signal[sig].add(page_num)

            # Every signal-shaped token also gets credited to this
            # page even if it has no xref entry.
            for sig in page_signals:
                pages_by_signal.setdefault(sig, set()).add(page_num)

            signals_by_page[page_num] = page_signals
            pages.append(PageInfo(num=page_num, title=title,
                                  signals=page_signals))
    finally:
        doc.close()

    has_text = total_chars > 100 * n_pages  # ~100 chars/page minimum
    return SchematicIndex(
        pdf_path=pdf_path,
        page_count=n_pages,
        has_text=has_text,
        pages=pages,
        pages_by_signal=pages_by_signal,
        signals_by_page=signals_by_page,
    )
