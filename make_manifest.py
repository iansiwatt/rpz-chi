#!/usr/bin/env python3
"""
make_manifest.py — write the small manifest the iOS app fetches to decide
whether a newer data file exists.

The split, and why:

  manifest.json       committed to the repo, ~1 KB, fetched over
                      raw.githubusercontent.com. Mutable pointer. Cheap to
                      fetch weekly, no API rate limit to worry about.

  the GeoJSON (~8 MB) published as a GitHub Release asset, immutable, one
                      per dated tag. Keeping it out of git means a quarterly
                      rebuild doesn't add 8 MB to the repo's history forever.

The app fetches the manifest first, compares `data_version` against what it
already has, and downloads `data_url` only when the manifest is newer. It
then verifies `sha256` and `feature_count` before swapping anything in.

`data_version` is a UTC date in YYYY-MM-DD form, so plain string comparison
orders versions correctly and a human can read it. It is read out of the
GeoJSON itself, which build_zones.py stamps with the date the DATA first
appeared, not the date it was last rebuilt — see resolve_data_version there.
The file and the manifest therefore always agree, which is what allows the
app to compare the manifest against what it already has without downloading
anything.

Standard library only, to match build_zones.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate manifest.json for the published zone data."
    )
    ap.add_argument("--data", required=True, help="the built GeoJSON")
    ap.add_argument("--out", required=True, help="manifest.json path to write")
    ap.add_argument(
        "--repo",
        required=True,
        help="owner/repo, used to build the release asset URL",
    )
    ap.add_argument(
        "--tag",
        help="release tag; defaults to data-<the file's data_version>",
    )
    ap.add_argument(
        "--report-json",
        help="build_zones.py --report-json output, to carry its health metrics",
    )
    args = ap.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = os.path.basename(args.data)

    with open(args.data, encoding="utf-8") as fh:
        payload = json.load(fh)
    features = payload.get("features", [])

    # Take the version from the DATA FILE, not the clock. The GeoJSON now
    # carries its own `data_version`, set by build_zones.py to the date the
    # data first appeared rather than the date it was last rebuilt, and that
    # file is the thing the app actually installs. Deriving the manifest's
    # version from today instead would let the two disagree: a run that
    # carried a version forward would advertise today's date, the app would
    # believe a newer file existed, download 8 MB, and find the data it
    # already had — every month, forever.
    #
    # Falling back to today covers only a file built before the stamp existed.
    data_version = str(payload.get("data_version") or today)
    tag = args.tag or f"data-{data_version}"

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_version": data_version,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release_tag": tag,
        "data_url": (
            f"https://github.com/{args.repo}/releases/download/{tag}/{filename}"
        ),
        "filename": filename,
        "bytes": os.path.getsize(args.data),
        "sha256": sha256_of(args.data),
        "feature_count": len(features),
        "zone_count": len({f["properties"]["zone"] for f in features}),
    }

    # Carry the build's own health metrics through, so a human reading the
    # manifest can see what the data was checked against — and so the app
    # could, if ever wanted, refuse a file whose metrics look wrong.
    if args.report_json:
        with open(args.report_json, encoding="utf-8") as fh:
            report = json.load(fh)
        manifest["checks"] = {
            "ground_truth_all_passed": all(
                g["pass"] for g in report.get("ground_truth", [])
            ),
            "match_rate": round(report.get("match_rate", {}).get("fraction", 0.0), 5),
            "opposite_side_coverage": round(
                report.get("opposite_side", {}).get("fraction", 0.0), 5
            ),
            "rows_unmatched": report.get("stats", {}).get("rows_unmatched"),
        }
        manifest["source_rows"] = {
            "parking_permit_zones_total": report.get("csv_notes", {}).get("total_rows"),
            "parking_permit_zones_active": report.get("csv_notes", {}).get(
                "active_rows"
            ),
            "street_center_lines_features": report.get("scl_notes", {}).get(
                "total_features"
            ),
        }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {args.out}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
