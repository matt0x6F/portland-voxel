import * as THREE from 'three';
import { dataUrl, fetchJson } from './util';

// Street names painted on the streets, as a map prints them (placed by scripts/streets.py: where each label goes and
// the stretch of centreline it lies on, with the ground's height along it). Every letter is a quad flat on the road,
// following its curve and slope; each label reads left to right from wherever the camera is (the shader picks the
// forward or the reversed layout by the label's direction on screen), and fades in once its letters are big enough
// on screen to read. Drawn in two passes, halo then fill, so one letter's halo never covers its neighbour.

type Raw = { labels: [string, number, number[]][] };
/** where the map shows fine detail (lod.ts): a mask over the city, `size` metres across */
export interface Detail { tex: THREE.Texture; size: [number, number]; fine: (x: number, z: number) => boolean }

const HEIGHT = [10, 7, 6, 5, 3.6, 3]; // cap height (m) by class: as scripts/streets.py placed them
const PX = 64;                        // atlas em, px
const CAP = 0.727;                    // Inter's cap height, em
const HALF = Math.ceil(0.62 * PX);    // atlas cell: cap middle to the cell edge, without the halo
const PAD = 10;                       // atlas px around each glyph, for its halo
const TRACK = 0.04;                   // letter spacing, em
const TILE = 2048;                    // m: one draw per tile, skipped when far off
const FONT = `700 ${PX}px Inter, system-ui, sans-serif`;

const VERT = /* glsl */ `
attribute vec3 aPosF, aDirF, aPosR, aDirR, aChord, aSize;
attribute vec4 aUV;
uniform float uFocal;
uniform sampler2D uDetail;
uniform vec2 uDetailSize;
varying vec2 vUv;
varying float vAlpha;
void main() {
  // left to right, and anything steeper than 60 degrees on screen bottom to top, as maps print it (so a street
  // running up the screen never reads downwards, nor flickers between the two as the view turns past it)
  vec3 cv = (viewMatrix * vec4(aChord, 0.0)).xyz;
  bool flip = cv.x + 0.58 * cv.y < 0.0;
  vec3 p = flip ? aPosR : aPosF, t = flip ? aDirR : aDirF;
  vec3 up = normalize(cross(vec3(0.0, 1.0, 0.0), t));
  vec3 w = p + t * position.x * aSize.x + up * position.y * aSize.y;
  vec3 toCam = cameraPosition - w;
  float d = length(toCam), slant = toCam.y / d;
  // the letters' height on screen, foreshortened by how low the view is over the road
  float px = aSize.z * uFocal / d * max(slant, 0.15);
  vAlpha = smoothstep(5.5, 8.5, px) * smoothstep(0.04, 0.2, slant);
  vAlpha *= step(0.5, texture2D(uDetail, p.xz / uDetailSize).r); // only on ground drawn in fine detail
  w += toCam / d * (0.25 + 0.004 * d); // over the road, not in it: kerbs and coarse tiles stay behind
  gl_Position = vAlpha > 0.003 ? projectionMatrix * viewMatrix * vec4(w, 1.0) : vec4(2.0, 2.0, 2.0, 1.0);
  vUv = mix(aUV.xy, aUV.zw, position.xy + 0.5);
}`;

const FRAG = /* glsl */ `
uniform sampler2D uAtlas;
uniform float uPass, uOpacity;
varying vec2 vUv;
varying float vAlpha;
void main() {
  vec4 s = texture2D(uAtlas, vUv);
  float a = (uPass < 0.5 ? s.g * 0.72 : s.r) * vAlpha * uOpacity;
  if (a < 0.01) discard;
  gl_FragColor = vec4(uPass < 0.5 ? vec3(0.015, 0.018, 0.024) : vec3(0.93), a);
}`;

interface Glyph { u0: number; v0: number; u1: number; v1: number; w: number; adv: number }

