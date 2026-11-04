import * as THREE from 'three';
import { fetchJson, hash } from './util';

// TriMet on its timetable (scripts/transit.py): every MAX train, streetcar, bus and aerial tram cabin where the
// published schedule puts it, at Portland's time. A block (one vehicle's day, its trips back to back) is one vehicle:
// between two trips it waits at the terminus, or leaves the map if its next trip starts somewhere else. Between stops a
// vehicle pulls away, cruises and brakes in the time the schedule gives it, after a stop's dwell (stations always,
// about two bus stops in three) taken out of that time, so it keeps to the timetable. Positions are a function of the
// clock alone: dragging the time slider moves every vehicle to that minute, and from there it runs on in real time.

type Kind = 'max' | 'streetcar' | 'bus' | 'bus60' | 'wes' | 'aerial';
type Net = { routes: [string, string, Kind, string][]; shapes: number[][] };
type DayData = { date: string; trips: number[][] };
type DayType = 'weekday' | 'saturday' | 'sunday';

interface Shape { x: Float32Array; y: Float32Array; z: Float32Array; s: Float32Array }
interface Trip { route: number; shape: number; arr: Float64Array; dep: Float64Array; s: Float64Array; start: number; end: number; id: number }
interface Block { trips: Trip[]; start: number; end: number; cur: number; kind: Kind }

interface Spec { cars: number; len: number; gap: number; w: number; h: number; body: number; band: number; off: number; dwell: number }
const SPEC: Record<Kind, Spec> = {
  max: { cars: 2, len: 28, gap: 1.2, w: 2.65, h: 3.6, body: 0xe8eae8, band: 0x2a3440, off: 0, dwell: 20 },
  streetcar: { cars: 1, len: 20, gap: 0, w: 2.46, h: 3.4, body: -1, band: 0x3a3e42, off: 0, dwell: 18 },
  bus: { cars: 1, len: 12.2, gap: 0, w: 2.6, h: 3.1, body: 0xf2f2ef, band: 0x1f4e8c, off: 1.8, dwell: 10 },
  bus60: { cars: 2, len: 8.9, gap: 0.6, w: 2.6, h: 3.1, body: 0xf2f2ef, band: 0x1f4e8c, off: 1.8, dwell: 14 },
  wes: { cars: 1, len: 26, gap: 0, w: 3.0, h: 4.2, body: 0xe4e4e2, band: 0x6e6e6e, off: 0, dwell: 30 },
  aerial: { cars: 1, len: 8.5, gap: 0, w: 4.2, h: 3.4, body: 0xd5dadd, band: 0x5d6266, off: 0, dwell: 30 },
};
const MAX_SECTIONS = 1500, REACH = 5000, LAYOVER = 3600, NEAR = 250;
const DAYS: DayType[] = ['sunday', 'weekday', 'weekday', 'weekday', 'weekday', 'weekday', 'saturday'];

function decodeShape(flat: number[]): Shape {
  const n = flat.length / 4, x = new Float32Array(n), y = new Float32Array(n), z = new Float32Array(n), s = new Float32Array(n);
  let a = 0, b = 0, c = 0, d = 0;
  for (let i = 0; i < n; i++) {
    a += flat[i * 4]; b += flat[i * 4 + 1]; c += flat[i * 4 + 2]; d += flat[i * 4 + 3];
    x[i] = a / 10; y[i] = b / 10; z[i] = c / 10; s[i] = d / 10;
  }
  return { x, y, z, s };
}

function decodeDay(data: DayData, routes: Net['routes']): Block[] {
  const blocks = new Map<number, Trip[]>();
  data.trips.forEach((r, id) => {
    const n = (r.length - 6) / 3 + 1, arr = new Float64Array(n), dep = new Float64Array(n), s = new Float64Array(n);
    arr[0] = r[3]; s[0] = r[4] / 10; dep[0] = arr[0] + r[5];
    for (let i = 1; i < n; i++) {
      const k = 6 + (i - 1) * 3;
      arr[i] = dep[i - 1] + r[k]; s[i] = s[i - 1] + r[k + 1] / 10; dep[i] = arr[i] + r[k + 2];
    }
    const t: Trip = { route: r[0], shape: r[1], arr, dep, s, start: arr[0], end: dep[n - 1], id };
    if (!blocks.has(r[2])) blocks.set(r[2], []);
    blocks.get(r[2])!.push(t);
  });
  return [...blocks.values()].map((trips) => {
    trips.sort((a, b) => a.start - b.start);
    return { trips, start: trips[0].start, end: trips[trips.length - 1].end, cur: 0, kind: routes[trips[0].route][2] };
  });
}

