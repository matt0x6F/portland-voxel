"""Ground height anywhere in the built city, in the viewer's units: city metres (x east, y south) -> the voxel height of
the ground's top face (meta y). For overlays laid on the map after the build: street names, transit.

Each chunk's voxelize run saved its ground (data/ground_<name>.npz: g, the first empty voxel above the ground, chunk
z) over the chunk window; city height = g + the chunk's dz. A chunk without that file (the first ones built, before
the per-chunk names) falls back to its lidar DTM (metres a.s.l. - the city datum), which agrees with g to within a
voxel off the roads that voxelize flattened.

usage: from cityground import CityGround, city_to_utm, utm_to_city
       gz = CityGround(); gz.at(xs, ys)
"""
import json
import math
from pathlib import Path

import numpy as np

RAW, CHUNKS = Path("data/raw"), Path("data/chunks")
GRID = json.loads((RAW / "city_grid.json").read_text())
CITY_BASE = -8  # voxelize.CITY_BASE: the height of city voxel z = 0, metres a.s.l.
_th = GRID["theta"]
EU = np.array([math.cos(_th), math.sin(_th)])   # city +x in UTM
EV = np.array([math.sin(_th), -math.cos(_th)])  # city +y in UTM


def utm_to_city(x, y):
    dx, dy = np.asarray(x) - GRID["cx"], np.asarray(y) - GRID["cy"]
    return GRID["I0"] + dx * EU[0] + dy * EU[1], GRID["J0"] + dx * EV[0] + dy * EV[1]


def city_to_utm(i, j):
    u, v = np.asarray(i) - GRID["I0"], np.asarray(j) - GRID["J0"]
    return GRID["cx"] + u * EU[0] + v * EV[0], GRID["cy"] + u * EU[1] + v * EV[1]


class CityGround:
    def __init__(self):
        S, M = GRID["chunk"], GRID["margin"]
        self.S, self.M = S, M
        self.parts = {}
        for p in sorted(CHUNKS.glob("*/part.json")):
            pj = json.loads(p.read_text())
            self.parts[(pj["core"][0] // S, pj["core"][1] // S)] = pj
        self.cache = {}

    def _chunk(self, key):
        if key in self.cache:
            return self.cache[key]
        pj = self.parts.get(key)
        if pj is None:
            self.cache[key] = None
            return None
        f = Path(f"data/ground_{pj['name']}.npz")
        if f.exists():
            g = np.load(f)["g"].astype(np.float32) + pj["dz"]
        else:
            g = self._from_dtm(key, pj)
        self.cache[key] = g
        return g

    def _from_dtm(self, key, pj):
        import rasterio
        n = self.S + 2 * self.M
        ox, oy = key[0] * self.S - self.M, key[1] * self.S - self.M
        jj, ii = np.mgrid[0:n, 0:n].astype(np.float64) + 0.5
        X, Y = city_to_utm(ii + ox, jj + oy)
        with rasterio.open(RAW / f"dtm_{pj['name']}.tif") as src:
            r, c = rasterio.transform.rowcol(src.transform, X.ravel(), Y.ravel())
            a = src.read(1)
        r = np.clip(np.asarray(r), 0, a.shape[0] - 1); c = np.clip(np.asarray(c), 0, a.shape[1] - 1)
        return (np.ceil(a[r, c] - CITY_BASE)).reshape(n, n).astype(np.float32)

    def at(self, xs, ys):
        """Ground top at city points (arrays); NaN outside the built chunks."""
        xs, ys = np.asarray(xs, np.float64), np.asarray(ys, np.float64)
        out = np.full(xs.shape, np.nan, np.float32)
        kx, ky = np.floor(xs / self.S).astype(int), np.floor(ys / self.S).astype(int)
        for key in set(zip(kx.ravel().tolist(), ky.ravel().tolist())):
            g = self._chunk(key)
            if g is None:
                continue
            m = (kx == key[0]) & (ky == key[1])
            i = np.clip((xs[m] - (key[0] * self.S - self.M)).astype(int), 0, g.shape[1] - 1)
            j = np.clip((ys[m] - (key[1] * self.S - self.M)).astype(int), 0, g.shape[0] - 1)
            out[m] = g[j, i]
        return out


if __name__ == "__main__":
    # the DTM fallback against the saved ground, in a chunk that has both
    gz = CityGround()
    key = (2, 2)
    pj = gz.parts[key]
    a = gz._chunk(key)
    b = gz._from_dtm(key, pj)
    d = (a - b)[256:-256:7, 256:-256:7]
    print(f"{pj['name']}: saved ground - DTM fallback: median {np.median(d):.2f}, |d|<=1 in {np.mean(np.abs(d) <= 1) * 100:.1f}%")
