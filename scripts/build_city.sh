#!/bin/sh
# Citywide build: Portland in 4096 m chunks (scripts/city_grid.py), each fetched, voxelised and written into the one
# tile pyramid in web/public/data/tiles, then merged (scripts/merge_chunks.py). Chunks build one at a time (they grow
# one shared palette, and each peaks at ~10-20 GB); a chunk with a part.json is skipped unless REBUILD=1.
#
# usage: sh scripts/build_city.sh                    every chunk with >= 1% of the city
#        CHUNKS="5,4 6,4" sh scripts/build_city.sh   just these
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH=scripts CITY=portland
mkdir -p data/raw data/debug data/cache data/chunks web/public/data
[ -s data/raw/city_grid.json ] || uv run --with osmium python scripts/city_grid.py
if [ -z "$CHUNKS" ]; then
  CHUNKS=$(uv run python -c "
import json
g = json.load(open('data/raw/city_grid.json'))
print(' '.join(f\"{c['cx']},{c['cy']}\" for c in g['chunks'] if c['share'] >= 0.01))")
fi
for c in $CHUNKS; do
  name="c${c%,*}_${c#*,}"
  if [ -s "data/chunks/$name/part.json" ] && [ -z "$REBUILD" ]; then echo "chunk $c: built, skipping"; continue; fi
  echo "=== chunk $c ($name)"
  export CHUNK=$c
  uv run python scripts/aoi.py
  sh scripts/fetch_osm.sh
  uv run python scripts/fetch_lidar.py
  uv run python scripts/fetch_ortho.py
  uv run python scripts/prep.py
  if ! uv run python scripts/voxelize.py > "data/debug/voxelize_$name.log" 2>&1; then
    tail -30 "data/debug/voxelize_$name.log"; exit 1
  fi
  grep -E "^chunk |chunk tiles|green-space|low buildings" "data/debug/voxelize_$name.log" || true
  uv run python scripts/places.py
  unset CHUNK
done
uv run python scripts/merge_chunks.py
uv run python scripts/climate.py
