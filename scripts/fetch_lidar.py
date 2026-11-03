"""Portland Metro lidar -> DSM (0.5 m) and DTM (1 m) GeoTIFFs over the AOI, in EPSG:32610.

Sources (Entwine Point Tiles; LIDAR=2014|2019, default 2014):
- 2014: DOGAMI / OLC Metro, NOAA Digital Coast 6377 (s3://noaa-nos-coastal-lidar-pds/entwine/geoid18/6377): 64 G
  points over 1,222 sq mi, ~20 pts/m2, flown Jul-Sep 2014; EPSG:6339 (NAD83(2011) UTM 10N) x/y, NAVD88 z. Covers all
  of Portland.
- 2019: OLC Portland Metro, USGS 3DEP (s3://usgs-lidar-public/OR_OLCMetro_2019): 32 G points, ~8 pulses/m2; EPSG:3857
  x/y. Newer, but it stops around SE 112th-122nd Ave: Gates Park, Powell Butte and most of East Portland are not in it.
EPT is an octree of LAZ nodes; the full-density cloud is the union of every depth, so every node whose footprint meets
the AOI is fetched (cached under data/raw/ept/<source>/).

- DSM: the highest non-noise return per 0.5 m cell (classes 7 and 18 and withheld points dropped), gaps filled from
  the nearest return within 2 m, else from the DTM (water, which returns little).
- DTM: the mean ground (2) / water (9) return per 1 m cell; the holes under buildings filled by normalised
  convolution at growing scales, so a warehouse floor is a smooth membrane, not nearest-neighbour terraces.

usage: PYTHONPATH=scripts uv run python scripts/fetch_lidar.py      (AOI=small for the debug box)
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import laspy
import numpy as np
import rasterio
import requests
from pyproj import Transformer
from rasterio.transform import from_origin
from scipy import ndimage

from aoi import EPSG, NAME, RAW, world_bounds

SOURCES = {
    "2014": ("https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/entwine/geoid18/6377", "6339", "OLC_Metro_2014"),
    "2019": ("https://s3-us-west-2.amazonaws.com/usgs-lidar-public/OR_OLCMetro_2019", "3857", "OR_OLCMetro_2019"),
}
LIDAR = os.environ.get("LIDAR", "2014")
BASE, SRC_EPSG, _dir = SOURCES[LIDAR]
CACHE = Path(RAW) / "ept" / _dir
MARGIN = 20  # metres beyond the AOI, so prep's rotated resample never reads the edge
NOISE = (7, 18)
GROUND = (2, 9)
UA = {"User-Agent": "portland-voxel/0.1"}
S = requests.Session()


def get(rel, binary=True):
    p = CACHE / rel
    if not p.exists() or p.stat().st_size == 0:
        p.parent.mkdir(parents=True, exist_ok=True)
        for k in range(5):
            try:
                r = S.get(f"{BASE}/{rel}", headers=UA, timeout=120)
                r.raise_for_status()
                p.with_suffix(".part").write_bytes(r.content)
                p.with_suffix(".part").rename(p)
                break
            except requests.RequestException as e:
                if k == 4:
                    raise
                print("retry", rel, e)
                time.sleep(5 * (k + 1))
    return p if binary else json.loads(p.read_text())


def nodes_in(ept, box):
    """EPT keys 'D-X-Y-Z' whose XY footprint meets box = (x0, y0, x1, y1), in the source CRS, and hold points."""
    bx0, by0, _, bx1, by1, _ = ept["bounds"]
    span = bx1 - bx0
    qx0, qy0, qx1, qy1 = box

    def meets(key):
        d, x, y, _ = map(int, key.split("-"))
        s = span / 2 ** d
        nx0, ny0 = bx0 + x * s, by0 + y * s
        return nx0 < qx1 and nx0 + s > qx0 and ny0 < qy1 and ny0 + s > qy0

    out, todo, seen = [], ["0-0-0-0"], set()
    while todo:
        h = get(f"ept-hierarchy/{todo.pop()}.json", binary=False)
        for key, n in h.items():
            if key in seen or not meets(key):
                continue
            if n == -1:
                # its subtree lives in its own hierarchy file, which lists this key again with its real count:
                # not marked seen here, or that entry (and the node's own points) would be skipped
                todo.append(key)
                continue
            seen.add(key)
            if n > 0:
                out.append(key)
    return out


def inpaint(z, have, scales=(1, 2, 4, 8, 16, 32, 64, 128)):
    """Fill ~have cells by normalised Gaussian convolution of the known cells, finest scale first."""
    out = np.where(have, z, 0).astype(np.float32)
    filled = have.copy()
    zk = np.where(have, z, 0).astype(np.float64)
    hk = have.astype(np.float64)
    for s in scales:
        if filled.all():
            break
        num = ndimage.gaussian_filter(zk, s, mode="nearest")
        den = ndimage.gaussian_filter(hk, s, mode="nearest")
        m = ~filled & (den > 1e-3)
        out[m] = (num[m] / den[m]).astype(np.float32)
        filled |= m
    if not filled.all():
        out[~filled] = np.float32(z[have].mean())
    return out


def main():
    ept = get("ept.json", binary=False)
    assert ept["srs"]["horizontal"] == SRC_EPSG, ept["srs"]
    x0, y0, x1, y1 = world_bounds(MARGIN)
    x0, y0, x1, y1 = np.floor(x0), np.floor(y0), np.ceil(x1), np.ceil(y1)
    tosrc = Transformer.from_crs(EPSG, int(SRC_EPSG), always_xy=True)
    cx, cy = tosrc.transform([x0, x1, x0, x1], [y0, y0, y1, y1])
    box = (min(cx) - 5, min(cy) - 5, max(cx) + 5, max(cy) + 5)
    keys = nodes_in(ept, box)
    print(f"AOI {NAME}, lidar {LIDAR}: {len(keys)} EPT nodes")

    with ThreadPoolExecutor(12) as ex:
        futs = [ex.submit(get, f"ept-data/{k}.laz") for k in keys]
        for i, f in enumerate(as_completed(futs), 1):
            f.result()
            if i % 200 == 0 or i == len(futs):
                print(f"  fetched {i}/{len(futs)}")

    fromw = Transformer.from_crs(int(SRC_EPSG), EPSG, always_xy=True)
    print("source -> grid transformation:", fromw.description)
    nr5, nc5 = int(round((y1 - y0) / 0.5)), int(round((x1 - x0) / 0.5))
    nr1, nc1 = int(round(y1 - y0)), int(round(x1 - x0))
    dsm = np.full(nr5 * nc5, -np.inf, np.float32)
    gsum = np.zeros(nr1 * nc1, np.float64)
    gcnt = np.zeros(nr1 * nc1, np.uint32)
    n_pts = n_in = 0
    for i, k in enumerate(keys, 1):
        las = laspy.read(CACHE / f"ept-data/{k}.laz")
        n_pts += len(las.points)
        cls = np.asarray(las.classification)
        keep = ~np.isin(cls, NOISE)
        wh = getattr(las, "withheld", None)
        if wh is not None:
            keep &= ~np.asarray(wh, bool)
        x, y = fromw.transform(np.asarray(las.x)[keep], np.asarray(las.y)[keep])
        z = np.asarray(las.z, np.float32)[keep]
        cls = cls[keep]
        inside = (x >= x0) & (x < x1) & (y > y0) & (y <= y1)
        x, y, z, cls = x[inside], y[inside], z[inside], cls[inside]
        n_in += len(z)
        c5 = ((x - x0) / 0.5).astype(np.int64)
        r5 = ((y1 - y) / 0.5).astype(np.int64)
        ok = (c5 < nc5) & (r5 < nr5)
        np.maximum.at(dsm, r5[ok] * nc5 + c5[ok], z[ok])
        gm = np.isin(cls, GROUND)
        gx, gy, gz = x[gm], y[gm], z[gm]
        c1 = (gx - x0).astype(np.int64)
        r1 = (y1 - gy).astype(np.int64)
        ok = (c1 < nc1) & (r1 < nr1)
        idx = r1[ok] * nc1 + c1[ok]
        np.add.at(gsum, idx, gz[ok])  # in place: a full-grid bincount per node would allocate 17 M cells x thousands
        np.add.at(gcnt, idx, 1)
        if i % 200 == 0 or i == len(keys):
            print(f"  rasterised {i}/{len(keys)} nodes, {n_in:,} of {n_pts:,} points in the AOI")

    have_g = gcnt > 0
    if n_in == 0 or not have_g.any():
        raise SystemExit(f"lidar {LIDAR} has no points in AOI {NAME} ({n_pts:,} read): the flight does not cover it")
    dtm = np.zeros(nr1 * nc1, np.float32)
    dtm[have_g] = (gsum[have_g] / gcnt[have_g]).astype(np.float32)
    dtm, have_g = dtm.reshape(nr1, nc1), have_g.reshape(nr1, nc1)
    print(f"DTM: {have_g.mean() * 100:.1f}% of 1 m cells hold ground returns; filling the rest")
    dtm = inpaint(dtm, have_g)

    dsm = dsm.reshape(nr5, nc5)
    have = np.isfinite(dsm)
    print(f"DSM: {have.mean() * 100:.1f}% of 0.5 m cells hold a return")
    dist, (ri, ci) = ndimage.distance_transform_edt(~have, return_indices=True)
    near = dsm[ri, ci]
    del ri, ci
    dtm5 = np.repeat(np.repeat(dtm, 2, 0), 2, 1)[:nr5, :nc5]
    dsm = np.where(have, dsm, np.where(dist <= 4, near, dtm5))  # 4 cells = 2 m
    dsm = np.maximum(dsm, dtm5).astype(np.float32)
    del near, dist

    prof = dict(driver="GTiff", count=1, dtype="float32", crs=f"EPSG:{EPSG}", nodata=-9999, compress="deflate",
                predictor=3, tiled=True, blockxsize=512, blockysize=512)
    for name, arr, res in (("dsm", dsm, 0.5), ("dtm", dtm, 1.0)):
        out = Path(RAW) / f"{name}_{NAME}.tif"
        with rasterio.open(out, "w", height=arr.shape[0], width=arr.shape[1],
                           transform=from_origin(x0, y1, res, res), **prof) as dst:
            dst.write(arr, 1)
        print(name, arr.shape, f"{float(arr.min()):.1f}..{float(arr.max()):.1f} m", out, out.stat().st_size // 2**20, "MB")


if __name__ == "__main__":
    main()
