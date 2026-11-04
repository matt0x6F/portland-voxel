"""TriMet on its timetable: every MAX train, streetcar, bus and the aerial tram that runs in the built city, for the
viewer to move by the clock (web/src/transit.ts). No live feed: the published schedule (GTFS static) says where each
vehicle is at any minute, and each block -- one vehicle's day, its trips back to back -- is one vehicle, so a train
reaching its terminus waits out its layover and leaves again the other way.

Input: data/raw/gtfs/trimet.zip (https://developer.trimet.org/schedule/gtfs.zip, no key), the Oregon OSM extract
(bridges and tunnels), the built chunks' ground (scripts/cityground.py) and lidar DSM (bridge decks).

Three representative days, the next Tuesday, Saturday and Sunday the feed covers without a holiday exception (the
viewer picks by Portland's weekday). Shapes are drawn on the ground; along an OSM bridge of the same kind (road or rail)
and running the same way, on the deck (from the lidar surface: see deck()); along a tunnel,
hidden, as outside the built city.

Output web/public/data/transit/
  net.json   {routes: [[name, long name, kind, colour]], shapes: [[x, y, z, s, dx, dy, dz, ds, ...]]}
             city decimetres, deltas after the first vertex; s = metres x 10 along the route's true shape (the
             vertices are simplified, the distances are not); z = HIDDEN where nothing is drawn
  <day>.json {date, trips: [[route, shape, block, t0, s0, dwell0, dt1, ds1, dwell1, ...]]}: seconds after the service
             day's midnight (past 24 h after midnight), s in decimetres; dt from the previous stop's departure

usage: PYTHONPATH=scripts uv run --with osmium --with pandas python scripts/transit.py
"""
import datetime as dt
import json
import math
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import osmium
import pandas as pd
import rasterio
from pyproj import Transformer
from scipy import ndimage
from shapely import STRtree
from shapely.geometry import LineString, Point

from cityground import GRID, RAW, CityGround, city_to_utm, simplify3, utm_to_city

ZIP = RAW / "gtfs/trimet.zip"
PBF = RAW / "geofabrik/oregon-latest.osm.pbf"
OUT = Path("web/public/data/transit")
FT = 0.3048
STEP = 4.0      # m: shapes sampled this finely for heights, bridges and tunnels
HIDDEN = -9999  # dm


def read(name, **kw):
    with zipfile.ZipFile(ZIP) as z:
        return pd.read_csv(z.open(f"{name}.txt"), dtype=str, keep_default_na=False, **kw)


# ---------------------------------------------------------------- service days
cal, cd = read("calendar"), read("calendar_dates")
WD = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def services_on(d):
    ds = d.strftime("%Y%m%d")
    on = set(cal[(cal[WD[d.weekday()]] == "1") & (cal.start_date <= ds) & (cal.end_date >= ds)].service_id)
    on |= set(cd[(cd.date == ds) & (cd.exception_type == "1")].service_id)
    return on - set(cd[(cd.date == ds) & (cd.exception_type == "2")].service_id)


DAYS = {}
for name, wd in (("weekday", 1), ("saturday", 5), ("sunday", 6)):
    d = dt.date.today()
    while d.weekday() != wd or d.strftime("%Y%m%d") in set(cd.date):
        d += dt.timedelta(1)
    DAYS[name] = (d, services_on(d))
    print(f"{name}: {d} ({len(DAYS[name][1])} services)")

# ---------------------------------------------------------------- routes and vehicles
routes = read("routes")


def kind_of(r):
    if r.route_type == "6":
        return "aerial"
    if r.agency_id == "PSC":
        return "streetcar"
    if r.route_type == "0":
        return "max"
    if r.route_type == "2":
        return "wes"
    return "bus60" if r.route_short_name.startswith("FX") else "bus"


R = {r.route_id: i for i, r in enumerate(routes.itertuples())}
route_rows = [[r.route_short_name or r.route_long_name.replace("Portland Streetcar - ", ""), r.route_long_name,
               kind_of(r), "#" + (r.route_color or "1359AE")] for r in routes.itertuples()]
KIND = {r.route_id: kind_of(r) for r in routes.itertuples()}

trips = read("trips", usecols=["route_id", "service_id", "trip_id", "block_id", "shape_id"])
wanted = set().union(*(s for _, s in DAYS.values()))
trips = trips[trips.service_id.isin(wanted) & trips.route_id.isin(R)]
shape_kind = {}
for r, sh in zip(trips.route_id, trips.shape_id):
    shape_kind.setdefault(sh, KIND[r])
print(f"{len(trips)} trips over the three days, {len(shape_kind)} shapes")

# ---------------------------------------------------------------- bridges and tunnels
lon0, lat0, lon1, lat1 = math.inf, math.inf, -math.inf, -math.inf
_ll = Transformer.from_crs(GRID["epsg"], 4326, always_xy=True)
for i, j in ((0, 0), (GRID["W"], 0), (0, GRID["D"]), (GRID["W"], GRID["D"])):
    lo, la = _ll.transform(*city_to_utm(i, j))
    lon0, lat0, lon1, lat1 = min(lon0, lo), min(lat0, la), max(lon1, lo), max(lat1, la)
