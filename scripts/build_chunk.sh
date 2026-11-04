#!/bin/sh
# One chunk of the citywide map, start to finish: OSM cut, lidar, ortho, resample, voxelise (tiles into the city
# pyramid, part.json, palette merged under its lock), search places. Every working file is the chunk's own
# (scripts/aoi.py), so build_city.sh runs several of these side by side. Log: data/debug/build_<name>.log.
#
# usage: sh scripts/build_chunk.sh cx,cy
set -e
cd "$(dirname "$0")/.."
c=$1
name="c${c%,*}_${c#*,}"
export PYTHONPATH=scripts CITY=portland CHUNK=$c NO_PBF_REFRESH=1
log="data/debug/build_$name.log"
mkdir -p data/debug
{
  echo "=== chunk $c ($name) $(date +%H:%M:%S)"
  uv run python scripts/aoi.py
  sh scripts/fetch_osm.sh
  uv run python scripts/fetch_lidar.py
  uv run python scripts/fetch_ortho.py
  uv run python scripts/prep.py
  uv run python scripts/voxelize.py > "data/debug/voxelize_$name.log" 2>&1 || { tail -30 "data/debug/voxelize_$name.log"; exit 1; }
  grep -E "^chunk |chunk tiles|green-space|low buildings|yards from" "data/debug/voxelize_$name.log" || true
  uv run python scripts/places.py
  rm -f "data/prep_$name.npz"  # ~1 GB per chunk, rebuilt from the cached rasters when needed
  echo "=== chunk $c done $(date +%H:%M:%S)"
} > "$log" 2>&1 || { echo "chunk $c FAILED (see $log)"; exit 1; }
echo "chunk $c done $(date +%H:%M:%S)"
