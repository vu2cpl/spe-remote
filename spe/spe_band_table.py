"""Recommended ATU sub-band tuning frequencies for the SPE 1.5K-FA.

Lifted verbatim from the SPE 1.5K-FA User Manual rev 3.2, Section 19
("BAND TABLE, SUB-BAND, CENTRAL FREQUENCY SUB-BAND"). The manual
prescribes that the operator visit each of these frequencies, key a
30-40 W carrier, and press TUNE so the amp can populate its ATU
memory for that sub-band. spe-remote automates the sequence: for a
given band, the orchestrator iterates this list, drives the Flex to
each freq, and runs a tune_single cycle.

All values are SUB-BAND CENTRAL FREQUENCIES in kHz. Convert to MHz at
the call site (Flex's slice-t command takes MHz).

Sub-band indices match the manual's bracketed numbering (e.g. 160m
[0..23], 80m [24..52], ...) so a manual cross-reference is direct.

Bands match the SPE's own naming convention as it appears in the
CSV state's ``band`` field (e.g. "160m", "20m", "6m"). Keys are
case-insensitive at the API boundary (see ``lookup``).
"""

from __future__ import annotations

from typing import List, Optional


BAND_TABLE: dict[str, List[int]] = {
    # 160m — indices [0..23]
    "160m": [
        1785, 1795, 1805, 1815, 1825, 1835,
        1845, 1855, 1865, 1875, 1885, 1895,
        1905, 1915, 1925, 1935, 1945, 1955,
        1965, 1975, 1985, 1995, 2005, 2015,
    ],
    # 80m — indices [24..52]
    "80m": [
        3470, 3490, 3510, 3530, 3550, 3570,
        3590, 3610, 3630, 3650, 3670, 3690,
        3710, 3730, 3750, 3770, 3790, 3810,
        3830, 3850, 3870, 3890, 3910, 3930,
        3950, 3970, 3990, 4010, 4030,
    ],
    # 60m — manual lists indices [53..72] across 5013-5488 kHz, but
    # those predate the WRC-15 amateur 60m allocation (5351.5-5366.5
    # kHz secondary) and are all out-of-band for ham use. Override to
    # the single freq at the centre of the amateur allocation; the
    # raw manual list stays available for non-amateur use via
    # _RAW_60M_MANUAL below.
    "60m": [5357],
    # 40m — indices [73..88]
    "40m": [
        6963, 6988, 7013, 7038, 7063, 7088,
        7113, 7138, 7163, 7188, 7213, 7238,
        7263, 7288, 7313, 7338,
    ],
    # 30m — indices [89..91]
    "30m": [10075, 10125, 10175],
    # 20m — indices [92..100]
    "20m": [
        13975, 14025, 14075, 14125, 14175, 14225,
        14275, 14325, 14375,
    ],
    # 17m — indices [101..103]
    "17m": [18075, 18125, 18165],
    # 15m — indices [104..114]
    "15m": [
        20975, 21025, 21075, 21125, 21175, 21225,
        21275, 21325, 21375, 21425, 21475,
    ],
    # 12m — indices [115..117]
    "12m": [24891, 24963, 25038],
    # 10m — indices [118..135]
    "10m": [
        28050, 28150, 28250, 28350, 28450, 28550,
        28650, 28750, 28850, 28950, 29050, 29150,
        29250, 29350, 29450, 29550, 29650, 29750,
    ],
    # 6m — indices [136..153]
    "6m": [
        49875, 50125, 50375, 50625, 50875, 51125,
        51375, 51625, 51875, 52125, 52375, 52625,
        52875, 53125, 53375, 53625, 53875, 54125,
    ],
}


# Raw 60m sub-band list as the SPE manual prints it — kept for any
# future non-amateur (MARS, broadcast, experimental 5 MHz) use. Not
# referenced by default; ``lookup("60m")`` returns the override
# above, which is the only amateur-legal 60m freq.
_RAW_60M_MANUAL: List[int] = [
    5013, 5038, 5063, 5088, 5113, 5138,
    5163, 5188, 5213, 5238, 5263, 5288,
    5313, 5338, 5363, 5388, 5413, 5438,
    5463, 5488,
]


# Amateur radio band edges (kHz). The SPE manual's BAND_TABLE includes
# many sub-band centers OUTSIDE these — the amp's ATU doesn't care
# about regulations and can match into MARS / broadcast / out-of-band
# antennas — but a TX-capable rig like the Flex 6000 refuses to key
# outside ham bands by default. For the amateur band-sweep use case,
# trim each band's list to in-band-only.
#
# Edges chosen to be the LOOSEST reasonable amateur allocation that
# still excludes all the manual's clearly out-of-band entries — covers
# US Extra, IARU R1, and VU privileges. Operators on tighter regional
# allocations should know they may need to ignore some sub-band cycles
# the sweep schedules.
HAM_BAND_EDGES: dict[str, tuple[int, int]] = {
    "160m": (1800, 2000),
    "80m":  (3500, 4000),
    "60m":  (5351, 5367),  # WRC-15 60m amateur secondary allocation
    "40m":  (7000, 7300),
    "30m":  (10100, 10150),
    "20m":  (14000, 14350),
    "17m":  (18068, 18168),
    "15m":  (21000, 21450),
    "12m":  (24890, 24990),
    "10m":  (28000, 29700),
    "6m":   (50000, 54000),
}


def band_for_freq(freq_mhz: float) -> Optional[str]:
    """Map a frequency (MHz) to the amateur band containing it, per
    ``HAM_BAND_EDGES``. Returns None when the freq sits outside every
    ham band (general-coverage RX, broadcast, torn value, ...) — the
    caller decides whether that's a warning or a hard stop."""
    freq_khz = freq_mhz * 1000.0
    for band, (low_khz, high_khz) in HAM_BAND_EDGES.items():
        if low_khz <= freq_khz <= high_khz:
            return band
    return None


def lookup(band: str, in_band_only: bool = True) -> List[int]:
    """Return the sub-band centers (kHz) for a band name. Case-
    insensitive. Raises KeyError if the band isn't in the table —
    callers should surface that to the user as ``FAIL`` rather than
    swallowing it.

    By default filters to amateur in-band freqs only (per
    ``HAM_BAND_EDGES``) — the manual's table includes MARS / broadcast
    / out-of-band entries that a normal ham-band-locked rig refuses to
    TX on. Set ``in_band_only=False`` to get the raw manual list (for
    MARS-style operation or any future need)."""
    key = band.strip().lower()
    if key not in BAND_TABLE:
        # Tolerate "20" instead of "20m" — the SPE itself uses "20m"
        # but callers might forget the m.
        if not key.endswith("m") and (key + "m") in BAND_TABLE:
            key = key + "m"
        else:
            raise KeyError(
                f"Unknown band {band!r}; known: {sorted(BAND_TABLE)}"
            )

    centers = BAND_TABLE[key]
    if in_band_only and key in HAM_BAND_EDGES:
        low_khz, high_khz = HAM_BAND_EDGES[key]
        centers = [f for f in centers if low_khz <= f <= high_khz]
    return centers


def all_bands() -> List[str]:
    """Sorted list of band names. Useful for client UI band pickers."""
    # Sort by the lowest freq in each band so the picker reads
    # 160m → 80m → 60m → ... → 6m (logical wavelength order rather
    # than alphabetic).
    return sorted(BAND_TABLE, key=lambda b: BAND_TABLE[b][0])
