#!/usr/bin/env python3
"""
fetch_sources.py — download the two Chicago Data Portal datasets the
pipeline needs, with retries and validation.

WHY THIS EXISTS, and why the obvious URL is wrong
-------------------------------------------------
The runbook names the Street Center Lines dataset as `6imu-meau`, which is
the ID in the human-facing portal URL. That asset is a **map visualization,
not a data table**: its metadata reports `assetType: "map"` with zero
columns, and SODA queries against it answer "No such column: street_nam".
Three separate endpoints on it return structurally valid but EMPTY results:

  /api/geospatial/6imu-meau?method=export&format=GeoJSON
      -> HTTP 200 and exactly 53 bytes: `{"type":"FeatureCollection","features":[`
         and nothing else. curl exits 0, so this failure is silent.
         (Confirmed not specific to this dataset — other "- Map" assets on
         the portal return the same 53-byte stub.)
  /resource/6imu-meau.json?$limit=1          -> `[{}]`
  /api/views/6imu-meau/rows.csv              -> 56,339 EMPTY lines

The actual data lives in the parent dataset named by the map's own
`modifyingViewUid` / `datasetUid`: **pr57-gg9e**. That one serves proper
GeoJSON with every field the pipeline needs, and `$where=status='N'` on it
returns 55,503 — matching the hand-exported file the project has been
building from.

So: do not "simplify" this back to the geospatial export URL. It looks
right, returns HTTP 200, and silently yields nothing.

The Parking Permit Zones CSV is a normal dataset and its row export works
directly.

Standard library only, matching build_zones.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORTAL = "https://data.cityofchicago.org"

# Parking Permit Zones — a real dataset; the row export carries the
# display-name headers the pipeline expects ("ROW ID", "ADDRESS RANGE - LOW").
ZONES_DATASET = "u9xt-hiju"
ZONES_CSV_URL = f"{PORTAL}/api/views/{ZONES_DATASET}/rows.csv?accessType=DOWNLOAD"

# Street Center Lines — the DATA dataset, not the map visualization.
SCL_DATASET = "pr57-gg9e"

# Pages of 10,000 keep each request comfortably inside portal timeouts;
# ~6 requests for the full 56k features.
SCL_PAGE_SIZE = 10_000

# Plausibility floors. These exist because the failure mode here is a
# well-formed empty answer, not an HTTP error.
MIN_CSV_BYTES = 300_000
MIN_CSV_ROWS = 9_000
MIN_SCL_FEATURES = 50_000

USER_AGENT = "rpz-chi-data-pipeline (+https://github.com/iansiwatt/rpz-chi)"


def fetch(url: str, *, attempts: int = 5, timeout: int = 180) -> bytes:
    """GET with exponential backoff. The portal intermittently answers
    `service-unavailable` even for queries that work moments later."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            last = exc
            wait = 2 ** i * 3
            print(f"   attempt {i + 1}/{attempts} failed ({exc}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"Giving up on {url}: {last}")


def fetch_zones_csv(path: str) -> None:
    print(f"Parking Permit Zones <- {ZONES_CSV_URL}")
    body = fetch(ZONES_CSV_URL)
    if len(body) < MIN_CSV_BYTES:
        raise SystemExit(
            f"CSV is only {len(body)} bytes (expected >{MIN_CSV_BYTES}). "
            "The portal likely returned an empty or stub response."
        )
    text = body.decode("utf-8-sig", errors="replace")
    lines = text.count("\n")
    if lines < MIN_CSV_ROWS:
        raise SystemExit(
            f"CSV has only ~{lines} lines (expected >{MIN_CSV_ROWS}). "
            "Refusing to build from a truncated download."
        )
    header = text.split("\n", 1)[0]
    if "ROW ID" not in header or "ADDRESS RANGE - LOW" not in header:
        raise SystemExit(
            f"CSV header is not what the pipeline expects.\n  got: {header[:200]}"
        )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"   {len(body):,} bytes, ~{lines:,} lines -> {path}")


def scl_page_url(limit: int, offset: int) -> str:
    # $order=:id makes paging stable and repeatable; verified that
    # offset 0 twice returns the same features, and offset 2 returns the next.
    query = urllib.parse.urlencode(
        {"$limit": limit, "$offset": offset, "$order": ":id"}
    )
    return f"{PORTAL}/resource/{SCL_DATASET}.geojson?{query}"


def fetch_scl_geojson(path: str) -> None:
    print(f"Street Center Lines <- {PORTAL}/resource/{SCL_DATASET}.geojson (paged)")
    features: list[dict] = []
    offset = 0
    while True:
        url = scl_page_url(SCL_PAGE_SIZE, offset)
        body = fetch(url)
        try:
            page = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"Page at offset {offset} is not valid JSON ({exc}). "
                f"First 200 bytes: {body[:200]!r}"
            )
        got = page.get("features") or []
        features.extend(got)
        print(f"   offset {offset:>6}: +{len(got):>6} features (total {len(features):,})")
        if len(got) < SCL_PAGE_SIZE:
            break
        offset += SCL_PAGE_SIZE
        if offset > 500_000:  # paranoia: never loop forever
            raise SystemExit("Pagination exceeded 500,000 rows; aborting.")

    if len(features) < MIN_SCL_FEATURES:
        raise SystemExit(
            f"Only {len(features)} features (expected >{MIN_SCL_FEATURES}). "
            "Refusing to build from a partial download."
        )

    # The pipeline reads these specific properties; a schema change upstream
    # should fail here rather than produce a build with no matches.
    needed = {"pre_dir", "street_nam", "street_typ", "suf_dir", "status",
              "l_f_add", "l_t_add", "r_f_add", "r_t_add", "l_parity", "r_parity"}
    present = set(features[0].get("properties") or {})
    missing = needed - present
    if missing:
        raise SystemExit(
            f"Feature properties are missing fields the pipeline needs: "
            f"{sorted(missing)}\n  present: {sorted(present)[:20]}"
        )

    with_geom = sum(1 for f in features if (f.get("geometry") or {}).get("coordinates"))
    status_n = sum(1 for f in features
                   if ((f.get("properties") or {}).get("status") or "").upper() == "N")
    print(f"   {len(features):,} features, {with_geom:,} with geometry, "
          f"{status_n:,} with status N")
    if with_geom < MIN_SCL_FEATURES:
        raise SystemExit(
            f"Only {with_geom} features carry geometry. This is the signature of "
            "querying a map visualization instead of its parent dataset — see the "
            "note at the top of this file."
        )

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)
    print(f"   -> {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--zones-csv-out", required=True)
    ap.add_argument("--scl-out", required=True)
    ap.add_argument("--skip-zones", action="store_true")
    ap.add_argument("--skip-scl", action="store_true")
    args = ap.parse_args()

    if not args.skip_zones:
        fetch_zones_csv(args.zones_csv_out)
    if not args.skip_scl:
        fetch_scl_geojson(args.scl_out)
    print("Both source datasets downloaded and validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
