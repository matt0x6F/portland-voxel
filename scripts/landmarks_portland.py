"""East Portland's stand-in for landmarks.py (Warsaw's hand-built palaces, bridges and facades).

voxelize.py calls the same hooks; here most are no-ops, and the curated part is the green space: every named park,
nature park, garden, urban farm, golf course and natural area in the AOI becomes a label sized by its acreage, and the
biggest get camera presets. East Portland's street-tree canopy is the city's thinnest (~20% against 45-56% west of the
river), but it holds Powell Butte, Johnson Creek, Leach Botanical Garden and Zenger Farm: the map should show both.
"""
import math

import numpy as np
from shapely.geometry import Polygon

from aoi import D, W

MAT_IDS = {"solid": 0, "wall": 1, "glass": 2, "metal": 6, "pkin": 7, "grass": 8, "roof": 12, "concrete": 13,
           "bronze_grid": 15, "sign": 16, "sign_band": 18, "ribbon": 19, "classic": 20, "window": 21, "tenement": 22}
LABELS, EXTRA_LABELS, OVERRIDES = {}, {}, {}
DECK = None   # set by voxelize.py
PIERS = []    # set by voxelize.py

# (tag, value) -> kind shown under the name; first match wins
GREEN = [
    (("leisure", "nature_reserve"), "nature park"),
    (("boundary", "protected_area"), "natural area"),
    (("leisure", "garden"), "garden"),
    (("leisure", "golf_course"), "golf course"),
    (("leisure", "park"), "park"),
    (("landuse", "farmland"), "urban farm"),
    (("landuse", "farmyard"), "urban farm"),
    (("place", "farm"), "urban farm"),
    (("landuse", "recreation_ground"), "recreation ground"),
    (("landuse", "cemetery"), "cemetery"),
    (("natural", "wood"), "woods"),
]
ACRE = 4046.86  # m2
# the presets, in the order the viewer lists them (names as OSM spells them; missing ones are skipped)
FEATURED = ["Powell Butte Nature Park", "Leach Botanical Garden", "Zenger Farm", "Gates Park", "Lents Park"]


def adjust_heights(top, feats, note):
    pass


def historic_core():
    """No pitched-roof old town here: a sliver outside the grid, so cells_of() finds nothing and contains() is False."""
    return Polygon([(-5, -5), (-4, -5), (-4, -4)])


def wall_override(f, pal):
    return None


def roof_override(f, pal):
    return None


def apply_zones(vol, g, feats, pal, bmask, note):
    pass


def apply_stripes(vol, g, bmask, pal, osm, cells_of, note):
    return []  # floods


def jazdow_houses(osm):
    return []


def small_tree(vol, g, r, c, h, leaves, trunk, height=None):
    """A young street tree: a clear trunk to ~40% of its height and a narrow, airy crown (same as Warsaw's)."""
    H = int(height or 7 + h % 3)
    z0 = int(g[r, c])
    tr = max(3, int(H * 0.42))
    vol[z0:z0 + tr, r, c] = trunk
    leaf = leaves[1 + (h >> 8) % 4]
    leaf2 = leaves[(1 + (h >> 8) % 4 + 2) % len(leaves)]
    ch = H - tr  # crown height
    for k in range(ch):
        f = (k + 0.5) / ch
        rad = 0.6 + 1.3 * math.sin(math.pi * min(1.0, f * 1.1))  # spindle, widest just below the middle
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                d = (dr * dr + dc * dc) ** 0.5
                if d > rad:
                    continue
                if d > 0.5 and ((h >> ((k * 5 + dr * 3 + dc + 20) % 28)) & 7) == 0:
                    continue  # airy gaps
                zz, rr, cc = z0 + tr + k, r + dr, c + dc
                if 0 <= rr < vol.shape[1] and 0 <= cc < vol.shape[2] and zz < vol.shape[0] and vol[zz, rr, cc] == 0:
                    vol[zz, rr, cc] = leaf if (dr + dc + k) % 3 else leaf2
    vol[z0 + tr:z0 + H - 1, r, c] = trunk  # leader through the crown


def green_kind(tags):
    for (k, v), kind in GREEN:
        if tags.get(k) == v:
            return kind
    return None


def details(vol, g, top, topi, bmask, feats, pal, C, osm, cells_of, note, leaf_ids=()):
    """Green-space labels and camera presets. Returns (labels, views) in voxelize.py's format."""
    def top_at(r, c):
        r, c = min(max(int(r), 0), D - 1), min(max(int(c), 0), W - 1)
        col = np.nonzero(vol[:, r, c])[0]
        return int(col[-1]) + 1 if len(col) else int(g[r, c])

    best = {}  # name -> (area, kind, geom): OSM often maps a park and its protected_area under one name
    for oid, typ, tags, geom in osm.areas:
        name, kind = tags.get("name"), green_kind(tags)
        if not name or not kind:
            continue
        inside = geom.intersection(Polygon([(0, 0), (W, 0), (W, D), (0, D)]))
        if inside.is_empty or inside.area < 4000:  # ~1 acre: pocket greens stay unlabelled
            continue
        if name not in best or inside.area > best[name][0]:
            best[name] = (inside.area, kind, inside, geom.area)

    labels = []
    for name, (area, kind, geom, full) in best.items():
        p = geom.representative_point()
        x, y = int(p.x), int(p.y)
        acres = full / ACRE
        far = 9000 if full >= 100 * ACRE else 5000 if full >= 10 * ACRE else 1800
        labels.append(dict(name=name, kind=f"{kind} · {acres:,.0f} acres" if acres >= 2 else kind,
                           x=x, y=y, z=top_at(y, x) + 4, far=far))
    # named streams: a label on the longest piece's midpoint (Johnson Creek)
    streams = {}
    for oid, tags, line in osm.lines:
        if tags.get("waterway") in ("stream", "river", "canal") and tags.get("name"):
            if line.length > streams.get(tags["name"], (0,))[0]:
                streams[tags["name"]] = (line.length, line)
    for name, (length, line) in streams.items():
        if length < 300:
            continue
        p = line.interpolate(0.5, normalized=True)
        x, y = int(p.x), int(p.y)
        if 0 <= x < W and 0 <= y < D:
            labels.append(dict(name=name, kind="creek", x=x, y=y, z=top_at(y, x) + 3, far=5000))
    labels.sort(key=lambda l: -l["far"])
    total = sum(v[3] for v in best.values()) / ACRE
    note("green-space labels:", len(labels), f"({total:,.0f} acres of named green space touching the AOI)")

    gc = int(g[D // 2, W // 2])
    views = {"overview": dict(label="Overview", target=[W / 2, gc + 20.0, D / 2],
                              pos=[W / 2 - 0.12 * W, gc + 0.42 * W, D / 2 + 0.62 * W])}
    by_name = {l["name"]: l for l in labels}
    for name in FEATURED:
        l = by_name.get(name)
        if l is None:
            note("featured green space not in the AOI / OSM:", name)
            continue
        rad = math.sqrt(best[name][0])
        t = [l["x"] + 0.5, float(l["z"] - 4), l["y"] + 0.5]
        views[name.lower().replace(" ", "_")] = dict(
            label=name.replace(" Nature Park", "").replace(" Botanical Garden", " Garden"),
            target=t, pos=[t[0] + 0.55 * rad + 40, t[1] + 0.5 * rad + 60, t[2] + 0.8 * rad + 80])
    return labels, views
