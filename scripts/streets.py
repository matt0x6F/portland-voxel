"""Street names laid along the streets, as a map prints them: where each name goes, citywide, for the viewer
(web/src/streets.ts draws the letters flat on the road surface, following its curve).

From the Oregon extract (no voxel rebuild): every named road in the built city, merged by name into chains, cut at
bridges and tunnels (a name follows the ground); then labels placed greedily, the bigger roads first: one every
SPACING metres along each chain, on a stretch straight and smooth enough to read, clear of every label already
placed. Each label is the stretch of centreline it sits on, with the ground height along it (scripts/cityground.py),
simplified in 3D.

Output web/public/data/streets.json: {"labels": [[text, class, [x, y, z, ...]], ...]}, class 0 (freeway) .. 5
(living street); x east, y south, z up, decimetres.

usage: PYTHONPATH=scripts uv run --with osmium python scripts/streets.py
"""
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import osmium
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge, unary_union

from cityground import GRID, CityGround, city_to_utm, simplify3, utm_to_city

PBF = Path("data/raw/geofabrik/oregon-latest.osm.pbf")
OUT = Path("web/public/data/streets.json")
CLS = {"motorway": 0, "trunk": 1, "primary": 1, "secondary": 2, "tertiary": 3, "unclassified": 4,
       "residential": 4, "living_street": 5, "pedestrian": 5}
HEIGHT = {0: 10.0, 1: 7.0, 2: 6.0, 3: 5.0, 4: 3.6, 5: 3.0}     # cap height, m: ~60% of the road voxelize paints
SPACING = {0: 1400, 1: 800, 2: 650, 3: 550, 4: 400, 5: 300}  # m between one name's labels along its street
STEP = 2.0                                                    # m: centreline sampling
TURN = math.radians(28)                                       # most a label's stretch may bend, end to end

DIRS = {"Northeast": "NE", "Northwest": "NW", "Southeast": "SE", "Southwest": "SW", "North": "N", "South": "S",
        "East": "E", "West": "W"}
SUFFIX = {"Street": "St", "Avenue": "Ave", "Boulevard": "Blvd", "Drive": "Dr", "Road": "Rd", "Court": "Ct",
          "Place": "Pl", "Lane": "Ln", "Parkway": "Pkwy", "Highway": "Hwy", "Terrace": "Ter", "Circle": "Cir",
          "Square": "Sq", "Freeway": "Fwy", "Expressway": "Expy", "Crescent": "Cres", "Trail": "Trl"}


def short(name):
    """Southeast Martin Luther King Junior Boulevard -> SE Martin Luther King Jr Blvd: Portland's own sign style."""
    w = name.split()
    if len(w) > 1 and w[0] in DIRS:
        w[0] = DIRS[w[0]]
    if len(w) > 1 and w[-1] in SUFFIX:
        w[-1] = SUFFIX[w[-1]]
    return " ".join("Jr" if x == "Junior" else x for x in w)


def shield(ref):
    """'I 84;US 30' -> 'I-84': the first route, as signed."""
    r = ref.split(";")[0].strip()
    return re.sub(r"^I\s*(\d)", r"I-\1", r)


def text_of(t, hw):
    # freeways by their shield (I-84); a trunk road by the name on its signs (SE Powell Blvd, not US 26)
    if hw == "motorway" and t.get("ref"):
        return shield(t["ref"])
    return short(t["name"]) if t.get("name") else shield(t["ref"]) if t.get("ref") else None


def em_width(s):
    """Rough advance of a bold sans in em (the viewer measures the real font); for spacing labels only."""
    w = 0.0
    for ch in s:
        w += 0.28 if ch == " " else 0.72 if ch.isupper() else 0.56 if ch.isalnum() else 0.4
    return w


# ---------------------------------------------------------------- read
lon0, lat0, lon1, lat1 = math.inf, math.inf, -math.inf, -math.inf
_ll = Transformer.from_crs(GRID["epsg"], 4326, always_xy=True)
for i, j in ((0, 0), (GRID["W"], 0), (0, GRID["D"]), (GRID["W"], GRID["D"])):
    lo, la = _ll.transform(*city_to_utm(i, j))
    lon0, lat0, lon1, lat1 = min(lon0, lo), min(lat0, la), max(lon1, lo), max(lat1, la)
ways = []  # (text, class, [(lon, lat)], cut: on a bridge or in a tunnel)


