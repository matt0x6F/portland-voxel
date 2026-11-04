"""Join the built chunks (data/chunks/<name>/part.json, places.json) into the citywide viewer meta.

The tiles are already in place: voxelize.py wrote each chunk's nodes into web/public/data/tiles under their city
indices. This writes web/public/data/meta.json (the city grid, the shared palette, every chunk's lamps, labels,
camera presets and trams, and `chunks`: the built rectangles, which tell the viewer to use its streaming LOD engine
and which nodes exist) and web/public/data/places.json (the search index).

usage: PYTHONPATH=scripts uv run python scripts/merge_chunks.py
"""
import json
import math
from pathlib import Path

RAW, CHUNKS, OUT = Path("data/raw"), Path("data/chunks"), Path("web/public/data")
N = 128
LEVELS = (1, 2, 4, 8)


def main():
    grid = json.loads((RAW / "city_grid.json").read_text())
    parts = [json.loads(p.read_text()) for p in sorted(CHUNKS.glob("*/part.json"))]
    assert parts, "no chunks built (scripts/build_city.sh)"
    palette = json.loads((CHUNKS / "palette.json").read_text())
    W, D = grid["W"], grid["D"]

    # labels: one per name (a creek crossing chunks is labelled in each), the most prominent first
    labels, seen = [], set()
    for l in sorted((l for p in parts for l in p["labels"]), key=lambda l: -l.get("far", 0)):
        if l["name"] not in seen:
            seen.add(l["name"])
            labels.append(l)
    views = {}
    for p in parts:
        for k, v in p["views"].items():
            views.setdefault(k, v)
    # overview: the built area from the south-south-west, high enough to take it all in
    x0 = min(p["core"][0] for p in parts); y0 = min(p["core"][1] for p in parts)
    x1 = max(p["core"][0] + p["core"][2] for p in parts); y1 = max(p["core"][1] + p["core"][3] for p in parts)
    span = max(x1 - x0, y1 - y0)
    gz = sorted(p["ground"][1] for p in parts)[len(parts) // 2]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    views = {"overview": dict(label="Overview", target=[cx, gz + 20.0, cy],
                              pos=[cx - 0.12 * span, gz + 0.45 * span, cy + 0.65 * span]), **views}

    places = {}
    for f in sorted(CHUNKS.glob("*/places.json")):
        for r in json.loads(f.read_text())["places"]:
            k = (r[0], r[1])  # a street crossing chunks: keep its longest piece
            if k not in places or r[4] > places[k][4]:
                places[k] = r

    meta = dict(
        W=W, D=D, H=max(p["top"] for p in parts) + 8, base=parts[0]["base"] - parts[0]["dz"],
        tiles=dict(N=N, levels=[dict(s=s, nx=-(-W // (N * s)), ny=-(-D // (N * s))) for s in LEVELS],
                   bytes=sum(p["tiles"]["bytes"] for p in parts)),
        chunks=[p["core"] for p in parts],
        center=[grid["cx"], grid["cy"]], theta_deg=math.degrees(grid["theta"]), crs=f"EPSG:{grid['epsg']}",
        palette=palette, lamps=[l for p in parts for l in p["lamps"]], jets=[j for p in parts for j in p["jets"]],
        floods=[], labels=labels, views=views, trams=[t for p in parts for t in p["trams"]], sources=parts[0]["sources"],
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))
    (OUT / "places.json").write_text(json.dumps({"places": sorted(places.values(), key=lambda r: -r[4])},
                                                ensure_ascii=False, separators=(",", ":")))
    # the single-map layers do not exist for the city (the LOD engine does without them): drop stale ones
    for stale in ("terrain.bin", "zones.bin", "zones.json", "windows.bin", "windows.json", "foliage.bin",
                  "phenology.json", "flood.bin", "flood.json", "qa_roadmask.bin.gz", "trees.json", "traffic.json"):
        (OUT / stale).unlink(missing_ok=True)
    print(f"merged {len(parts)} chunks ({', '.join(p['name'] for p in parts)}): city grid {W} x {D} m, "
          f"{len(labels)} labels, {len(meta['lamps'])} lamps, {len(places)} places, palette {len(palette)}, "
          f"tiles {meta['tiles']['bytes'] / 2 ** 20:.1f} MB")


if __name__ == "__main__":
    main()
