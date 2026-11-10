#!/usr/bin/env python3
"""
build_zones.py — rebuild chicago_parking_zones.geojson from the two
City of Chicago source datasets.

This is the runnable implementation of the prose runbook
(chicago_parking_data_pipeline.md). The runbook remains the spec; this
script follows its step numbering in comments so the two stay comparable.

Inputs
  --zones-csv   Parking Permit Zones export (CSV) from the Data Portal
                dataset u9xt-hiju
  --scl         Street Center Lines export (GeoJSON) from dataset 6imu-meau

Output
  --out         GeoJSON FeatureCollection, one Polygon per restricted
                curb stretch, properties: zone, row_id, signage_status,
                centerline

Validation
  Always runs the runbook's Step 10 checks. Exits non-zero if a
  ground-truth point disagrees or a health metric moves outside
  tolerance, so a scheduled rebuild fails closed and publishes nothing.

Optional
  --baseline    an existing build to diff against, to separate "the city
                changed the data" from "this script differs from whatever
                built the baseline"

Standard library only, deliberately: runs on a stock macOS python3 and on
a GitHub Actions runner with no install step.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Flat-earth scaling. Chicago's latitude range is narrow enough that a single
# reference latitude introduces negligible error citywide (runbook Step 7).
# Values from the standard WGS84 degree-length series evaluated at 41.86 N.
REFERENCE_LATITUDE = 41.86


def _ft_per_degree(lat_deg: float) -> tuple[float, float]:
    """Feet per degree of latitude and of longitude at a given latitude."""
    phi = math.radians(lat_deg)
    m_per_deg_lat = (
        111132.92
        - 559.82 * math.cos(2 * phi)
        + 1.175 * math.cos(4 * phi)
        - 0.0023 * math.cos(6 * phi)
    )
    m_per_deg_lon = (
        111412.84 * math.cos(phi)
        - 93.5 * math.cos(3 * phi)
        + 0.118 * math.cos(5 * phi)
    )
    ft_per_m = 1.0 / 0.3048
    return m_per_deg_lat * ft_per_m, m_per_deg_lon * ft_per_m


FT_PER_DEG_LAT, FT_PER_DEG_LON = _ft_per_degree(REFERENCE_LATITUDE)

# Chicago's address origin: the State St / Madison St intersection. Addresses
# increase with distance from here along the relevant axis (runbook Step 7).
ORIGIN_LAT = 41.881826
ORIGIN_LON = -87.628151

BUFFER_WIDTH_FEET = 50.0
COORD_PRECISION = 7

# A clipped centerline shorter than this has no usable address gradient, and
# gets a one-lot stub instead of being dropped (see one_lot_feet).
MIN_CLIPPED_LENGTH_FEET = 1.0

# Zero-width address overlaps are real restrictions, not noise, and must not
# be dropped or emitted as zero-area polygons. Three cases hit this:
#   * a row whose own range is a single address (lo == hi)
#   * a row whose range touches a segment's range at exactly one address
#   * an SCL segment whose own address range is a single address
# Each gets a ribbon one lot long. Chicago addresses step by 2 per lot on a
# side, so a lot is 2 address units; the feet-per-unit figure is derived from
# the matched segment rather than assumed, and clamped against pathological
# segments (e.g. a long segment nominally spanning 2 addresses).
ADDRESSES_PER_LOT = 2
DEFAULT_LOT_FEET = 25.0
MIN_LOT_FEET = 10.0
MAX_LOT_FEET = 50.0

# Step 3 — known street-type corrections. SCL holds the current/correct type;
# the parking CSV carries a stale one. Keyed by (direction, name) and applied
# regardless of the row's own stated type, so every row on that street is
# corrected, not only the ones that originally mismatched.
TYPE_CORRECTIONS = {
    ("N", "ROCKWELL"): "ST",
    ("N", "KILDARE"): "AVE",
    ("S", "CALUMET"): "AVE",
    ("W", "CONCORD"): "PL",
}

# Step 4 — names the parking CSV keeps whole but SCL splits into
# street_nam + suf_dir. Mapped on the parking side so both sides compare equal.
PARKING_NAME_REMAP = {
    "LINCOLN PARK WEST": "LINCOLN PARK W",
}

REQUIRED_CSV_COLUMNS = (
    "ROW ID",
    "STATUS",
    "ZONE",
    "ODD_EVEN",
    "ADDRESS RANGE - LOW",
    "ADDRESS RANGE - HIGH",
    "STREET DIRECTION",
    "STREET NAME",
    "STREET TYPE",
    "BUFFER",
)

# Which signage_status values get geometry built.
#
# DECISION, 2026-11-06 (final form — an earlier draft of this same decision
# got it wrong, and the wrong version is instructive).
#
# A buffer stretch carries NO PARKING RESTRICTION. That is what a buffer is:
# parking is unrestricted there, and residents of those addresses are merely
# eligible to buy permits for the zone named. So the answer to "can I park
# here" on a buffer stretch is yes.
#
# The mistake was to conclude from that that buffer geometry should not
# exist. It should. The no-restriction finding is about what the app REPORTS
# WHILE DRIVING, not about what the data contains. Buffer geometry is still
# built, still rendered, and still returned by taps and address lookups —
# behind a toggle that defaults OFF — because a resident asking "which zone
# could I apply for at this address" is a real question the data answers.
# What changed is only that the driving report ignores buffer, and that
# buffer results are worded "Zone X buffer: no parking restrictions" rather
# than as a restriction.
#
# Hence BOTH statuses get geometry, as before. Buffer-unsigned still does
# not. The app, not the pipeline, decides when to suppress buffer.
GEOMETRY_STATUSES = ("Standard", "Buffer-signed")

# Step 10 — ground truth. These are field-verified and are pass/fail.
# Haussen Ct was ground-truthed twice (aldermanic office, then in person).
# A build that puts a zone there is wrong; do not "fix" the expectation.
GROUND_TRUTH = [
    {
        # The runbook's original latitude for this point (41.91109350058938)
        # sat 49.93 ft from the centerline against a ribbon exactly 50.00 ft
        # wide — under an inch inside the edge, so whether it read as inside
        # depended on the ft-per-degree constant rather than on the data.
        # That is the boundary-instability trap recorded in the app handoff
        # doc: test points must be interior, never on an edge. Replaced with a
        # point 25 ft south of the same centerline, at the same longitude, on
        # the same block and the same side, resolving the same ROW 10472.
        # The field finding is unchanged: the odd side of the 2100 block of
        # W Concord Pl is Zone 102, and this build agrees.
        "label": "2151 W Concord Pl (odd, Standard) — 25 ft interior",
        "lon": -87.68089114362121,
        "lat": 41.911161915080179,
        "expect_zone": "102",
    },
    {
        # ROW 10184, a Buffer row classified Buffer-signed because signs were
        # seen here IN PERSON. It expects Zone 102 and gets it.
        #
        # For a few hours on 2026-11-06 this expectation was flipped to None,
        # when the no-restriction finding was misread as "buffer geometry
        # should not exist". That put the build in direct contradiction with a
        # field observation, which is a thing a ground-truth suite exists to
        # make loud. The scope correction (see GEOMETRY_STATUSES above)
        # removed the contradiction: the polygon exists and resolves, and
        # suppression happens in the app's driving report instead.
        "label": "2134 W Concord Pl (even, Buffer-signed)",
        "lon": -87.68110260980616,
        "lat": 41.911295434929755,
        "expect_zone": "102",
    },
    {
        "label": "3121 N Haussen Ct",
        "lon": -87.7220154225609,
        "lat": 41.93767516304817,
        "expect_zone": None,
    },
    {
        "label": "Offshore, Lake Michigan",
        "lon": -87.55,
        "lat": 41.90,
        "expect_zone": None,
    },
]

# Health-metric tolerances. Not pass/fail on their own merits — they are
# regression tripwires. A fresh data pull that moves these a long way is
# worth a human look before the file reaches the app.
MIN_MATCH_RATE = 0.97
EXPECTED_OPPOSITE_SIDE_FRACTION = 0.91
OPPOSITE_SIDE_TOLERANCE = 0.05
MAX_FEATURE_COUNT_DRIFT = 0.10


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def to_feet(lon: float, lat: float) -> tuple[float, float]:
    return lon * FT_PER_DEG_LON, lat * FT_PER_DEG_LAT


def segment_lengths_feet(line: list[list[float]]) -> tuple[list[float], float]:
    """Cumulative arc length in feet at each vertex, plus the total."""
    cum = [0.0]
    for i in range(1, len(line)):
        x0, y0 = to_feet(line[i - 1][0], line[i - 1][1])
        x1, y1 = to_feet(line[i][0], line[i][1])
        cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
    return cum, cum[-1]


def interpolate_at_distance(
    line: list[list[float]], cum: list[float], target: float
) -> list[float]:
    """The coordinate at `target` feet along the line."""
    if target <= 0:
        return list(line[0])
    if target >= cum[-1]:
        return list(line[-1])
    for i in range(1, len(cum)):
        if cum[i] >= target:
            span = cum[i] - cum[i - 1]
            if span <= 0:
                return list(line[i])
            f = (target - cum[i - 1]) / span
            lon = line[i - 1][0] + f * (line[i][0] - line[i - 1][0])
            lat = line[i - 1][1] + f * (line[i][1] - line[i - 1][1])
            return [lon, lat]
    return list(line[-1])


def address_metric(coord: list[float], pre_dir: str) -> float:
    """
    Addresses increase with distance from the State/Madison origin along the
    street's axis. Larger metric = higher address (runbook Step 7).
    """
    lon, lat = coord[0], coord[1]
    if pre_dir == "E":
        return lon - ORIGIN_LON
    if pre_dir == "W":
        return ORIGIN_LON - lon
    if pre_dir == "N":
        return lat - ORIGIN_LAT
    if pre_dir == "S":
        return ORIGIN_LAT - lat
    # No usable direction: fall back to the digitization order.
    return 0.0


def one_lot_feet(total_length: float, address_span: int) -> float:
    """
    Length of a single lot's frontage, derived from the matched segment's own
    address density where that is meaningful, clamped against nonsense.
    """
    if address_span <= 0:
        return DEFAULT_LOT_FEET
    feet_per_address = total_length / address_span
    return min(MAX_LOT_FEET, max(MIN_LOT_FEET, feet_per_address * ADDRESSES_PER_LOT))


def clip_line_to_address_range(
    line: list[list[float]],
    seg_lo: int,
    seg_hi: int,
    ov_lo: int,
    ov_hi: int,
    pre_dir: str,
) -> tuple[list[list[float]] | None, str]:
    """
    Step 7 — clip a segment's centerline to the sub-range the parking row
    actually claims. Skipping this is the 43%-over-extension bug.

    Returns (clipped_line, reason). reason is one of:
      "clipped"                  normal address-range clip
      "stub_degenerate_segment"  the SCL segment's own range is one address
      "stub_zero_width_overlap"  the row/segment overlap is one address
      "too_short"                nothing usable; caller should skip
    """
    cum, total = segment_lengths_feet(line)
    if total <= 0:
        return None, "too_short"

    span = seg_hi - seg_lo

    def window(d_lo: float, d_hi: float) -> list[list[float]]:
        d_lo = max(0.0, min(total, d_lo))
        d_hi = max(0.0, min(total, d_hi))
        start = interpolate_at_distance(line, cum, d_lo)
        end = interpolate_at_distance(line, cum, d_hi)
        interior = [list(line[i]) for i in range(len(line)) if d_lo < cum[i] < d_hi]
        return [start] + interior + [end]

    if span <= 0:
        # The segment carries no address gradient to clip against. Claiming the
        # whole segment here would be the over-extension bug in miniature (some
        # of these segments run 290 ft for a single address), so take one lot at
        # the segment's midpoint instead.
        lot = min(DEFAULT_LOT_FEET, total)
        mid = total / 2.0
        return window(mid - lot / 2.0, mid + lot / 2.0), "stub_degenerate_segment"

    forward = address_metric(line[-1], pre_dir) > address_metric(line[0], pre_dir)

    if forward:
        f_lo = (ov_lo - seg_lo) / span
        f_hi = (ov_hi - seg_lo) / span
    else:
        f_lo = (seg_hi - ov_hi) / span
        f_hi = (seg_hi - ov_lo) / span

    f_lo = min(max(f_lo, 0.0), 1.0)
    f_hi = min(max(f_hi, 0.0), 1.0)
    if f_lo > f_hi:
        f_lo, f_hi = f_hi, f_lo

    d_lo, d_hi = f_lo * total, f_hi * total

    if d_hi - d_lo < MIN_CLIPPED_LENGTH_FEET:
        # Zero-width overlap: a single-address row, or a row whose range meets
        # this segment's range at exactly one address. Real restriction, so give
        # it one lot centred on that address rather than dropping it (which
        # loses it) or emitting a zero-length centerline (which the previous
        # build did, producing polygons that can never contain a point).
        lot = min(one_lot_feet(total, span), total)
        centre = (d_lo + d_hi) / 2.0
        return window(centre - lot / 2.0, centre + lot / 2.0), "stub_zero_width_overlap"

    return window(d_lo, d_hi), "clipped"


def offset_ring(
    centerline: list[list[float]], side: str, normal_space: str
) -> list[list[float]] | None:
    """
    Step 8 — a one-sided ribbon: the centerline, plus the same line pushed
    BUFFER_WIDTH_FEET to the matched side, reversed, then closed.

    normal_space="feet" computes the perpendicular in a locally-metric frame,
    which is the geometrically correct reading. "degrees" reproduces the
    naive version that treats lon/lat as a square grid; kept only so a
    baseline built that way can be identified in the diff.
    """
    n = len(centerline)
    if n < 2:
        return None

    if normal_space == "feet":
        pts = [to_feet(p[0], p[1]) for p in centerline]
    else:
        pts = [(p[0], p[1]) for p in centerline]

    offsets: list[list[float]] = []
    last_normal: tuple[float, float] | None = None

    for i in range(n):
        if i == 0:
            dx = pts[1][0] - pts[0][0]
            dy = pts[1][1] - pts[0][1]
        elif i == n - 1:
            dx = pts[-1][0] - pts[-2][0]
            dy = pts[-1][1] - pts[-2][1]
        else:
            dx = pts[i + 1][0] - pts[i - 1][0]
            dy = pts[i + 1][1] - pts[i - 1][1]

        mag = math.hypot(dx, dy)
        if mag == 0:
            if last_normal is None:
                continue
            nx, ny = last_normal
        else:
            ux, uy = dx / mag, dy / mag
            # 'L' is the left-hand side travelling along the digitized
            # direction, 'R' the right-hand side.
            nx, ny = (-uy, ux) if side == "L" else (uy, -ux)
            last_normal = (nx, ny)

        if normal_space == "feet":
            ox = pts[i][0] + nx * BUFFER_WIDTH_FEET
            oy = pts[i][1] + ny * BUFFER_WIDTH_FEET
            offsets.append([ox / FT_PER_DEG_LON, oy / FT_PER_DEG_LAT])
        else:
            dlon = BUFFER_WIDTH_FEET / FT_PER_DEG_LON
            dlat = BUFFER_WIDTH_FEET / FT_PER_DEG_LAT
            offsets.append([pts[i][0] + nx * dlon, pts[i][1] + ny * dlat])

    if len(offsets) < 2:
        return None

    ring = [list(p) for p in centerline] + offsets[::-1]
    ring.append(list(centerline[0]))
    return ring


# Grid/diagonal threshold for compass-side labelling, in degrees. A street
# whose overall bearing is within this much of an axis gets a crisp N/S/E/W
# label; anything further off is diagonal and gets an octant (NE/SE/SW/NW).
# 20 deg was chosen deliberately: Chicago's grid streets sit within a couple
# of degrees of true axes, so the tolerance is generous for them, while the
# genuine diagonals (Milwaukee, Lincoln, Clark north of the river, Archer)
# are 25-45 deg off and fall outside it cleanly. It is NOT a knob to widen if
# some street labels look wrong — a wrong label on a bending street is a
# curvature problem, which the app solves by computing the side at the
# centerline segment nearest the query rather than from this one label.
GRID_TOLERANCE_DEGREES = 20.0

_OCTANTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass_side(
    centerline: list[list[float]], ring: list[list[float]]
) -> tuple[str, float, float]:
    """
    Which compass side of its street this polygon lies on, derived from the
    geometry itself rather than from the odd/even convention.

    Worth being explicit about why: the convention (east-west streets
    even=north odd=south, north-south streets even=west odd=east) is a
    generalisation. The Street Center Lines file carries SURVEYED per-side
    parity (l_parity / r_parity), the ribbon is offset to whichever of those
    sides matched the row, and so the polygon's actual position already
    encodes the authoritative answer. Reading it back off the geometry
    therefore agrees with the survey wherever the survey and the convention
    disagree, which the convention alone could not.

    Returns (side, street_bearing, bend) where `bend` is the worst deviation
    of any single centerline segment from the overall bearing — a curvature
    measure the app can use to decide whether one label for the whole
    polygon is honest.
    """
    cpts = [to_feet(p[0], p[1]) for p in centerline]
    rpts = [to_feet(p[0], p[1]) for p in ring[:-1]]  # drop the closing repeat
    if len(cpts) < 2 or len(rpts) < 3:
        return "", 0.0, 0.0

    # Overall street direction, first vertex to last, as a compass bearing
    # folded to 0-180 (a street has an orientation, not a direction of travel).
    dx = cpts[-1][0] - cpts[0][0]
    dy = cpts[-1][1] - cpts[0][1]
    if math.hypot(dx, dy) < 1e-9:
        return "", 0.0, 0.0
    bearing = math.degrees(math.atan2(dx, dy)) % 180.0

    bend = 0.0
    for i in range(len(cpts) - 1):
        sdx = cpts[i + 1][0] - cpts[i][0]
        sdy = cpts[i + 1][1] - cpts[i][1]
        if math.hypot(sdx, sdy) < 1e-9:
            continue
        sb = math.degrees(math.atan2(sdx, sdy)) % 180.0
        dev = abs(sb - bearing)
        bend = max(bend, min(dev, 180.0 - dev))

    # Vector from the centerline's midpoint to the ring's centroid points
    # from the street toward this polygon's flank.
    mx = sum(p[0] for p in cpts) / len(cpts)
    my = sum(p[1] for p in cpts) / len(cpts)
    vx = sum(p[0] for p in rpts) / len(rpts) - mx
    vy = sum(p[1] for p in rpts) / len(rpts) - my
    if math.hypot(vx, vy) < 1e-9:
        return "", bearing, bend

    if abs(bearing - 90.0) <= GRID_TOLERANCE_DEGREES:
        side = "N" if vy > 0 else "S"            # east-west street
    elif bearing <= GRID_TOLERANCE_DEGREES or bearing >= 180.0 - GRID_TOLERANCE_DEGREES:
        side = "E" if vx > 0 else "W"            # north-south street
    else:
        off = math.degrees(math.atan2(vx, vy)) % 360.0
        side = _OCTANTS[int(((off + 22.5) % 360.0) // 45.0)]
    return side, bearing, bend


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray-casting containment test. Unstable exactly on the boundary."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            denom = yj - yi
            if denom != 0 and lon < (xj - xi) * (lat - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def distance_point_to_line_feet(
    lon: float, lat: float, line: list[list[float]]
) -> float:
    """Shortest distance in feet from a point to a polyline."""
    px, py = to_feet(lon, lat)
    best = float("inf")
    for i in range(1, len(line)):
        x0, y0 = to_feet(line[i - 1][0], line[i - 1][1])
        x1, y1 = to_feet(line[i][0], line[i][1])
        dx, dy = x1 - x0, y1 - y0
        denom = dx * dx + dy * dy
        if denom == 0:
            d = math.hypot(px - x0, py - y0)
        else:
            t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / denom))
            d = math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
        best = min(best, d)
    return best


# ---------------------------------------------------------------------------
# Step 1-4 — load and clean
# ---------------------------------------------------------------------------


def ranges_overlap(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    return not (a_hi < b_lo or a_lo > b_hi)


def load_parking_rows(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Steps 2-4 — filter to ACTIVE, clean, correct types, normalize names."""
    notes: dict[str, Any] = {
        "swapped_ranges": [],
        "backticks_stripped": [],
        "unparsable_rows": [],
    }

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
        if missing:
            raise SystemExit(
                f"{path}: missing expected column(s): {', '.join(missing)}"
            )
        raw = list(reader)

    notes["total_rows"] = len(raw)
    notes["source_columns"] = len(header)

    status_counts = collections.Counter((r.get("STATUS") or "").strip() for r in raw)
    notes["status_counts"] = dict(status_counts)

    rows: list[dict[str, Any]] = []
    for r in raw:
        if (r.get("STATUS") or "").strip().upper() != "ACTIVE":
            continue

        name_raw = r.get("STREET NAME") or ""
        if "`" in name_raw:
            notes["backticks_stripped"].append(r.get("ROW ID"))
        name = name_raw.replace("`", "").strip().upper()

        direction = (r.get("STREET DIRECTION") or "").strip().upper()
        street_type = (r.get("STREET TYPE") or "").strip().upper()

        # Step 3
        street_type = TYPE_CORRECTIONS.get((direction, name), street_type)
        # Step 4
        name = PARKING_NAME_REMAP.get(name, name)

        try:
            row_id = int(str(r["ROW ID"]).strip())
            lo = int(str(r["ADDRESS RANGE - LOW"]).strip())
            hi = int(str(r["ADDRESS RANGE - HIGH"]).strip())
        except (ValueError, KeyError, TypeError):
            notes["unparsable_rows"].append(r.get("ROW ID"))
            continue

        if lo > hi:
            notes["swapped_ranges"].append(row_id)
            lo, hi = hi, lo

        rows.append(
            {
                "row_id": row_id,
                "zone": (r.get("ZONE") or "").strip(),
                "odd_even": (r.get("ODD_EVEN") or "").strip().upper(),
                "lo": lo,
                "hi": hi,
                "direction": direction,
                "name": name,
                "street_type": street_type,
                "buffer": (r.get("BUFFER") or "").strip().upper(),
                "second_street_direction": (
                    r.get("SECOND STREET DIRECTION") or ""
                ).strip().upper(),
            }
        )

    notes["active_rows"] = len(rows)
    return rows, notes


