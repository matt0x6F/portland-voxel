#!/bin/sh
# OSM for the AOI (see scripts/aoi.py) + 100 m margin, as Overpass JSON with geometry clipped to the bbox, written to
# the AOI's own file (aoi.OSM_FILE: data/raw/osm_<name>.json, so chunks can be cut side by side).
# Default: the daily Geofabrik extract for Oregon, cut locally by scripts/osm_pbf.py (same output as Overpass
# `nwr(bbox); out body geom(bbox);`). The public Overpass servers turn bbox pulls away when busy (502, queue full:
# first Portland run, 2026-11-03); OVERPASS=1 uses them anyway. NO_PBF_REFRESH=1 keeps the extract as it is
# (build_city.sh refreshes it once, before chunks run in parallel).
set -e
cd "$(dirname "$0")/.."
eval "$(PYTHONPATH=scripts uv run python -c "
from aoi import lonlat_bounds, OSM_FILE
lon0, lat0, lon1, lat1 = lonlat_bounds(100)
print('BB=%.5f,%.5f,%.5f,%.5f; OUTF=%s' % (lat0, lon0, lat1, lon1, OSM_FILE))")"
echo "bbox $BB -> $OUTF"
mkdir -p data/raw
if [ -z "$OVERPASS" ]; then
  P=data/raw/geofabrik/oregon-latest.osm.pbf
  mkdir -p data/raw/geofabrik
  if [ ! -s $P ] || { [ -z "$NO_PBF_REFRESH" ] && [ -n "$(find $P -mtime +1)" ]; }; then
    curl -sS -L -o $P.part.$$ https://download.geofabrik.de/north-america/us/oregon-latest.osm.pbf && mv $P.part.$$ $P
  fi
  uv run --with osmium python scripts/osm_pbf.py $P $BB "$OUTF"
  exit 0
fi
Q="[out:json][timeout:600][maxsize:536870912];nwr($BB);out body geom($BB);"
for round in 1 2 3 4 5 6; do for u in https://overpass-api.de/api/interpreter https://overpass.private.coffee/api/interpreter https://overpass.kumi.systems/api/interpreter; do
  curl -s -m 900 -A "portland-voxel/0.1" --data-urlencode "data=$Q" "$u" -o "$OUTF.part"
  if head -c 20 "$OUTF.part" | grep -q '{'; then mv "$OUTF.part" "$OUTF"; echo "OK $u"; exit 0; fi
  echo "fail $u (round $round)"; sleep $((round * 10))
done; done
exit 1