_utm = Transformer.from_crs(4326, GRID["epsg"], always_xy=True)
structs = defaultdict(list)  # (road|rail, bridge|tunnel) -> lines in city metres
RAILS = ("light_rail", "tram", "rail", "subway", "narrow_gauge")


class Structures(osmium.SimpleHandler):
    def way(self, w):
        t = w.tags
        mode = "road" if "highway" in t else "rail" if t.get("railway") in RAILS else None
        if mode is None:
            return
        what = "bridge" if t.get("bridge", "no") != "no" else \
            "tunnel" if t.get("tunnel", "no") not in ("no", "building_passage") else None
        if what is None:
            return
        pts = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(pts) < 2 or not any(lon0 <= x <= lon1 and lat0 <= y <= lat1 for x, y in pts):
            return
        x, y = _utm.transform(*zip(*pts))
        structs[(mode, what)].append(LineString(np.column_stack(utm_to_city(np.array(x), np.array(y)))))


Structures().apply_file(str(PBF), locations=True, idx="flex_mem")
trees = {k: STRtree(v) for k, v in structs.items()}
print("structures:", {f"{a} {b}": len(v) for (a, b), v in structs.items()})


def along(mode, what, xy, tan):
    """Samples on a structure of this mode that runs their way (within 8 m, within ~25 degrees)."""
    out = np.zeros(len(xy), bool)
    tree = trees.get((mode, what))
    if tree is None or not len(xy):
        return out
    pts = [Point(p) for p in xy]
    (ia, ib), _ = tree.query_nearest(pts, max_distance=8, return_distance=True)
    lines = structs[(mode, what)]
    for a, b in zip(ia, ib):
        ln, s = lines[b], lines[b].project(pts[a])
        p0, p1 = ln.interpolate(max(0, s - 2)), ln.interpolate(min(ln.length, s + 2))
        d = np.array([p1.x - p0.x, p1.y - p0.y])
        n = np.linalg.norm(d)
        if n > 0 and abs(d @ tan[a]) / n > 0.9:
            out[a] = True
    return out


# ---------------------------------------------------------------- decks from the lidar surface
dsm_cache = {}


def dsm_of(key):
    if key not in dsm_cache:
        f = RAW / f"dsm_c{key[0]}_{key[1]}.tif"
        dsm_cache[key] = rasterio.open(f) if f.exists() else None
    return dsm_cache[key]