def scl_normalized_name(props: dict[str, Any]) -> str:
    """
    Step 4 — SCL stores some names with a trailing direction word in suf_dir
    rather than in street_nam (confirmed: LINCOLN PARK W).
    """
    name = (props.get("street_nam") or "").strip().upper()
    street_type = (props.get("street_typ") or "").strip().upper()
    suf_dir = (props.get("suf_dir") or "").strip().upper()
    if not street_type and suf_dir in ("N", "S", "E", "W"):
        return f"{name} {suf_dir}"
    return name


def load_scl(path: str) -> tuple[dict[tuple[str, str, str], list[dict]], dict]:
    """Step 1b — load centerlines, keep status 'N' only, index for matching."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    features = data.get("features", [])
    notes: dict[str, Any] = {
        "total_features": len(features),
        "status_counts": dict(
            collections.Counter(
                (f.get("properties") or {}).get("status") for f in features
            )
        ),
        "multipart_features": 0,
        "skipped_no_geometry": 0,
    }

    index: dict[tuple[str, str, str], list[dict]] = collections.defaultdict(list)

    for feat in features:
        props = feat.get("properties") or {}
        if (props.get("status") or "").strip().upper() != "N":
            continue

        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        gtype = geom.get("type")

        if gtype == "MultiLineString":
            if len(coords) > 1:
                notes["multipart_features"] += 1
            parts = coords
        elif gtype == "LineString":
            parts = [coords]
        else:
            notes["skipped_no_geometry"] += 1
            continue

        for part in parts:
            if not part or len(part) < 2:
                notes["skipped_no_geometry"] += 1
                continue

            def as_int(key: str) -> int | None:
                v = props.get(key)
                if v is None:
                    return None
                s = str(v).strip()
                if not s or not s.lstrip("-").isdigit():
                    return None
                return int(s)

            entry = {
                "line": [[float(p[0]), float(p[1])] for p in part],
                "l_parity": (props.get("l_parity") or "").strip().upper(),
                "r_parity": (props.get("r_parity") or "").strip().upper(),
                "l_f": as_int("l_f_add"),
                "l_t": as_int("l_t_add"),
                "r_f": as_int("r_f_add"),
                "r_t": as_int("r_t_add"),
            }

            key = (
                (props.get("pre_dir") or "").strip().upper(),
                scl_normalized_name(props),
                (props.get("street_typ") or "").strip().upper(),
            )
            index[key].append(entry)

    notes["indexed_keys"] = len(index)
    notes["indexed_segments"] = sum(len(v) for v in index.values())
    return index, notes


# ---------------------------------------------------------------------------
# Step 6 — signage_status
# ---------------------------------------------------------------------------


def derive_signage_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    A Buffer(Y) row counts as physically signed if its range overlaps a
    Standard(N) row on the same street, same zone, ANY parity. Field-verified
    on W Concord Pl; do not weaken without new field evidence.
    """
    standard_by_street_zone: dict[tuple, list[tuple[int, int]]] = (
        collections.defaultdict(list)
    )
    for r in rows:
        if r["buffer"] == "N":
            key = (r["zone"], r["direction"], r["name"], r["street_type"])
            standard_by_street_zone[key].append((r["lo"], r["hi"]))

    for r in rows:
        if r["buffer"] == "N":
            r["signage_status"] = "Standard"
            continue
        key = (r["zone"], r["direction"], r["name"], r["street_type"])
        overlapping = any(
            ranges_overlap(r["lo"], r["hi"], lo, hi)
            for lo, hi in standard_by_street_zone.get(key, ())
        )
        r["signage_status"] = "Buffer-signed" if overlapping else "Buffer-unsigned"

    return dict(collections.Counter(r["signage_status"] for r in rows))