// distance travelled after `tau` of a run of `T` seconds over `D` metres: pull away, cruise, brake
function run(tau: number, T: number, D: number) {
  if (T <= 0 || tau >= T) return D;
  if (tau <= 0) return 0;
  const a = Math.min(15, T / 2), v = D / (T - a);
  if (tau < a) return 0.5 * v / a * tau * tau;
  if (tau < T - a) return 0.5 * v * a + v * (tau - a);
  const r = T - tau;
  return D - 0.5 * v / a * r * r;
}

interface Pt { x: number; y: number; z: number; dx: number; dy: number; hidden: boolean }
function sample(sh: Shape, s: number, out: Pt): Pt {
  const S = sh.s, n = S.length;
  let lo = 0, hi = n - 1;
  if (s <= S[0]) hi = 1;
  else if (s >= S[n - 1]) lo = n - 2;
  else while (hi - lo > 1) { const m = (lo + hi) >> 1; if (S[m] <= s) lo = m; else hi = m; }
  const L = S[hi] - S[lo] || 1e-6, f = (s - S[lo]) / L; // beyond either end: carried on along the end segment
  const ex = sh.x[hi] - sh.x[lo], ey = sh.y[hi] - sh.y[lo], el = Math.hypot(ex, ey) || 1;
  out.x = sh.x[lo] + ex * f; out.y = sh.y[lo] + ey * f; out.z = sh.z[lo] + (sh.z[hi] - sh.z[lo]) * Math.min(Math.max(f, 0), 1);
  out.dx = ex / el; out.dy = ey / el;
  out.hidden = sh.z[lo] < -900 || sh.z[hi] < -900;
  return out;
}

export class Transit {
  private readonly shapes: Shape[];
  private readonly routes: Net['routes'];
  private readonly colours: THREE.Color[];
  private readonly days = new Map<DayType, Block[] | 'loading'>();
  private readonly parts: THREE.InstancedMesh[] = [];
  private readonly winMat = new THREE.MeshStandardMaterial({ color: 0x26323d, roughness: 0.15, metalness: 0.4, emissive: new THREE.Color(1.0, 0.85, 0.6), emissiveIntensity: 0 });
  private day = 1;
  private secs = 12 * 3600;
  fine: (x: number, z: number) => boolean = () => true;
  shown = 0;

