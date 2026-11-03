#!/bin/sh
# East Portland data pipeline: fetch (cached in data/raw) -> mosaic -> voxelize -> the viewer's web/public/data.
# AOI=small builds the 1 km debug box (scripts/aoi.py). Warsaw-only steps (flood: the Vistula gauge; phenology:
# DWD weather + Polish iNaturalist places) are skipped: the viewer runs without them.
set -e
cd "$(dirname "$0")/.."
export PYTHONPATH=scripts CITY=portland
mkdir -p data/raw data/debug data/cache web/public/data
uv run python scripts/aoi.py
sh scripts/fetch_osm.sh
uv run python scripts/fetch_lidar.py
uv run python scripts/fetch_ortho.py
uv run python scripts/prep.py
uv run python scripts/voxelize.py
uv run python scripts/terrain.py
uv run python scripts/zones.py
uv run python scripts/windows.py
uv run python scripts/places.py
uv run python scripts/climate.py