# ---------------------------------------------------------------------------
# Step 5, 7, 8 — join, clip, offset
# ---------------------------------------------------------------------------


def candidate_matches(
    row: dict[str, Any], index: dict[tuple[str, str, str], list[dict]]
) -> list[tuple[dict, str, int, int]]:
    """Step 5 — every (segment, side) whose address range overlaps the row."""
    key = (row["direction"], row["name"], row["street_type"])
    out: list[tuple[dict, str, int, int]] = []
    for seg in index.get(key, ()):
        for side, parity_key, lo_key, hi_key in (
            ("L", "l_parity", "l_f", "l_t"),
            ("R", "r_parity", "r_f", "r_t"),
        ):
            if seg[parity_key] != row["odd_even"]:
                continue
            seg_lo, seg_hi = seg[lo_key], seg[hi_key]
            if seg_lo is None or seg_hi is None:
                continue
            if seg_lo > seg_hi:
                seg_lo, seg_hi = seg_hi, seg_lo
            if seg_lo == 0 and seg_hi == 0:
                # No addressed frontage on this side.
                continue
            if ranges_overlap(row["lo"], row["hi"], seg_lo, seg_hi):
                out.append((seg, side, seg_lo, seg_hi))
    return out


def build_features(
    rows: list[dict[str, Any]],
    index: dict[tuple[str, str, str], list[dict]],
    normal_space: str,
) -> tuple[list[dict], dict[str, Any]]:
    features: list[dict] = []
    stats: dict[str, Any] = {
        "rows_needing_geometry": 0,
        "rows_matched": 0,
        "rows_unmatched": 0,
        "active_rows_matched": 0,
        "matches_total": 0,
        "matches_skipped_zero_overlap": 0,
        "matches_skipped_too_short": 0,
        "matches_skipped_bad_ring": 0,
        "matches_clipped": 0,
        "stub_zero_width_overlap": 0,
        "stub_degenerate_segment": 0,
        "stub_single_address_rows": 0,
        "side_counts": collections.Counter(),
        "polygons_bending_past_tolerance": 0,
        "unmatched_detail": [],
    }

    # Match rate is quoted in the runbook against ALL active rows, so measure
    # that separately from the geometry-building subset.
    for row in rows:
        if candidate_matches(row, index):
            stats["active_rows_matched"] += 1

    for row in rows:
        if row["signage_status"] not in GEOMETRY_STATUSES:
            continue
        stats["rows_needing_geometry"] += 1

        matches = candidate_matches(row, index)
        if not matches:
            stats["rows_unmatched"] += 1
            stats["unmatched_detail"].append(
                {
                    "row_id": row["row_id"],
                    "zone": row["zone"],
                    "street": f"{row['direction']} {row['name']} {row['street_type']}".strip(),
                    "range": [row["lo"], row["hi"]],
                    "odd_even": row["odd_even"],
                    "reason": (
                        "street not found in SCL"
                        if not index.get(
                            (row["direction"], row["name"], row["street_type"])
                        )
                        else "no segment/parity with an overlapping address range"
                    ),
                }
            )
            continue

        stats["rows_matched"] += 1
        built_any = False

        for seg, side, seg_lo, seg_hi in matches:
            stats["matches_total"] += 1

            ov_lo = max(row["lo"], seg_lo)
            ov_hi = min(row["hi"], seg_hi)
            if ov_lo > ov_hi:
                stats["matches_skipped_zero_overlap"] += 1
                continue

            clipped, reason = clip_line_to_address_range(
                seg["line"], seg_lo, seg_hi, ov_lo, ov_hi, row["direction"]
            )
            if clipped is None or len(clipped) < 2:
                stats["matches_skipped_too_short"] += 1
                continue

            if reason == "clipped":
                stats["matches_clipped"] += 1
            else:
                stats[reason] += 1
                if reason == "stub_zero_width_overlap" and row["lo"] == row["hi"]:
                    stats["stub_single_address_rows"] += 1

            ring = offset_ring(clipped, side, normal_space)
            if ring is None:
                stats["matches_skipped_bad_ring"] += 1
                continue

            def rnd(pts: list[list[float]]) -> list[list[float]]:
                return [
                    [round(p[0], COORD_PRECISION), round(p[1], COORD_PRECISION)]
                    for p in pts
                ]

            compass, bearing, bend = compass_side(clipped, ring)
            stats["side_counts"][compass or "?"] += 1
            if bend > GRID_TOLERANCE_DEGREES:
                stats["polygons_bending_past_tolerance"] += 1

            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "zone": row["zone"],
                        "row_id": row["row_id"],
                        "signage_status": row["signage_status"],
                        # Compass side of the street this polygon sits on,
                        # read off the geometry (see compass_side). Empty
                        # string if it could not be determined.
                        "side": compass,
                        # "O" or "E", straight from the source row's ODD_EVEN.
                        # The app needs this to pick the right flank when
                        # resolving a street address, where the house number's
                        # parity is the only thing that says which side is
                        # meant.
                        "parity": row["odd_even"],
                        # Worst deviation of any one centerline segment from
                        # this polygon's overall bearing, in degrees. A large
                        # value means the polygon bends, so `side` above
                        # describes only part of its length and the app should
                        # prefer its own per-segment computation.
                        "bend_degrees": round(bend, 1),
                        "centerline": rnd(clipped),
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [rnd(ring)],
                    },
                }
            )
            built_any = True

        if not built_any:
            # Matched a street but produced nothing usable.
            stats["rows_matched"] -= 1
            stats["rows_unmatched"] += 1
            stats["unmatched_detail"].append(
                {
                    "row_id": row["row_id"],
                    "zone": row["zone"],
                    "street": f"{row['direction']} {row['name']} {row['street_type']}".strip(),
                    "range": [row["lo"], row["hi"]],
                    "odd_even": row["odd_even"],
                    "reason": "matched a segment but no polygon could be built",
                }
            )

    return features, stats


