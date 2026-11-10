#!/usr/bin/env python3
"""
Tests for `data_version` — the one value in this pipeline that an installed
app compares to decide what to install.

Run:  python3 -I test_data_version.py
Exits non-zero on any failure. No network, runs in under a second.

WHY THIS FILE EXISTS. Everything else here can be checked by looking at the
output: a wrong polygon is visible, a wrong count is printed. A wrong version
is invisible at build time and only shows up as apps that quietly stop
updating, or that re-download 8 MB every week forever. Both failure modes
have nearly happened:

  - a clock-derived stamp would have changed the file's bytes on every
    monthly run, publishing a release for data that had not changed;
  - two builds on the same day would have shared one version, so the second
    could never reach a phone holding the first. That one happened for real
    in the Seattle project on 2026-11-09.

The ordering these tests assert is load-bearing in two places: the app
compares `data_version` as a plain string, and `make_manifest.py` derives the
release tag from it.
"""

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bz", os.path.join(HERE, "build_zones.py"))
bz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bz)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
FAILS = 0


def check(label, got, want):
    global FAILS
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {got!r}, expected {want!r}")
        FAILS += 1


def feature(zone, row_id, lon=-87.68, lat=41.91):
    """A minimal feature carrying every field canonical_sort keys on.

    `signage_status` is one of them and is easy to forget — leaving it out
    makes canonical_sort raise rather than quietly mis-sort, which is the
    right failure but an unhelpful one to meet inside a version test.
    """
    centerline = [[lon, lat], [lon + 0.0004, lat]]
    return {
        "type": "Feature",
        "properties": {
            "zone": zone,
            "row_id": row_id,
            "signage_status": "Standard",
            "side": "N",
            "centerline": centerline,
        },
        "geometry": {"type": "Polygon", "coordinates": [centerline + [[lon, lat]]]},
    }


FEATURES = [feature(str(i % 7 + 1), 1000 + i, lon=-87.68 + 0.0001 * i)
            for i in range(50)]

D = tempfile.mkdtemp()


def baseline(name, version, features):
    envelope = {"type": "FeatureCollection"}
    if version:
        envelope["data_version"] = version
    envelope["features"] = features
    path = os.path.join(D, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh)
    return path


changed = json.loads(json.dumps(FEATURES))
changed[0]["properties"]["zone"] = "9999"

print("no baseline to compare against")
check("no baseline -> today", bz.resolve_data_version(FEATURES, None), TODAY)
check("missing baseline file -> today",
      bz.resolve_data_version(FEATURES, os.path.join(D, "absent.geojson")), TODAY)
corrupt = os.path.join(D, "corrupt.geojson")
open(corrupt, "w").write("{ truncated")
check("unreadable baseline -> today", bz.resolve_data_version(FEATURES, corrupt), TODAY)

print("\ncarry-forward: identical data keeps its version")
old = baseline("old.geojson", "2026-03-01", FEATURES)
check("identical content carries the old date forward",
      bz.resolve_data_version(FEATURES, old), "2026-03-01")
check("reordered input still carries forward",
      bz.resolve_data_version(list(reversed(FEATURES)), old), "2026-03-01")
unstamped = baseline("unstamped.geojson", None, FEATURES)
check("identical content but unstamped baseline -> today",
      bz.resolve_data_version(FEATURES, unstamped), TODAY)

print("\nchanged data on an older baseline")
check("changed content -> today",
      bz.resolve_data_version(changed, old), TODAY)
check("one fewer feature -> today",
      bz.resolve_data_version(FEATURES[:-1], old), TODAY)

print("\nchanged data when the baseline is ALSO from today")
today_base = baseline("today.geojson", TODAY, FEATURES)
check("first same-day change -> .01",
      bz.resolve_data_version(changed, today_base), TODAY + ".01")
seq1 = baseline("seq1.geojson", TODAY + ".01", FEATURES)
check("second same-day change -> .02",
      bz.resolve_data_version(changed, seq1), TODAY + ".02")
check("unchanged content on a suffixed baseline carries the suffix forward",
      bz.resolve_data_version(FEATURES, seq1), TODAY + ".01")

# The case that made padding necessary: unpadded, .10 sorts BELOW .9, so an
# app holding .9 would refuse .10.
seq9 = baseline("seq9.geojson", TODAY + ".09", FEATURES)
check("the sequence crosses 9 -> .10", bz.resolve_data_version(changed, seq9), TODAY + ".10")
check("and .10 really does sort above .09", TODAY + ".09" < TODAY + ".10", True)
seq99 = baseline("seq99.geojson", TODAY + ".99", FEATURES)
try:
    got99 = bz.resolve_data_version(changed, seq99)
    check("exhausting the sequence aborts", got99, "SystemExit")
except SystemExit:
    check("exhausting the sequence aborts", "SystemExit", "SystemExit")

# A single-digit suffix would advance to a padded value sorting below it.
unpadded = baseline("unpadded.geojson", TODAY + ".1", FEATURES)
try:
    gotu = bz.resolve_data_version(changed, unpadded)
    check("an unpadded baseline aborts rather than sorting backwards", gotu, "SystemExit")
except SystemExit:
    check("an unpadded baseline aborts rather than sorting backwards", "SystemExit", "SystemExit")

print("\na suffix this script cannot advance must stop the build")
weird = baseline("weird.geojson", TODAY + ".beta", FEATURES)
try:
    got = bz.resolve_data_version(changed, weird)
    check("unrecognised suffix aborts rather than guessing", got, "SystemExit")
except SystemExit:
    check("unrecognised suffix aborts rather than guessing", "SystemExit", "SystemExit")

print("\nordering — relied on by the app and by the release tag")
ordered = ["2026-03-01", "2026-11-08", "2026-11-09", "2026-11-09.01",
           "2026-11-09.02", "2026-11-09.09", "2026-11-09.10", "2026-11-09.99",
           "2026-11-10", "2026-12-01"]
check("every version sorts above the one published before it",
      all(ordered[i] < ordered[i + 1] for i in range(len(ordered) - 1)), True)
check("a same-day suffix still sorts below the next day",
      "2026-11-09.99" < "2026-11-10", True)

print(f"\nFAILURES: {FAILS}")
sys.exit(1 if FAILS else 0)