// one atlas for every character in use: R the letter, G the letter with its halo
function atlas(chars: string[], aniso: number) {
  const meas = document.createElement('canvas').getContext('2d')!;
  meas.font = FONT;
  const W = 1024, cellH = 2 * (HALF + PAD);
  const cells: { ch: string; x: number; y: number; w: number; adv: number }[] = [];
  let x = 0, y = 0;
  for (const ch of chars) {
    const adv = meas.measureText(ch).width, w = Math.ceil(adv + 2 * PAD);
    if (x + w > W) { x = 0; y += cellH; }
    cells.push({ ch, x, y, w, adv });
    x += w;
  }
  const H = THREE.MathUtils.ceilPowerOfTwo(y + cellH);
  const draw = (halo: boolean) => {
    const c = document.createElement('canvas');
    c.width = W; c.height = H;
    const g = c.getContext('2d')!;
    g.font = FONT; g.fillStyle = g.strokeStyle = '#fff'; g.lineJoin = 'round'; g.lineWidth = 12;
    for (const e of cells) {
      const bx = e.x + PAD, by = e.y + PAD + HALF + (CAP * PX) / 2; // the cap's middle at the cell's middle
      if (halo) g.strokeText(e.ch, bx, by);
      g.fillText(e.ch, bx, by);
    }
    return g.getImageData(0, 0, W, H).data;
  };
  const fill = draw(false), halo = draw(true), data = new Uint8Array(W * H * 4);
  for (let i = 0; i < W * H; i++) { data[i * 4] = fill[i * 4 + 3]; data[i * 4 + 1] = halo[i * 4 + 3]; data[i * 4 + 3] = 255; }
  const tex = new THREE.DataTexture(data, W, H, THREE.RGBAFormat);
  tex.generateMipmaps = true;
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.anisotropy = aniso;
  tex.needsUpdate = true;
  const glyphs = new Map<string, Glyph>();
  for (const e of cells) glyphs.set(e.ch, { u0: e.x / W, v0: (e.y + cellH) / H, u1: (e.x + e.w) / W, v1: e.y / H, w: e.w, adv: e.adv });
  return { tex, glyphs, cellH, meas };
}

// a polyline in world space (x, height, z) with arc length: points and smoothed directions anywhere along it
class Path {
  readonly pts: THREE.Vector3[] = [];
  readonly cum: number[] = [0];
  constructor(raw: number[]) {
    for (let i = 0; i < raw.length; i += 3) this.pts.push(new THREE.Vector3(raw[i] / 10, raw[i + 2] / 10, raw[i + 1] / 10));
    for (let i = 1; i < this.pts.length; i++) this.cum.push(this.cum[i - 1] + this.pts[i].distanceTo(this.pts[i - 1]));
  }
  get length() { return this.cum[this.cum.length - 1]; }
  at(s: number, out = new THREE.Vector3()) {
    const n = this.pts.length;
    let i = 1;
    while (i < n - 1 && this.cum[i] < s) i++; // beyond either end: carried on along the end segment
    const a = this.pts[i - 1], b = this.pts[i], L = this.cum[i] - this.cum[i - 1] || 1;
    return out.lerpVectors(a, b, (s - this.cum[i - 1]) / L);
  }
  dir(s: number, half: number) {
    return this.at(s + half).sub(this.at(s - half)).normalize();
  }
}

export class Streets {
  private readonly tiles: { mesh: THREE.Mesh[]; x0: number; z0: number }[] = [];
  private readonly uniforms = { uAtlas: { value: null as THREE.Texture | null }, uFocal: { value: 1000 }, uOpacity: { value: 1 },
    uDetail: { value: null as THREE.Texture | null }, uDetailSize: { value: new THREE.Vector2(1, 1) } };
  visible = true;