def canonical_sort(features: list[dict]) -> list[dict]:
    """
    Order the output deterministically, independent of the order rows and
    segments happened to arrive in.

    This is not cosmetic. The publishing workflow decides whether to publish
    by comparing the output's SHA-256 against the last published one, so the
    build has to be reproducible for identical input DATA, not merely for an
    identical input FILE.

    It wasn't, and this is how it was caught: the first successful CI run
    produced a file byte-for-byte the same length as a local build, with the
    same 11,167 features and byte-identical geometry for every one of them,
    but a different hash — because the portal's `rows.csv` orders rows
    differently from a hand export through the portal UI, and features were
    emitted in input order. Left alone, the workflow would have published a
    fresh release whenever Socrata's row ordering shifted, with identical
    content.

    The key is content-derived and verified unique across all 11,167
    features, so there are no ties left to resolve nondeterministically.
    """
    def key(feature: dict) -> tuple:
        props = feature["properties"]
        centerline = props["centerline"]
        return (
            props["row_id"],
            props["zone"],
            props["signage_status"],
            centerline[0][0],
            centerline[0][1],
            centerline[-1][0],
            centerline[-1][1],
            len(centerline),
        )

    return sorted(features, key=key)


# ---------------------------------------------------------------------------
# Step 10 — validation
# ---------------------------------------------------------------------------


