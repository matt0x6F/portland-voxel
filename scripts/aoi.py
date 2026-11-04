"""Area of interest shared by all pipeline steps: East Portland, from I-205 east along Johnson Creek to Powell Butte.

The box is given in lon/lat and everything else is derived from it. World coords are WGS 84 / UTM 10N (EPSG:32610):
the lidar (EPT, EPSG:3857), the Oregon 2022 orthophoto (EPSG:3857) and OSM (EPSG:4326) all share the WGS 84 datum,
so no layer picks up a ~1 m NAD83 shift against another.

East Portland's numbered avenues run true north-south, but UTM grid north is not true north: here, ~0.47 deg east of
zone 10's central meridian, the meridians lean by ~0.34 deg. Over 5.7 km that is ~33 m, so the grid is rotated by the
meridian convergence (THETA, computed below) to put the avenues on voxel faces instead of staircases.

Grid coords: (i, j) continuous, i = column (east), j = row (growing south); cell (c, r) spans [c, c+1) x [r, r+1).
AOI=small selects a 1 km debug box (Gates Park to the west slope of Powell Butte).

CHUNK=cx,cy instead selects one chunk of the citywide grid (scripts/city_grid.py -> data/raw/city_grid.json): the
grid is the city's (its rotation, its origin), and W x D is the chunk's core plus MARGIN on each side, so every
script works in the chunk's own coordinates unchanged. CHUNK_INFO says where that window sits in the city grid.
"""
import json
import math
import os

import numpy as np
from pyproj import Transformer

EPSG = 32610
RAW = "data/raw"
CHUNK_INFO = None  # set in chunk mode: dict(cx, cy, off_i, off_j, core=(r0, c0, rows, cols), city_W, city_D, ...)

# lon0, lat0, lon1, lat1
BOXES = {
    # I-205 (Lents, the Green Line) east past Zenger Farm, Leach Botanical Garden and Gates Park to Powell Butte;
    # SE Powell Blvd on the north edge, Johnson Creek and Foster Rd across the south
    "east": (-122.566, 45.470, -122.493, 45.497),
    "small": (-122.524, 45.482, -122.511, 45.491),
    # 2 x 2 km, Gates Park to the butte's summit: the second point of the memory fit (peak RSS vs area)
    "mid": (-122.530, 45.480, -122.504, 45.498),
}
_T = Transformer.from_crs(4326, EPSG, always_xy=True)

if os.environ.get("CHUNK"):
    _cx, _cy = (int(v) for v in os.environ["CHUNK"].split(","))
    _g = json.load(open(f"{RAW}/city_grid.json"))
    _c = next((c for c in _g["chunks"] if (c["cx"], c["cy"]) == (_cx, _cy)), None)
    assert _c is not None, f"chunk {_cx},{_cy} does not touch the city (scripts/city_grid.py)"
    M, S = _g["margin"], _g["chunk"]
    NAME = f"c{_cx}_{_cy}"
    CX, CY, THETA = _g["cx"], _g["cy"], _g["theta"]
    _off_i, _off_j = _cx * S - M, _cy * S - M  # the window's corner in city grid metres
    W, D = _c["w"] + 2 * M, _c["d"] + 2 * M
    I0, J0 = _g["I0"] - _off_i, _g["J0"] - _off_j  # the city grid's anchor in window coordinates
    CHUNK_INFO = dict(cx=_cx, cy=_cy, off_i=_off_i, off_j=_off_j, core=(M, M, _c["d"], _c["w"]), city_W=_g["W"],
                      city_D=_g["D"], chunk=S, margin=M)
else:
    NAME = os.environ.get("AOI", "east")
    LON0, LAT0, LON1, LAT1 = BOXES[NAME]
    _lonc, _latc = (LON0 + LON1) / 2, (LAT0 + LAT1) / 2
    CX, CY = (float(v) for v in _T.transform(_lonc, _latc))
    # true north at the centre, as a unit vector in UTM
    _n0, _n1 = _T.transform(_lonc, _latc - 0.01), _T.transform(_lonc, _latc + 0.01)
    _nx, _ny = _n1[0] - _n0[0], _n1[1] - _n0[1]
    _nl = math.hypot(_nx, _ny)
    # EV (grid +j, south) must be -north: (sin t, -cos t) = (-nx, -ny)
    THETA = math.atan2(-_nx / _nl, _ny / _nl)

    # extent in metres along the rotated axes: the box's width at its centre latitude and height at its centre meridian
    _w0, _w1 = _T.transform(LON0, _latc), _T.transform(LON1, _latc)
    _h0, _h1 = _T.transform(_lonc, LAT0), _T.transform(_lonc, LAT1)
    W = int(round(math.hypot(_w1[0] - _w0[0], _w1[1] - _w0[1])))
    D = int(round(math.hypot(_h1[0] - _h0[0], _h1[1] - _h0[1])))
    I0, J0 = W // 2, D // 2  # grid position of the centre

# per-AOI working files, so chunks can build side by side (scripts/build_city.sh JOBS=n)
OSM_FILE = f"{RAW}/osm_{NAME}.json"
PREP_FILE = f"data/prep_{NAME}.npz"
GROUND_FILE = f"data/ground_{NAME}.npz"
DEBUG_DIR = f"data/debug/{NAME}"

EU = np.array([math.cos(THETA), math.sin(THETA)])   # grid +i in world
EV = np.array([math.sin(THETA), -math.cos(THETA)])  # grid +j in world


def to_grid(x, y):
    dx, dy = np.asarray(x) - CX, np.asarray(y) - CY
    return I0 + dx * EU[0] + dy * EU[1], J0 + dx * EV[0] + dy * EV[1]


def to_world(i, j):
    u, v = np.asarray(i) - I0, np.asarray(j) - J0
    return CX + u * EU[0] + v * EV[0], CY + u * EU[1] + v * EV[1]


def world_bounds(margin=0):
    xs, ys = to_world(np.array([0, W, W, 0]), np.array([0, 0, D, D]))
    return xs.min() - margin, ys.min() - margin, xs.max() + margin, ys.max() + margin


def lonlat_bounds(margin=0):
    """(lon0, lat0, lon1, lat1) of the world bounds plus margin metres."""
    t = Transformer.from_crs(EPSG, 4326, always_xy=True)
    x0, y0, x1, y1 = world_bounds(margin)
    p = [t.transform(x, y) for x in (x0, x1) for y in (y0, y1)]
    return min(q[0] for q in p), min(q[1] for q in p), max(q[0] for q in p), max(q[1] for q in p)


if __name__ == "__main__":
    print(f"AOI {NAME}: {W} x {D} m ({W * D / 1e6:.1f} km2), centre {CX:.1f} {CY:.1f}, theta {math.degrees(THETA):.4f} deg")