  constructor(raw: Raw, scene: THREE.Scene, aniso: number, detail?: Detail) {
    const all = new THREE.DataTexture(new Uint8Array([255, 255, 255, 255]), 1, 1);
    all.needsUpdate = true;
    this.uniforms.uDetail.value = detail?.tex ?? all;
    this.uniforms.uDetailSize.value.set(...(detail?.size ?? [1, 1]));
    const chars = [...new Set(raw.labels.flatMap(([t]) => [...t]))].filter((c) => c !== ' ');
    const { tex, glyphs, cellH, meas } = atlas(chars, aniso);
    this.uniforms.uAtlas.value = tex;
    const byTile = new Map<string, number[][]>(); // attribute rows per tile
    const v = new THREE.Vector3();
    for (const [text, cls, pts] of raw.labels) {
      const path = new Path(pts), h = HEIGHT[cls] ?? 4, k = h / CAP / PX; // metres per atlas px
      const total = (meas.measureText(text).width + TRACK * PX * (text.length - 1)) * k;
      const s0 = (path.length - total) / 2;
      const chord = path.pts[path.pts.length - 1].clone().sub(path.pts[0]).normalize();
      const mid = path.at(path.length / 2);
      const key = `${Math.floor(mid.x / TILE)},${Math.floor(mid.z / TILE)}`;
      if (!byTile.has(key)) byTile.set(key, []);
      const rows = byTile.get(key)!;
      for (let i = 0; i < text.length; i++) {
        const g = glyphs.get(text[i]);
        if (!g) continue;
        const x = (meas.measureText(text.slice(0, i)).width + TRACK * PX * i + g.adv / 2) * k; // kerned, tracked
        const half = Math.max(g.adv * k * 0.6, 1.5);
        const sf = s0 + x, sr = path.length - sf;
        const pf = path.at(sf), df = path.dir(sf, half), pr = path.at(sr, v.clone()), dr = path.dir(sr, half).negate();
        rows.push([pf.x, pf.y, pf.z, df.x, df.y, df.z, pr.x, pr.y, pr.z, dr.x, dr.y, dr.z, chord.x, chord.y, chord.z,
          g.w * k, cellH * k, h, g.u0, g.v0, g.u1, g.v1]);
      }
    }
    const quad = new THREE.PlaneGeometry(1, 1);
    const mats = [0, 1].map((pass) => new THREE.ShaderMaterial({
      vertexShader: VERT, fragmentShader: FRAG, transparent: true, depthWrite: false, side: THREE.DoubleSide,
      uniforms: { ...this.uniforms, uPass: { value: pass } },
    }));
    for (const [key, rows] of byTile) {
      const [tx, tz] = key.split(',').map(Number);
      const g = new THREE.InstancedBufferGeometry();
      g.index = quad.index;
      g.setAttribute('position', quad.getAttribute('position'));
      const n = rows.length, layout: [string, number, number][] = [['aPosF', 0, 3], ['aDirF', 3, 3], ['aPosR', 6, 3], ['aDirR', 9, 3],
        ['aChord', 12, 3], ['aSize', 15, 3], ['aUV', 18, 4]];
      for (const [name, off, size] of layout) {
        const a = new Float32Array(n * size);
        for (let r = 0; r < n; r++) for (let c = 0; c < size; c++) a[r * size + c] = rows[r][off + c];
        g.setAttribute(name, new THREE.InstancedBufferAttribute(a, size));
      }
      g.instanceCount = n;
      g.boundingSphere = new THREE.Sphere(new THREE.Vector3((tx + 0.5) * TILE, 100, (tz + 0.5) * TILE), TILE * 0.75 + 300);
      const mesh = mats.map((m, pass) => {
        const o = new THREE.Mesh(g, m);
        o.renderOrder = 10 + pass; // after the map's own transparent bits, halo before fill
        o.matrixAutoUpdate = false;
        scene.add(o);
        return o;
      });
      this.tiles.push({ mesh, x0: tx * TILE, z0: tz * TILE });
    }
  }

  update(camera: THREE.PerspectiveCamera, night: number) {
    this.uniforms.uFocal.value = innerHeight / 2 / Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2);
    this.uniforms.uOpacity.value = 1 - 0.25 * night;
    // the biggest letters read up to ~focal * 10 m / 5.5 px away: tiles beyond that are skipped outright
    const reach = this.uniforms.uFocal.value * HEIGHT[0] / 5.5, p = camera.position;
    for (const t of this.tiles) {
      const dx = Math.max(t.x0 - p.x, 0, p.x - t.x0 - TILE), dz = Math.max(t.z0 - p.z, 0, p.z - t.z0 - TILE);
      const on = this.visible && Math.hypot(dx, dz) < reach;
      for (const m of t.mesh) m.visible = on;
    }
  }
}

export async function loadStreets(scene: THREE.Scene, aniso: number, detail?: Detail) {
  const raw = await fetchJson<Raw>(dataUrl('streets.json'));
  await document.fonts?.load(FONT);
  return new Streets(raw, scene, aniso, detail);
}