class Roads(osmium.SimpleHandler):
    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in CLS or w.tags.get("area") == "yes":
            return
        t = {k: w.tags.get(k) for k in ("name", "ref", "bridge", "tunnel", "layer")}
        text = text_of(t, hw)
        if not text:
            return
        pts = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(pts) < 2 or not any(lon0 <= x <= lon1 and lat0 <= y <= lat1 for x, y in pts):
            return
        cut = t["bridge"] not in (None, "no") or t["tunnel"] not in (None, "no")
        ways.append((text, CLS[hw], pts, cut))


Roads().apply_file(str(PBF), locations=True, idx="flex_mem")
print(f"{len(ways)} named road ways in the city's box")

_utm = Transformer.from_crs(4326, GRID["epsg"], always_xy=True)
groups = defaultdict(list)  # (text, class) -> lines in city metres: each label sized for the road under it
for text, c, pts, cut in ways:
    if cut:
        continue
    x, y = _utm.transform(*zip(*pts))
    i, j = utm_to_city(np.array(x), np.array(y))
    groups[(text, c)].append(LineString(np.column_stack([i, j])))

# ---------------------------------------------------------------- chains with the ground under them
gz = CityGround()


def chains(lines):
    u = unary_union(lines)
    m = linemerge(u) if isinstance(u, MultiLineString) else u  # one way alone comes back a LineString
    for g in getattr(m, "geoms", [m]):
        if g.length < 30:
            continue
        n = max(2, int(g.length / STEP) + 1)
        s = np.linspace(0, g.length, n)
        xy = np.array([g.interpolate(v).coords[0] for v in s])
        z = gz.at(xy[:, 0], xy[:, 1])
        # outside the built chunks: split there
        ok = np.isfinite(z)
        for run in np.split(np.arange(n), np.nonzero(np.diff(ok.astype(int)))[0] + 1):
            if ok[run[0]] and len(run) >= 15:
                yield np.column_stack([xy[run], z[run]])


def heading(p):
    d = np.diff(p[:, :2], axis=0)
    return np.unwrap(np.arctan2(d[:, 1], d[:, 0]))


# ---------------------------------------------------------------- place
placed, boxes, by_name = [], [], defaultdict(list)
tree = None
order = sorted(groups, key=lambda k: (k[1], -sum(l.length for l in groups[k])))
for n_text, (text, c) in enumerate(order):
    h = HEIGHT[c]
    need = em_width(text) * h / 0.72 * 1.15 + 2 * h  # the text, a little air either end
    half = int(math.ceil(need / 2 / STEP))
    for p in chains(groups[(text, c)]):
        Lc = (len(p) - 1) * STEP
        if Lc < need:
            continue
        k = max(1, int(Lc // SPACING[c]))
        for q in range(k):
            centre = (q + 0.5) * Lc / k
            for shift in (0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5):
                m = int(round((centre + shift * need) / STEP))
                a, b = m - half, m + half
                if a < 0 or b >= len(p):
                    continue
                seg = p[a:b + 1]
                hd = heading(seg)
                if np.ptp(hd) > TURN or np.abs(np.diff(seg[:, 2])).max() > 1.5:
                    continue  # bends too much, or crosses a wall/kerb step
                mid = seg[len(seg) // 2, :2]
                if any(np.hypot(*(mid - o)) < SPACING[c] * 0.6 for o in by_name[text]):
                    continue  # the other carriageway of the same street, or too near its last label
                box = LineString(seg[:, :2]).buffer(h * 0.9, cap_style="flat")
                if tree is not None and any(boxes[i].intersects(box) for i in tree.query(box)):
                    continue
                if any(b_.intersects(box) for b_ in boxes[len(boxes) - (len(boxes) % 256):]):
                    continue  # placed since the tree was last built
                placed.append((text, c, simplify3(seg)))
                boxes.append(box)
                by_name[text].append(mid)
                if len(boxes) % 256 == 0:
                    tree = STRtree(boxes)
                break
    if n_text % 1000 == 0:
        print(f"  {n_text}/{len(order)} names, {len(placed)} labels", flush=True)

labels = [[t, c, [int(round(v * 10)) for row in p for v in row]] for t, c, p in placed]
OUT.write_text(json.dumps({"labels": labels}, ensure_ascii=False, separators=(",", ":")))
per = defaultdict(int)
for _, c, _ in placed:
    per[c] += 1
print(f"{len(placed)} labels for {len(by_name)} streets, by class {dict(sorted(per.items()))}, "
      f"{OUT.stat().st_size / 2 ** 20:.1f} MB -> {OUT}")
