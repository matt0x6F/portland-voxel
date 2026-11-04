#!/bin/sh
# Citywide build: Portland in 4096 m chunks (scripts/city_grid.py), each built by scripts/build_chunk.sh into the one
# tile pyramid in web/public/data/tiles, JOBS at a time (default 2: a chunk peaks at ~8-9 GB, more in the West Hills,
# on a 36 GB machine), then merged (scripts/merge_chunks.py). Chunks share one palette, merged under a lock at export.
# A chunk with a part.json is skipped unless REBUILD=1; a failed chunk does not stop the others.
#
# usage: sh scripts/build_city.sh                    every chunk with any city-limits land
#        CHUNKS="5,4 6,4" sh scripts/build_city.sh   just these
#        JOBS=3 sh scripts/build_city.sh             three at a time
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH=scripts CITY=portland
JOBS=${JOBS:-2}
mkdir -p data/raw data/debug data/cache data/chunks web/public/data
[ -s data/raw/city_grid.json ] || uv run --with osmium python scripts/city_grid.py
if [ -z "$CHUNKS" ]; then
  CHUNKS=$(uv run python -c "
import json
g = json.load(open('data/raw/city_grid.json'))
print(' '.join(f\"{c['cx']},{c['cy']}\" for c in g['chunks'] if c['land'] > 0))")
fi
# the Oregon extract: refreshed once here, not by chunks racing each other
P=data/raw/geofabrik/oregon-latest.osm.pbf
mkdir -p data/raw/geofabrik
if [ ! -s $P ] || [ -n "$(find $P -mtime +1)" ]; then
  curl -sS -L -o $P.part https://download.geofabrik.de/north-america/us/oregon-latest.osm.pbf && mv $P.part $P
fi
TODO=""
for c in $CHUNKS; do
  name="c${c%,*}_${c#*,}"
  if [ -s "data/chunks/$name/part.json" ] && [ -z "$REBUILD" ]; then echo "chunk $c: built, skipping"; else TODO="$TODO $c"; fi
done
n=$(echo $TODO | wc -w | tr -d ' ')
echo "building $n chunks, $JOBS at a time: $TODO"
status=0
printf '%s\n' $TODO | xargs -P "$JOBS" -I{} sh scripts/build_chunk.sh {} || status=$?
uv run python scripts/merge_chunks.py
uv run python scripts/climate.py
[ $status -eq 0 ] || { echo "some chunks failed: see data/debug/build_c*.log"; exit $status; }
