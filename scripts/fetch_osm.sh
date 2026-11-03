#!/bin/sh
# OSM for the AOI (see scripts/aoi.py) + 100 m margin, as Overpass JSON with geometry clipped to the bbox.
# Default: the daily Geofabrik extract for Oregon, cut locally by scripts/osm_pbf.py (same output as Overpass
# `nwr(bbox); out body geom(bbox);`). The public Overpass servers turn bbox pulls away when busy (502, queue full:
# first Portland run, 2026-11-03); OVERPASS=1 uses them anyway.
set -e
cd "$(dirname "$0")/.."
BB=$(PYTHONPATH=scripts uv run python -c "
from aoi import lonlat_bounds
lon0, lat0, lon1, lat1 = lonlat_bounds(100)
print('%.5f,%.5f,%.5f,%.5f' % (lat0, lon0, lat1, lon1))")
echo "bbox $BB"
mkdir -p data/raw
if [ -z "$OVERPASS" ]; then
  P=data/raw/geofabrik/oregon-latest.osm.pbf
  mkdir -p data/raw/geofabrik
  if [ ! -s $P ] || [ -n "$(find $P -mtime +1)" ]; then
    curl -sS -L -o $P.part https://download.geofabrik.de/north-america/us/oregon-latest.osm.pbf && mv $P.part $P
  fi
  uv run --with osmium python scripts/osm_pbf.py $P $BB data/raw/osm.json
  exit 0
fi
Q="[out:json][timeout:600][maxsize:536870912];nwr($BB);out body geom($BB);"
for round in 1 2 3 4 5 6; do for u in https://overpass-api.de/api/interpreter https://overpass.private.coffee/api/interpreter https://overpass.kumi.systems/api/interpreter; do
  curl -s -m 900 -A "portland-voxel/0.1" --data-urlencode "data=$Q" "$u" -o data/raw/osm.json.part
  if head -c 20 data/raw/osm.json.part | grep -q '{'; then mv data/raw/osm.json.part data/raw/osm.json; echo "OK $u"; exit 0; fi
  echo "fail $u (round $round)"; sleep $((round * 10))
done; done
exit 1