  constructor(net: Net, scene: THREE.Scene) {
    this.routes = net.routes;
    this.shapes = net.shapes.map(decodeShape);
    this.colours = net.routes.map((r) => new THREE.Color(r[3]));
    const unit = new THREE.BoxGeometry(1, 1, 1).translate(0, 0.5, 0);
    const std = (o: THREE.MeshStandardMaterialParameters) => new THREE.MeshStandardMaterial({ roughness: 0.4, metalness: 0.15, ...o });
    // body, lower band, route stripe (instance colours); windows (lit at night); roof gear
    const mats = [std({ color: 0xffffff }), std({ color: 0xffffff }), std({ color: 0xffffff }), this.winMat, std({ color: 0xcfd2d4, roughness: 0.6 })];
    mats.forEach((m, i) => {
      const mesh = new THREE.InstancedMesh(unit, m, MAX_SECTIONS);
      if (i < 3) mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_SECTIONS * 3), 3);
      mesh.castShadow = i === 0;
      mesh.receiveShadow = i < 3;
      mesh.frustumCulled = false;
      mesh.count = 0;
      scene.add(mesh);
      this.parts.push(mesh);
    });
  }

  /** the clock: day of the year, hour of the day; between calls it runs on in real time */
  setClock(day: number, hour: number) {
    this.day = day;
    this.secs = hour * 3600;
  }

  private blocksFor(type: DayType) {
    const b = this.days.get(type);
    if (b === undefined) {
      this.days.set(type, 'loading');
      fetchJson<DayData>(`data/transit/${type}.json`).then((d) => this.days.set(type, decodeDay(d, this.routes)))
        .catch((e) => { console.warn('transit', type, e); this.days.delete(type); });
      return null;
    }
    return b === 'loading' ? null : b;
  }

  private typeOf(day: number) {
    return DAYS[new Date(new Date().getFullYear(), 0, day).getDay()];
  }

  update(dt: number, camera: THREE.Camera, night: number) {
    this.secs += dt;
    if (this.secs >= 86400) { this.secs -= 86400; this.day++; }
    const m = new THREE.Matrix4(), part = new THREE.Matrix4(), lift = new THREE.Matrix4(), pos = new THREE.Vector3(), q = new THREE.Quaternion(), qp = new THREE.Quaternion();
    const up = new THREE.Vector3(0, 1, 0), side = new THREE.Vector3(1, 0, 0), sc = new THREE.Vector3(), c = new THREE.Color();
    const a: Pt = { x: 0, y: 0, z: 0, dx: 0, dy: 0, hidden: false }, b = { ...a }, cam = camera.position;
    let n = 0;
    // today's service, and yesterday's running past midnight (its times go on past 24 h)
    for (const [day, t] of [[this.day, this.secs], [this.day - 1, this.secs + 86400]] as const) {
      const blocks = this.blocksFor(this.typeOf(day));
      if (!blocks) continue;
      for (const bl of blocks) {
        if (t < bl.start || t > bl.end) continue;
        const pose = this.where(bl, t);
        if (!pose) continue;
        const sh = this.shapes[pose.trip.shape], spec = SPEC[bl.kind];
        sample(sh, pose.s, a);
        if (a.hidden || Math.hypot(a.x - cam.x, a.y - cam.z) > REACH || !this.fine(a.x, a.y)) continue;
        const route = pose.trip.route, col = this.colours[route];
        for (let k = 0; k < spec.cars && n < MAX_SECTIONS; k++) {
          const front = pose.s - k * (spec.len + spec.gap), back = front - spec.len;
          sample(sh, front, a); sample(sh, back, b);
          if (a.hidden || b.hidden) break;
          const hx = a.x - b.x, hy = a.y - b.y, hl = Math.hypot(hx, hy) || 1;
          // centred between its ends, on the right of the way (grid y points south: right of (dx, dy) is (-dy, dx))
          pos.set((a.x + b.x) / 2 - (hy / hl) * spec.off, (a.z + b.z) / 2, (a.y + b.y) / 2 + (hx / hl) * spec.off);
          q.setFromAxisAngle(up, Math.atan2(hx, hy)).multiply(qp.setFromAxisAngle(side, -Math.atan2(a.z - b.z, hl)));
          m.compose(pos, q, sc.set(1, 1, 1));
          const L = spec.len, W = spec.w, H = spec.h;
          const put = (i: number, y0: number, w: number, h: number, l: number) =>
            this.parts[i].setMatrixAt(n, part.makeScale(w, h, l).premultiply(lift.makeTranslation(0, y0, 0)).premultiply(m));
          put(0, 0.35, W, H - 0.65, L);
          put(1, 0.3, W + 0.04, 0.9, L + 0.02);
          put(2, 0.72 * H, W + 0.06, 0.07 * H + 0.05, L - 0.6);
          put(3, 0.42 * H, W + 0.08, 0.27 * H, L - 2);
          put(4, H - 0.3, W * 0.6, 0.35, L * 0.5);
          this.parts[0].setColorAt(n, spec.body < 0 ? col : c.setHex(spec.body));
          this.parts[1].setColorAt(n, c.setHex(spec.band));
          this.parts[2].setColorAt(n, spec.body < 0 ? c.setHex(0xf4f4f2) : col);
          n++;
        }
      }
    }
    for (const p of this.parts) {
      p.count = n;
      p.instanceMatrix.needsUpdate = true;
      if (p.instanceColor) p.instanceColor.needsUpdate = true;
    }
    this.winMat.emissiveIntensity = night * 1.4;
    this.shown = n;
  }

  // where a block's vehicle is at t: on a trip (between stops, or standing at one), at a terminus between two trips
  // that meet there, or nowhere (out of service, or gone to start its next trip elsewhere)
  private where(bl: Block, t: number): { trip: Trip; s: number } | null {
    const T = bl.trips;
    if (bl.cur >= T.length || T[bl.cur].start > t) bl.cur = 0;
    while (bl.cur + 1 < T.length && T[bl.cur + 1].start <= t) bl.cur++;
    const tr = T[bl.cur];
    if (t > tr.end) {
      const nx = T[bl.cur + 1];
      if (!nx || nx.start - tr.end > LAYOVER) return null;
      const a = sample(this.shapes[tr.shape], tr.s[tr.s.length - 1], { x: 0, y: 0, z: 0, dx: 0, dy: 0, hidden: false });
      const b = sample(this.shapes[nx.shape], nx.s[0], { x: 0, y: 0, z: 0, dx: 0, dy: 0, hidden: false });
      return Math.hypot(a.x - b.x, a.y - b.y) < NEAR ? { trip: tr, s: tr.s[tr.s.length - 1] } : null;
    }
    const { arr, dep, s } = tr;
    let lo = 0, hi = arr.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (arr[mid] <= t) lo = mid; else hi = mid; }
    if (t <= dep[lo] || lo === arr.length - 1) return { trip: tr, s: s[lo] };
    const i = lo, span = arr[i + 1] - dep[i], spec = SPEC[bl.kind];
    const stops = bl.kind.startsWith('bus') ? hash(tr.id * 131 + i) < 0.65 : true;
    const dw = stops && i > 0 ? Math.min(spec.dwell, 0.3 * span) : 0;
    return { trip: tr, s: s[i] + run(t - dep[i] - dw, span - dw, s[i + 1] - s[i]) };
  }
}

export async function loadTransit(scene: THREE.Scene) {
  return new Transit(await fetchJson<Net>('data/transit/net.json'), scene);
}
