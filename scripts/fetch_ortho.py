"""Oregon Statewide Imagery Program 2022 orthophoto (1 ft, 4-band, Jun-Sep 2022) over the AOI -> RGB GeoTIFF tiles.

ArcGIS ImageServer exportImage, reprojected server-side to EPSG:32610 at 0.3 m, in tiles under the service's
15000 x 4100 px limit. Each tile is georeferenced from the extent the server reports (f=json), not the one asked for.
Output: data/raw/orto_<AOI>_r<row>c<col>.tif, read by prep.py's mosaic_rgb.

usage: PYTHONPATH=scripts uv run python scripts/fetch_ortho.py      (AOI=small for the debug box)
"""
import io
import math
import time
from pathlib import Path

import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.transform import from_bounds

from aoi import EPSG, NAME, RAW, world_bounds

SERVICE = "https://imagery.oregonexplorer.info/arcgis/rest/services/OSIP_2022/OSIP_2022_WM/ImageServer/exportImage"
RES = 0.3
TILE = 4000  # px, under maxImageHeight 4100
UA = {"User-Agent": "portland-voxel/0.1"}


def fetch(bbox, w, h):
    params = dict(bbox=",".join(f"{v:.2f}" for v in bbox), bboxSR=EPSG, imageSR=EPSG, size=f"{w},{h}",
                  format="jpg", compressionQuality=92, bandIds="0,1,2", interpolation="RSP_BilinearInterpolation",
                  f="json")
    for k in range(5):
        try:
            meta = requests.get(SERVICE, params=params, headers=UA, timeout=300).json()
            if "href" not in meta:
                raise RuntimeError(meta)
            img = requests.get(meta["href"], headers=UA, timeout=300)
            img.raise_for_status()
            return meta, np.asarray(Image.open(io.BytesIO(img.content)).convert("RGB"))
        except (requests.RequestException, RuntimeError, ValueError) as e:
            if k == 4:
                raise
            print("retry", e)
            time.sleep(10 * (k + 1))


def main():
    x0, y0, x1, y1 = world_bounds(margin=15)
    step = TILE * RES
    nc, nr = math.ceil((x1 - x0) / step), math.ceil((y1 - y0) / step)
    print(f"AOI {NAME}: {nr} x {nc} ortho tiles of {TILE} px at {RES} m")
    for r in range(nr):
        for c in range(nc):
            out = Path(RAW) / f"orto_{NAME}_r{r}c{c}.tif"
            if out.exists() and out.stat().st_size > 0:
                continue
            tx0, ty1 = x0 + c * step, y1 - r * step
            tx1, ty0 = min(tx0 + step, x1), max(ty1 - step, y0)
            w, h = round((tx1 - tx0) / RES), round((ty1 - ty0) / RES)
            meta, rgb = fetch((tx0, ty0, tx1, ty1), w, h)
            e = meta["extent"]
            tr = from_bounds(e["xmin"], e["ymin"], e["xmax"], e["ymax"], rgb.shape[1], rgb.shape[0])
            with rasterio.open(out.with_suffix(".part"), "w", driver="GTiff", height=rgb.shape[0], width=rgb.shape[1],
                               count=3, dtype="uint8", crs=f"EPSG:{EPSG}", transform=tr, compress="jpeg",
                               jpeg_quality=92, photometric="ycbcr", tiled=True) as dst:
                dst.write(np.moveaxis(rgb, -1, 0))
            out.with_suffix(".part").rename(out)
            print(f"  r{r}c{c} {rgb.shape[1]}x{rgb.shape[0]} px, mean RGB {rgb.reshape(-1, 3).mean(0).round()}")
            time.sleep(1)  # polite to the state's server


if __name__ == "__main__":
    main()
