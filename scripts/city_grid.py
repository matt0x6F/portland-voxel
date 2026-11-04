"""The citywide grid: Portland's city limits (OSM) -> one rotated 1 m grid cut into chunks, cached in
data/raw/city_grid.json and read by aoi.py when CHUNK=cx,cy is set.

- Boundary: the admin_level 8 relation named Portland in the Geofabrik Oregon extract (data/raw/geofabrik).
- Grid: WGS 84 / UTM 10N rotated by the meridian convergence at the city's centre (~0.25 deg), so the avenues lie on
  voxel faces to within ~0.1 deg anywhere in the city.
- Chunks: CHUNK m squares (4096 = 4 nodes of the coarsest level, 128 cells x 8 m), so every level's node grid lines up
  across chunks; built with MARGIN m on each side and exported only over their core (tiles.export_chunk).
- The viewer's mesher writes absolute Int16 vertex positions: the grid must stay under 32767 m on each side.

usage: uv run --with osmium python scripts/city_grid.py
"""
import json
import math
from pathlib import Path

import osmium
from pyproj import Transformer
from shapely import wkb
from shapely.geometry import box, mapping
from shapely.ops import transform, unary_union

RAW = Path("data/raw")
PBF = RAW / "geofabrik" / "oregon-latest.osm.pbf"
CHUNK, MARGIN, EPSG = 4096, 256, 32610
BUFFER = 150  # metres of context kept around the city limits (river banks, the edge of Forest Park)


class Boundary(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.polys = []

    def area(self, a):
        t = a.tags
        if t.get("boundary") == "administrative" and t.get("admin_level") == "8" and t.get("name") == "Portland" \
                and not a.from_way():
            self.polys.append(wkb.loads(self.wkb.create_multipolygon(a), hex=True))


def main():
    h = Boundary()
    h.apply_file(str(PBF), locations=True, idx="flex_mem")
    assert h.polys, "no Portland admin_level 8 relation in the extract"
    city_ll = unary_union(h.polys)
    lon0, lat0, lon1, lat1 = city_ll.bounds
    t = Transformer.from_crs(4326, EPSG, always_xy=True)
    lonc, latc = (lon0 + lon1) / 2, (lat0 + lat1) / 2
    n0, n1 = t.transform(lonc, latc - 0.01), t.transform(lonc, latc + 0.01)
    nx, ny = n1[0] - n0[0], n1[1] - n0[1]
    nl = math.hypot(nx, ny)
    theta = math.atan2(-nx / nl, ny / nl)
    eu = (math.cos(theta), math.sin(theta))   # grid +i (east) in world
    ev = (math.sin(theta), -math.cos(theta))  # grid +j (south) in world
    city = transform(lambda x, y: t.transform(x, y), city_ll)
    # the city in grid metres around an arbitrary origin, then shifted so its buffered bounds start at 0
    cx0, cy0 = t.transform(lonc, latc)
    to_ij = lambda x, y: ((x - cx0) * eu[0] + (y - cy0) * eu[1], (x - cx0) * ev[0] + (y - cy0) * ev[1])
    city_ij = transform(lambda x, y: to_ij(x, y), city).buffer(BUFFER)
    i0, j0, i1, j1 = city_ij.bounds
    # sized to the coarsest node (1024 m), not to whole chunks: 28.45 km of city plus its buffer is just over 7 chunks,
    # and an 8th would overflow the mesher's Int16 vertices; the last chunk of a row or column is narrower instead
    NODE = 1024
    GW, GD = math.ceil((i1 - i0) / NODE) * NODE, math.ceil((j1 - j0) / NODE) * NODE
    ncx, ncy = math.ceil(GW / CHUNK), math.ceil(GD / CHUNK)
    # centre the city in the chunk grid
    oi = i0 - (GW - (i1 - i0)) / 2
    oj = j0 - (GD - (j1 - j0)) / 2
    assert GW <= 32767 and GD <= 32767, (GW, GD)
    city_g = transform(lambda i, j: (i - oi, j - oj), city_ij)
    chunks = []
    for cy in range(ncy):
        for cx in range(ncx):
            cw, cd = min(CHUNK, GW - cx * CHUNK), min(CHUNK, GD - cy * CHUNK)  # core size: edge chunks are narrower
            cell = box(cx * CHUNK, cy * CHUNK, cx * CHUNK + cw, cy * CHUNK + cd)
            share = cell.intersection(city_g).area / cell.area
            if share > 0:
                chunks.append(dict(cx=cx, cy=cy, w=cw, d=cd, share=round(share, 3)))
    grid = dict(epsg=EPSG, theta=theta, cx=cx0, cy=cy0, I0=-oi, J0=-oj, W=GW, D=GD, chunk=CHUNK, margin=MARGIN,
                chunks=chunks, city_km2=round(city.area / 1e6, 1))
    (RAW / "city_grid.json").write_text(json.dumps(grid, indent=1))
    (RAW / "city_limits.geojson").write_text(json.dumps(dict(type="Feature", properties={}, geometry=mapping(city_g))))
    print(f"Portland: {grid['city_km2']} km2, grid {GW} x {GD} m ({ncx} x {ncy} chunks of {CHUNK} m), "
          f"{len(chunks)} chunks touch the city, theta {math.degrees(theta):.3f} deg")


if __name__ == "__main__":
    main()