def deck(xy, tan, ground):
    """The deck under a run of bridge samples, from the lidar surface: its low end across the way (the 10th percentile
    over +-3 m: the road between the cars, railings and truss members), then an opening along it (a running minimum
    then maximum over 84 m) -- what stands above the deck for less than that, a through truss or a lift span's towers,
    is cut away while a ramp or a crown passes unchanged -- and a 20 m mean; never below the ground."""
    nrm = np.column_stack([-tan[:, 1], tan[:, 0]])
    off = np.array([(a, b) for a in np.arange(-3.0, 3.1, 0.5) for b in (-1.0, 0.0, 1.0)])
    z = np.full(len(xy), np.nan)
    for i, (p, t, n) in enumerate(zip(xy, tan, nrm)):
        q = p + off[:, :1] * n + off[:, 1:] * t
        src = dsm_of((int(p[0] // GRID["chunk"]), int(p[1] // GRID["chunk"])))
        if src is None:
            continue
        X, Y = city_to_utm(q[:, 0], q[:, 1])
        v = np.array([s[0] for s in src.sample(zip(X, Y))], np.float64)
        v = v[v > -1000]
        if len(v):
            z[i] = np.percentile(v, 10) + 8  # metres a.s.l. -> city voxels (CITY_BASE -8)
    if not np.isfinite(z).any():
        return ground
    z = np.where(np.isfinite(z), z, ground)
    z = ndimage.grey_opening(z, size=21, mode="nearest")
    z = ndimage.uniform_filter1d(z, 5, mode="nearest")
    return np.maximum(z, ground)


# ---------------------------------------------------------------- shapes
gz = CityGround()
shapes = read("shapes")
shapes = shapes[shapes.shape_id.isin(shape_kind)]
shapes = shapes.assign(seq=shapes.shape_pt_sequence.astype(int)).sort_values(["shape_id", "seq"])
S_IDX, shape_rows, dense = {}, [], {}
n_bridge = n_tunnel = 0
for sid, g in shapes.groupby("shape_id", sort=False):
    x, y = _utm.transform(g.shape_pt_lon.astype(float).values, g.shape_pt_lat.astype(float).values)
    xy = np.column_stack(utm_to_city(x, y))
    m = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    feet = g.shape_dist_traveled.astype(float).values
    s = np.arange(0, m[-1], STEP)
    if len(s) < 2:
        continue
    p = np.column_stack([np.interp(s, m, xy[:, 0]), np.interp(s, m, xy[:, 1])])
    z = gz.at(p[:, 0], p[:, 1]).astype(np.float64)
    vis = np.isfinite(z)
    if not vis.any():
        continue  # never in the built city
    tan = np.gradient(p, axis=0)
    tan /= np.maximum(np.linalg.norm(tan, axis=1, keepdims=True), 1e-9)
    kind = shape_kind[sid]
    if kind == "aerial":  # on its cable: from station to station, clear of the hillside under it
        ends = np.nonzero(vis)[0][[0, -1]]
        line = np.interp(np.arange(len(z)), ends, z[ends] + 6)
        z = np.where(vis, np.maximum(line, z + 6), z)
    else:
        mode = "road" if kind.startswith("bus") else "rail"
        # on a bridge: runs a few samples apart are one bridge (the match flickers along a ramp)
        on = ndimage.binary_closing(along(mode, "bridge", p, tan), np.ones(6, bool)) & vis
        for run in np.split(np.arange(len(z)), np.nonzero(np.diff(on.astype(int)))[0] + 1):
            if on[run[0]]:
                z[run] = deck(p[run], tan[run], z[run])
                n_bridge += len(run)
        tun = along(mode, "tunnel", p, tan) & vis
        z[tun] = np.nan
        n_tunnel += int(tun.sum())
    vis = np.isfinite(z)
    dense[sid] = (s, vis, m, feet, p)
    # simplified: drawn stretches in 3D (s riding along), hidden ones by their ends
    rows = np.column_stack([p, np.where(vis, z, 0), s])
    keep = []
    for run in np.split(np.arange(len(z)), np.nonzero(np.diff(vis.astype(int)))[0] + 1):
        if vis[run[0]]:
            keep.append(simplify3(rows[run], tol=0.4))
        else:
            r = rows[[run[0], run[-1]]].copy()
            r[:, 2] = HIDDEN / 10
            keep.append(r)
    v = np.round(np.concatenate(keep) * 10).astype(np.int64)
    flat = np.concatenate([v[:1], np.diff(v, axis=0)]).ravel()
    S_IDX[sid] = len(shape_rows)
    shape_rows.append(flat.tolist())
print(f"{len(shape_rows)} shapes in the city; {n_bridge * STEP / 1000:.1f} km on bridges, "
      f"{n_tunnel * STEP / 1000:.1f} km in tunnels (route km, all shapes)")

# ---------------------------------------------------------------- timetables
trips = trips[trips.shape_id.isin(S_IDX)]
st = read("stop_times", usecols=["trip_id", "arrival_time", "departure_time", "stop_sequence", "shape_dist_traveled"])
st = st[st.trip_id.isin(set(trips.trip_id))]
st = st.assign(seq=st.stop_sequence.astype(int)).sort_values(["trip_id", "seq"])


def secs(col):
    hms = col.str.split(":", expand=True).astype(int)
    return (hms[0] * 3600 + hms[1] * 60 + hms[2]).values


st = st.assign(arr=secs(st.arrival_time), dep=secs(st.departure_time), ft=st.shape_dist_traveled.astype(float))
by_trip = {tid: g for tid, g in st.groupby("trip_id", sort=False)}
OUT.mkdir(parents=True, exist_ok=True)
for day, (date, svc) in DAYS.items():
    rows, blocks = [], {}
    for t in trips[trips.service_id.isin(svc)].itertuples():
        g = by_trip.get(t.trip_id)
        if g is None or len(g) < 2:
            continue
        s, vis, m, feet, _ = dense[t.shape_id]
        sm = np.interp(g.ft.values * 1.0, feet, m)  # the stop's distance, feet -> metres along our projection
        # the stretch between the stops on either side of the city: segments that cross any drawn sample
        cv = np.concatenate([[0], np.cumsum(vis)])
        ia, ib = np.searchsorted(s, sm[:-1]), np.searchsorted(s, sm[1:], side="right")
        seg = (cv[ib] - cv[ia]) > 0
        if not seg.any():
            continue
        k0, k1 = int(np.argmax(seg)), len(seg) - int(np.argmax(seg[::-1]))  # stops k0..k1
        arr, dep, sd = g.arr.values[k0:k1 + 1], g.dep.values[k0:k1 + 1], np.round(sm[k0:k1 + 1] * 10).astype(int)
        flat = [int(arr[0]), int(sd[0]), int(dep[0] - arr[0])]
        for i in range(1, len(arr)):
            flat += [int(arr[i] - dep[i - 1]), int(sd[i] - sd[i - 1]), int(dep[i] - arr[i])]
        b = blocks.setdefault(t.block_id or f"trip {t.trip_id}", len(blocks))
        rows.append([R[t.route_id], S_IDX[t.shape_id], b, *flat])
    f = OUT / f"{day}.json"
    f.write_text(json.dumps({"date": str(date), "trips": rows}, separators=(",", ":")))
    print(f"{day} {date}: {len(rows)} trips, {len(blocks)} vehicles, {f.stat().st_size / 2 ** 20:.1f} MB")
(OUT / "net.json").write_text(json.dumps({"routes": route_rows, "shapes": shape_rows}, separators=(",", ":")))
print(f"net: {len(route_rows)} routes, {len(shape_rows)} shapes, {(OUT / 'net.json').stat().st_size / 2 ** 20:.1f} MB")
