import * as THREE from 'three';
import type { TileNode, TileStreamer } from './stream';
import type { Meta } from './types';
import { makeVoxelMaterial, type VoxelUniforms } from './voxelMaterial';

// The citywide map (scripts/merge_chunks.py: meta.chunks), streamed by distance. city.ts loads every 1 m tile and
// keeps it, which is ~1,000 tiles for one district and ~45,000 for Portland; here a quadtree over the tile levels
// (8, 4, 2, 1 m) is refined only where a coarser cell would span more than ERR pixels on screen, in view, over a built
// chunk. Four children replace their parent only once all four are in (no holes while they load); zooming out, the
// children cover until the parent is back. Level-1 nodes bring their foliage tiles (trees and grass). Nodes not on
// screen are freed oldest-first beyond BUDGET.
const BUDGET = 1400;
const KEEP_NEAR = 300; // metres: 1 m nodes this close to the focus keep their columns (ground height, label rays)

type Key = [number, number, number];

export function installLod({ streamer: S, meta, camera, uniforms, focus, qs }: {
  streamer: TileStreamer; meta: Meta; camera: THREE.PerspectiveCamera; uniforms: VoxelUniforms;
  focus: () => THREE.Vector3; qs: URLSearchParams;
}) {
  const ERR = +(qs.get('err') ?? 3);
  const N = S.N, levels = S.levels, top = levels[0], fine = levels[levels.length - 1];
  const chunks = meta.chunks ?? [[0, 0, meta.W, meta.D]];
  const folMat = makeVoxelMaterial(uniforms);
  folMat.polygonOffset = true; folMat.polygonOffsetFactor = -1; folMat.polygonOffsetUnits = -2; // grass over bare earth

  const span = (s: number) => N * s;
  const built = (s: number, tx: number, ty: number) => {
    const L = span(s), x0 = tx * L, y0 = ty * L;
    return chunks.some(([x, y, w, h]) => x0 < x + w && x0 + L > x && y0 < y + h && y0 + L > y);
  };
  const roots: Key[] = [];
  const d = S.dims[top];
  for (let ty = 0; ty < d.ny; ty++) for (let tx = 0; tx < d.nx; tx++) if (built(top, tx, ty)) roots.push([top, tx, ty]);
  const kids = (s: number, tx: number, ty: number): Key[] => {
    const h = s / 2;
    return [[h, 2 * tx, 2 * ty], [h, 2 * tx + 1, 2 * ty], [h, 2 * tx, 2 * ty + 1], [h, 2 * tx + 1, 2 * ty + 1]]
      .filter(([q, x, y]) => built(q, x, y)) as Key[];
  };

  const box = new THREE.Box3(), frustum = new THREE.Frustum(), pm = new THREE.Matrix4(), cam = new THREE.Vector3();
  const nodeBox = (s: number, tx: number, ty: number) => {
    const L = span(s);
    return box.set(new THREE.Vector3(tx * L, 0, ty * L), new THREE.Vector3((tx + 1) * L, meta.H, (ty + 1) * L));
  };
  // pixels one cell of this level spans at the node's nearest point
  let pxPerM = 1;
  const cellPx = (s: number, tx: number, ty: number) => s * pxPerM / Math.max(1, nodeBox(s, tx, ty).distanceToPoint(cam));

  const get = (s: number, tx: number, ty: number) => S.nodes.get(S.key(s, tx, ty));
  const isReady = (n?: TileNode) => !!n && n.state === 'ready';
  const shown = new Set<TileNode>();
  const wanted = new Set<string>();
  let now = 0;

  const want = (s: number, tx: number, ty: number, prio: number) => {
    const key = S.key(s, tx, ty);
    wanted.add(key);
    const f = focus(), L = span(s);
    const keep = s === fine && Math.hypot((tx + 0.5) * L - f.x, (ty + 0.5) * L - f.z) < KEEP_NEAR + L;
    S.request(s, tx, ty, prio, keep);
    const n = get(s, tx, ty);
    if (n) n.used = now;
    return n;
  };
  const prioOf = (s: number, tx: number, ty: number) => (s === top ? 1e6 : 0) + 1e5 * Math.log2(s) - nodeBox(s, tx, ty).distanceToPoint(cam);

  // show what is ready over this node without requesting anything: itself, else all of its children's cover
  const cover = (s: number, tx: number, ty: number, out: TileNode[]): boolean => {
    const n = get(s, tx, ty);
    if (isReady(n)) { out.push(n!); return true; }
    if (s === fine) return false;
    const mark = out.length;
    if (kids(s, tx, ty).every(([q, x, y]) => cover(q, x, y, out))) return true;
    out.length = mark;
    return false;
  };

  const visit = (s: number, tx: number, ty: number, out: TileNode[]) => {
    const inView = frustum.intersectsBox(nodeBox(s, tx, ty));
    if (s > fine && inView && cellPx(s, tx, ty) > ERR) {
      const ks = kids(s, tx, ty);
      const ns = ks.map(([q, x, y]) => want(q, x, y, prioOf(q, x, y)));
      if (ns.every(isReady)) { for (const [q, x, y] of ks) visit(q, x, y, out); return; }
    }
    const n = want(s, tx, ty, prioOf(s, tx, ty) + (inView ? 0 : -5e4));
    if (isReady(n)) out.push(n!);
    else cover(s, tx, ty, out);
  };

  let last = 0, rootsIn = 0;
  S.update = () => {
    if (performance.now() - last < 100) return;
    last = now = performance.now();
    S.retire();
    camera.updateMatrixWorld();
    cam.copy(camera.position);
    frustum.setFromProjectionMatrix(pm.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse));
    pxPerM = innerHeight / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
    wanted.clear();
    const out: TileNode[] = [];
    for (const [s, tx, ty] of roots) visit(s, tx, ty, out);
    // level-1 nodes bring their trees and grass
    const fol: TileNode[] = [];
    for (const n of out) if (n.s === 1) {
      const key = `f/${n.tx}_${n.ty}`;
      wanted.add(key);
      S.requestFoliage(n.tx, n.ty, prioOf(1, n.tx, n.ty) - 1);
      const f = S.nodes.get(key);
      if (f) { f.used = now; if (isReady(f)) fol.push(f); }
    }
    const next = new Set([...out, ...fol]);
    for (const n of shown) if (!next.has(n) && n.mesh) n.mesh.visible = false;
    for (const n of next) if (n.mesh) {
      n.mesh.visible = true;
      if (n.foliage) n.mesh.material = folMat;
    }
    shown.clear();
    for (const n of next) shown.add(n);
    rootsIn = roots.filter(([s, x, y]) => isReady(get(s, x, y))).length;
    // forget queued work the camera moved away from, then free the oldest unused nodes over the budget
    S.dropQueued((n) => wanted.has(n.key));
    if (S.nodes.size > BUDGET) {
      const old = [...S.nodes.values()].filter((n) => n.state === 'ready' && !shown.has(n) && !wanted.has(n.key) && n.pinned < now)
        .sort((a, b) => a.used - b.used);
      for (const n of old.slice(0, S.nodes.size - BUDGET)) S.free(n);
    }
    S.pump();
  };

  return {
    gate: () => true,
    get complete() { return rootsIn === roots.length; },
  };
}