def resolve_zones_at(
    lon: float, lat: float, features: list[dict]
) -> list[dict[str, Any]]:
    hits = []
    for feat in features:
        ring = feat["geometry"]["coordinates"][0]
        if point_in_ring(lon, lat, ring):
            hits.append(
                {
                    "zone": feat["properties"]["zone"],
                    "row_id": feat["properties"]["row_id"],
                    "distance_ft": distance_point_to_line_feet(
                        lon, lat, feat["properties"]["centerline"]
                    ),
                }
            )
    hits.sort(key=lambda h: h["distance_ft"])
    return hits


def check_ground_truth(features: list[dict]) -> list[dict[str, Any]]:
    results = []
    for case in GROUND_TRUTH:
        hits = resolve_zones_at(case["lon"], case["lat"], features)
        got = hits[0]["zone"] if hits else None
        results.append(
            {
                "label": case["label"],
                "expected": case["expect_zone"],
                "got": got,
                "all_hits": [f"{h['zone']} ({h['distance_ft']:.1f}ft)" for h in hits],
                "pass": got == case["expect_zone"],
            }
        )
    return results


def opposite_side_fraction(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Health metric — what share of signed rows have a signed counterpart of the
    opposite parity on the same street and zone. ~91% at last measure; the
    remainder is believed to be genuinely one-sided restriction.
    """
    signed = [r for r in rows if r["signage_status"] in GEOMETRY_STATUSES]
    by_key: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in signed:
        by_key[(r["zone"], r["direction"], r["name"], r["street_type"])].append(r)

    with_opposite = 0
    for r in signed:
        key = (r["zone"], r["direction"], r["name"], r["street_type"])
        other = "E" if r["odd_even"] == "O" else "O"
        if any(
            o["odd_even"] == other and ranges_overlap(r["lo"], r["hi"], o["lo"], o["hi"])
            for o in by_key[key]
        ):
            with_opposite += 1

    total = len(signed)
    return {
        "signed_rows": total,
        "with_opposite_side": with_opposite,
        "fraction": (with_opposite / total) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Baseline diff
# ---------------------------------------------------------------------------


def feature_key(feat: dict) -> tuple:
    cl = feat["properties"]["centerline"]
    return (
        feat["properties"]["row_id"],
        round(cl[0][0], 6),
        round(cl[0][1], 6),
        round(cl[-1][0], 6),
        round(cl[-1][1], 6),
    )


def ring_displacement_feet(a: dict, b: dict) -> float | None:
    """Mean vertex displacement between two rings of equal length."""
    ra = a["geometry"]["coordinates"][0]
    rb = b["geometry"]["coordinates"][0]
    if len(ra) != len(rb):
        return None
    total = 0.0
    for pa, pb in zip(ra, rb):
        x0, y0 = to_feet(pa[0], pa[1])
        x1, y1 = to_feet(pb[0], pb[1])
        total += math.hypot(x1 - x0, y1 - y0)
    return total / len(ra)


def content_fingerprint(features: list[dict]) -> str:
    """SHA-256 of the canonically-sorted features alone.

    Deliberately excludes the envelope, so the fingerprint answers "is this
    the same zone data?" and not "is this the same file?". `data_version`
    lives in the envelope, so hashing the whole file here would make every
    build look different from every other and defeat the carry-over below.
    """
    payload = json.dumps(features, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_data_version(features: list[dict], baseline_path: str | None) -> str:
    """The date this DATA first appeared — not the date it was last rebuilt.

    Why this is not simply `date.today()`: `data_version` is written into the
    GeoJSON itself, so a clock-derived stamp would change the file's bytes on
    every monthly run even when the city changed nothing. The publishing
    workflow decides what to publish by comparing the output's SHA-256
    against the last published one, so a build-date stamp would cut a fresh
    release every month forever — the exact failure `canonical_sort` exists
    to prevent, reintroduced through the envelope.

    So: if the features are identical to the baseline's, carry the baseline's
    version forward and the file stays byte-identical. Only genuinely changed
    data gets today's date.

    This also makes the stamp mean the right thing for the app's "Data as of
    <date>" line. First-appearance is the honest reading; last-rebuild would
    advance monthly while telling the user nothing had changed.

    An unstamped baseline (anything built before this function existed)
    cannot be carried forward, so it gets today's date. That is a one-off
    publish whose only change is the stamp, after which the chain is intact.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not baseline_path or not os.path.exists(baseline_path):
        return today
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            base = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return today
    previous = base.get("data_version")
    if not previous:
        return today
    # Both sides are sorted here rather than trusting the caller. `main`
    # happens to pass already-sorted features, so this is not a live bug —
    # but a caller that did not would get today's date for identical data and
    # publish for nothing, which is precisely the failure canonical_sort was
    # introduced to stop. Cheap to make the function correct on its own terms.
    if content_fingerprint(canonical_sort(base.get("features", []))) == \
            content_fingerprint(canonical_sort(features)):
        return str(previous)

    # The data genuinely changed. Usually that means today's date — but not
    # when the baseline was ALSO published today. That is not hypothetical:
    # it happened in the Seattle project on 2026-11-09, where two builds
    # hours apart (2,858 features, then 2,857 after the city dropped a
    # blockface) were both stamped the same date. The app refuses anything
    # not strictly newer, so the second build could never reach a phone
    # holding the first.
    #
    # Chicago publishes monthly and is less likely to rebuild twice in a day
    # — but `workflow_dispatch` makes it possible at any time, and the cost
    # of being wrong is data that cannot reach an installed app.
    #
    # So a same-day change gets a sequence suffix. Plain string comparison
    # still orders these correctly, which is what both the app and the
    # manifest rely on:
    #
    #     2026-11-09 < 2026-11-09.01 < 2026-11-09.02 < ... < 2026-11-10
    #
    # including across the day boundary, because "0" < "1" at the character
    # where 2026-11-09.99 and 2026-11-10 first differ.
    #
    # THE SUFFIX IS ZERO-PADDED TO TWO DIGITS, and that is not cosmetic. An
    # unpadded sequence breaks at the tenth same-day rebuild, because
    # "2026-11-09.10" sorts BELOW "2026-11-09.9" as a plain string — so an
    # app holding .9 would refuse .10, which is the exact failure this whole
    # mechanism exists to prevent, reappearing nine steps later. Caught by a
    # test rather than by reading, after the unpadded version had already
    # been written.
    #
    # The release TAG follows the version (make_manifest derives it), so a
    # same-day republish also gets its own tag rather than colliding with
    # the morning's release.
    previous = str(previous)
    if previous == today or previous.startswith(today + "."):
        suffix = previous[len(today) + 1:]
        if suffix == "":
            n = 0
        elif len(suffix) == 2 and suffix.isdigit():
            n = int(suffix)
        else:
            # Anything else is not something to guess at — including a
            # single-digit suffix, which would advance to a padded value
            # that sorts BELOW it (".1" then ".02"). Emitting a version that
            # sorts below the baseline strands every installed app, which is
            # a worse outcome than not building at all.
            raise SystemExit(
                f"baseline data_version {previous!r} does not end in a "
                f"two-digit sequence this script can advance. Fix it by hand "
                f"rather than letting the build invent a version that might "
                f"sort below it.")
        if n >= 99:
            raise SystemExit(
                f"baseline data_version {previous!r} has exhausted the "
                f"two-digit same-day sequence. Ninety-nine rebuilds in one "
                f"day means something is wrong upstream; widening the field "
                f"would break ordering against everything already published.")
        return f"{today}.{n + 1:02d}"
    return today


def diff_against_baseline(features: list[dict], baseline_path: str) -> dict[str, Any]:
    with open(baseline_path, encoding="utf-8") as fh:
        base = json.load(fh)
    base_features = base.get("features", [])

    new_by_key = {feature_key(f): f for f in features}
    base_by_key = {feature_key(f): f for f in base_features}

    new_rows = {f["properties"]["row_id"] for f in features}
    base_rows = {f["properties"]["row_id"] for f in base_features}
    new_zones = {f["properties"]["zone"] for f in features}
    base_zones = {f["properties"]["zone"] for f in base_features}

    shared = set(new_by_key) & set(base_by_key)
    identical = 0
    moved: list[tuple[float, tuple]] = []
    shape_changed = 0

    for key in shared:
        d = ring_displacement_feet(new_by_key[key], base_by_key[key])
        if d is None:
            shape_changed += 1
        elif d < 0.5:
            identical += 1
        else:
            moved.append((d, key))

    moved.sort(reverse=True)

    return {
        "baseline_features": len(base_features),
        "new_features": len(features),
        "feature_delta": len(features) - len(base_features),
        "baseline_zones": len(base_zones),
        "new_zones": len(new_zones),
        "zones_added": sorted(new_zones - base_zones)[:40],
        "zones_removed": sorted(base_zones - new_zones)[:40],
        "zones_added_count": len(new_zones - base_zones),
        "zones_removed_count": len(base_zones - new_zones),
        "row_ids_added_count": len(new_rows - base_rows),
        "row_ids_removed_count": len(base_rows - new_rows),
        "row_ids_added": sorted(new_rows - base_rows)[:40],
        "row_ids_removed": sorted(base_rows - new_rows)[:40],
        "features_only_in_new": len(set(new_by_key) - set(base_by_key)),
        "features_only_in_baseline": len(set(base_by_key) - set(new_by_key)),
        "features_shared": len(shared),
        "shared_geometry_identical": identical,
        "shared_geometry_moved": len(moved),
        "shared_vertex_count_changed": shape_changed,
        "max_displacement_ft": round(moved[0][0], 2) if moved else 0.0,
        "median_displacement_ft": (
            round(moved[len(moved) // 2][0], 2) if moved else 0.0
        ),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_report(result: dict[str, Any]) -> str:
    L: list[str] = []
    add = L.append

    add("=" * 72)
    add("Chicago Parking Zones — pipeline build report")
    add("=" * 72)

    csv_notes = result["csv_notes"]
    add("")
    add("SOURCE — Parking Permit Zones CSV")
    add(f"  columns in export        {csv_notes['source_columns']}")
    add(f"  total rows               {csv_notes['total_rows']}")
    for status, n in sorted(csv_notes["status_counts"].items()):
        add(f"    {status or '(blank)':<22} {n}")
    add(f"  ACTIVE rows kept         {csv_notes['active_rows']}")
    if csv_notes["backticks_stripped"]:
        add(f"  backticks stripped       rows {csv_notes['backticks_stripped']}")
    if csv_notes["swapped_ranges"]:
        add(
            f"  low>high ranges swapped  {len(csv_notes['swapped_ranges'])} "
            f"rows {csv_notes['swapped_ranges'][:10]}"
        )
    if csv_notes["unparsable_rows"]:
        add(f"  unparsable rows dropped  {csv_notes['unparsable_rows']}")

    scl = result["scl_notes"]
    add("")
    add("SOURCE — Street Center Lines GeoJSON")
    add(f"  total features           {scl['total_features']}")
    for status, n in sorted(
        scl["status_counts"].items(), key=lambda kv: -kv[1]
    ):
        add(f"    status {str(status):<15} {n}")
    add(f"  indexed (status N)       {scl['indexed_segments']} segments")
    add(f"  distinct street keys     {scl['indexed_keys']}")
    if scl["multipart_features"]:
        add(f"  multi-part geometries    {scl['multipart_features']}")
    if scl["skipped_no_geometry"]:
        add(f"  skipped, no usable line  {scl['skipped_no_geometry']}")

    add("")
    add("STEP 6 — signage_status (ACTIVE rows)")
    for status, n in sorted(result["signage_counts"].items()):
        add(f"  {status:<24} {n}")

    st = result["stats"]
    add("")
    add("STEP 5/7/8 — join, clip, offset")
    add(f"  rows needing geometry    {st['rows_needing_geometry']}")
    add(f"  rows matched             {st['rows_matched']}")
    add(f"  rows unmatched           {st['rows_unmatched']}")
    add(f"  row-segment matches      {st['matches_total']}")
    add(f"    normal address clip    {st['matches_clipped']}")
    add(f"    one-lot stub (overlap) {st['stub_zero_width_overlap']}")
    add(f"      of which single-addr {st['stub_single_address_rows']}")
    add(f"    one-lot stub (segment) {st['stub_degenerate_segment']}")
    add(f"  skipped, zero overlap    {st['matches_skipped_zero_overlap']}")
    add(f"  skipped, too short       {st['matches_skipped_too_short']}")
    add(f"  skipped, bad ring        {st['matches_skipped_bad_ring']}")
    add(f"  features built           {result['feature_count']}")
    add(f"  distinct zones           {result['zone_count']}")

    add("")
    add("SIDE OF STREET (per-polygon, read off the geometry)")
    sc = st["side_counts"]
    grid = sum(sc[k] for k in ("N", "S", "E", "W"))
    diag = sum(sc[k] for k in ("NE", "SE", "SW", "NW"))
    total = max(1, grid + diag + sc["?"])
    add(f"  grid-aligned             {grid} ({grid / total:.1%})")
    for k in ("N", "S", "E", "W"):
        add(f"    {k:<22} {sc[k]}")
    add(f"  diagonal                 {diag} ({diag / total:.1%})")
    for k in ("NE", "SE", "SW", "NW"):
        add(f"    {k:<22} {sc[k]}")
    if sc["?"]:
        add(f"  INDETERMINATE            {sc['?']}")
    add(
        f"  bending past {GRID_TOLERANCE_DEGREES:.0f} deg       "
        f"{st['polygons_bending_past_tolerance']}"
    )
    add("    ^ these polygons curve enough that one side label describes only")
    add("      part of their length; the app computes side per segment instead.")

    add("")
    add("STEP 10 — ground truth (pass/fail)")
    for r in result["ground_truth"]:
        mark = "PASS" if r["pass"] else "FAIL"
        exp = r["expected"] or "unrestricted"
        got = r["got"] or "unrestricted"
        add(f"  [{mark}] {r['label']}")
        add(f"         expected {exp}, got {got}")
        if len(r["all_hits"]) > 1:
            add(f"         all containing polygons: {', '.join(r['all_hits'])}")

    add("")
    add("HEALTH METRICS")
    mr = result["match_rate"]
    add(
        f"  ACTIVE row match rate    {mr['fraction']*100:.2f}% "
        f"({mr['matched']}/{mr['total']}) — runbook ~98.7%"
    )
    op = result["opposite_side"]
    add(
        f"  opposite-side coverage   {op['fraction']*100:.2f}% "
        f"({op['with_opposite_side']}/{op['signed_rows']}) — runbook ~91%"
    )

    if result.get("diff"):
        d = result["diff"]
        add("")
        add("BASELINE DIFF")
        add(
            f"  features                 {d['baseline_features']} -> "
            f"{d['new_features']} ({d['feature_delta']:+d})"
        )
        add(f"  zones                    {d['baseline_zones']} -> {d['new_zones']}")
        add(
            f"  zones added/removed      {d['zones_added_count']} / "
            f"{d['zones_removed_count']}"
        )
        if d["zones_added"]:
            add(f"    added:   {', '.join(d['zones_added'])}")
        if d["zones_removed"]:
            add(f"    removed: {', '.join(d['zones_removed'])}")
        add(
            f"  row_ids added/removed    {d['row_ids_added_count']} / "
            f"{d['row_ids_removed_count']}"
        )
        add(f"  features shared          {d['features_shared']}")
        add(f"    geometry identical     {d['shared_geometry_identical']}")
        add(f"    geometry moved         {d['shared_geometry_moved']}")
        add(f"    vertex count changed   {d['shared_vertex_count_changed']}")
        add(f"  only in new build        {d['features_only_in_new']}")
        add(f"  only in baseline         {d['features_only_in_baseline']}")
        add(
            f"  displacement (moved)     median "
            f"{d['median_displacement_ft']}ft, max {d['max_displacement_ft']}ft"
        )

    add("")
    add("VERDICT")
    for line in result["verdict_lines"]:
        add(f"  {line}")
    add(f"  overall: {'PASS' if result['ok'] else 'FAIL'}")
    add("")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Rebuild chicago_parking_zones.geojson from city source data."
    )
    ap.add_argument("--zones-csv", required=True, help="Parking Permit Zones CSV")
    ap.add_argument("--scl", required=True, help="Street Center Lines GeoJSON")
    ap.add_argument("--out", required=True, help="output GeoJSON path")
    ap.add_argument("--baseline", help="existing build to diff against")
    ap.add_argument("--report", help="write the text report here as well as stdout")
    ap.add_argument("--report-json", help="write a machine-readable report here")
    ap.add_argument(
        "--normal-space",
        choices=("feet", "degrees"),
        default="feet",
        help="frame for the perpendicular offset (default feet, the correct one)",
    )
    ap.add_argument(
        "--unmatched-csv", help="write the unmatched-row detail here as CSV"
    )
    ap.add_argument(
        "--no-write",
        action="store_true",
        help="run everything and report, but do not write the output file",
    )
    args = ap.parse_args(argv)

    rows, csv_notes = load_parking_rows(args.zones_csv)
    index, scl_notes = load_scl(args.scl)
    signage_counts = derive_signage_status(rows)
    features, stats = build_features(rows, index, args.normal_space)
    # Must happen before the hash anyone compares is taken — see canonical_sort.
    features = canonical_sort(features)

    zones = {f["properties"]["zone"] for f in features}

    result: dict[str, Any] = {
        "csv_notes": csv_notes,
        "scl_notes": scl_notes,
        "signage_counts": signage_counts,
        "stats": {k: v for k, v in stats.items() if k != "unmatched_detail"},
        "feature_count": len(features),
        "zone_count": len(zones),
        "normal_space": args.normal_space,
        "ground_truth": check_ground_truth(features),
        "match_rate": {
            "matched": stats["active_rows_matched"],
            "total": csv_notes["active_rows"],
            "fraction": (
                stats["active_rows_matched"] / csv_notes["active_rows"]
                if csv_notes["active_rows"]
                else 0.0
            ),
        },
        "opposite_side": opposite_side_fraction(rows),
        "unmatched_detail": stats["unmatched_detail"],
    }

    if args.baseline:
        result["diff"] = diff_against_baseline(features, args.baseline)

    # Verdict
    verdict_lines: list[str] = []
    ok = True

    failed_gt = [r for r in result["ground_truth"] if not r["pass"]]
    if failed_gt:
        ok = False
        for r in failed_gt:
            verdict_lines.append(
                f"FAIL ground truth: {r['label']} expected "
                f"{r['expected'] or 'unrestricted'}, got {r['got'] or 'unrestricted'}"
            )
    else:
        verdict_lines.append("OK all 4 ground-truth points correct")

    if result["match_rate"]["fraction"] < MIN_MATCH_RATE:
        ok = False
        verdict_lines.append(
            f"FAIL match rate {result['match_rate']['fraction']*100:.2f}% "
            f"below floor {MIN_MATCH_RATE*100:.0f}%"
        )
    else:
        verdict_lines.append("OK match rate within tolerance")

    op_delta = abs(
        result["opposite_side"]["fraction"] - EXPECTED_OPPOSITE_SIDE_FRACTION
    )
    if op_delta > OPPOSITE_SIDE_TOLERANCE:
        ok = False
        verdict_lines.append(
            f"FAIL opposite-side coverage "
            f"{result['opposite_side']['fraction']*100:.2f}% is more than "
            f"{OPPOSITE_SIDE_TOLERANCE*100:.0f}pp from the expected "
            f"{EXPECTED_OPPOSITE_SIDE_FRACTION*100:.0f}%"
        )
    else:
        verdict_lines.append("OK opposite-side coverage within tolerance")

    if result.get("diff"):
        base_n = result["diff"]["baseline_features"]
        if base_n:
            drift = abs(result["feature_count"] - base_n) / base_n
            if drift > MAX_FEATURE_COUNT_DRIFT:
                ok = False
                verdict_lines.append(
                    f"FAIL feature count moved {drift*100:.1f}% vs baseline "
                    f"(limit {MAX_FEATURE_COUNT_DRIFT*100:.0f}%)"
                )
            else:
                verdict_lines.append(
                    f"OK feature count within {MAX_FEATURE_COUNT_DRIFT*100:.0f}% "
                    f"of baseline"
                )

    result["verdict_lines"] = verdict_lines
    result["ok"] = ok

    report = build_report(result)
    print(report)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)

    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)

    if args.unmatched_csv:
        with open(args.unmatched_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["row_id", "zone", "street", "range_low", "range_high",
                        "odd_even", "reason"])
            for u in stats["unmatched_detail"]:
                w.writerow([u["row_id"], u["zone"], u["street"], u["range"][0],
                            u["range"][1], u["odd_even"], u["reason"]])

    if not ok:
        print(
            "\nValidation failed — output NOT written. "
            "Resolve before bundling this file into the app.",
            file=sys.stderr,
        )
        return 1

    if args.no_write:
        print("\n--no-write set: validation passed, output file not written.")
        return 0

    # `data_version` is what lets the app know what it already has, which is
    # what lets the updater refuse to install something older. Without it the
    # bundled file is undatable and "never install older" cannot be
    # implemented at all. See resolve_data_version for why it is not today's
    # date. Keys are ordered so the stamp sits at the top of the file where a
    # human reading `head -c 200` will see it.
    data_version = resolve_data_version(features, args.baseline)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "type": "FeatureCollection",
                "data_version": data_version,
                "features": features,
            },
            fh,
        )

    print(f"\nWrote {args.out} ({len(features)} features, {len(zones)} zones, "
          f"data_version {data_version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
