# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Unit heuristics shared by the desktop canvases and the Android export.

Boardview formats store coordinates in whatever unit the exporter chose
and carry no unit metadata, so scale is inferred from the coordinate
span of the placed components. Every parser normalises to either mils
(GENCAD/BRD/FZ/XZZ/ASC) or centi-mils (TVW), which the span separates
cleanly: no real board is 50,000 mils (1.27 m) wide.

Stdlib-only on purpose — board_export.py runs under Chaquopy on Android
where tkinter doesn't exist, so nothing UI-flavoured may leak in here.
"""

MILS_PER_MM = 39.37
CENTIMILS_PER_MM = 3937.0

# Component-extent span (in file units) above which coordinates are
# taken to be centi-mils rather than mils.
CENTIMIL_SPAN_THRESHOLD = 50_000


def units_per_mm_for_span(span: float) -> float:
    """File-units-per-mm inferred from the component coordinate span."""
    return CENTIMILS_PER_MM if span > CENTIMIL_SPAN_THRESHOLD else MILS_PER_MM


__all__ = [
    "CENTIMILS_PER_MM",
    "CENTIMIL_SPAN_THRESHOLD",
    "MILS_PER_MM",
    "units_per_mm_for_span",
]
