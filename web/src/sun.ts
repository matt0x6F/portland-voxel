// East Portland (SE 122nd & Foster); longitude east-positive
const RAD = Math.PI / 180, LAT = 45.48 * RAD, LON = -122.53;

// the n-th Sunday (1-based) of a month, as a day of the month
const nthSunday = (year: number, month: number, n: number) => 1 + (7 - new Date(Date.UTC(year, month, 1)).getUTCDay()) % 7 + 7 * (n - 1);
export const dayOfYear = (d: Date) => Math.round((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) - Date.UTC(d.getFullYear(), 0, 0)) / 864e5);
export const dateOf = (day: number, year = new Date().getFullYear()) => new Date(year, 0, day);

// The moment the browser's clock says, as it is in Portland -- wherever the viewer is: the map is the city's own time
// (someone in New York at 9 pm sees Portland at 6 pm). Day of year and fractional hour, Pacific time with its DST.
const PDX = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', year: 'numeric', month: 'numeric', day: 'numeric',
  hour: 'numeric', minute: 'numeric', second: 'numeric', hourCycle: 'h23' });
export function portlandNow(at = new Date()) {
  const p = Object.fromEntries(PDX.formatToParts(at).map((x) => [x.type, x.value]));
  return { day: dayOfYear(new Date(+p.year, +p.month - 1, +p.day)), hour: +p.hour + +p.minute / 60 + +p.second / 3600 };
}

// US Pacific time: PDT (UTC-7) from the second Sunday of March to the first Sunday of November, else PST (UTC-8)
export function utcOffset(day: number, year = new Date().getFullYear()) {
  const start = dayOfYear(new Date(year, 2, nthSunday(year, 2, 2))), end = dayOfYear(new Date(year, 10, nthSunday(year, 10, 1)));
  return day >= start && day < end ? -7 : -8;
}

// NOAA low-precision declination and equation of time
export function sunAt(day: number, hour: number) {
  const g = 2 * Math.PI / 365 * (day - 1 + (hour - 12) / 24);
  const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g) + 0.000907 * Math.sin(2 * g)
    - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
  const eot = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g) - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
  const solar = hour - utcOffset(day) + LON / 15 + eot / 60;
  const H = (solar - 12) * 15 * RAD;
  const el = Math.asin(Math.sin(LAT) * Math.sin(decl) + Math.cos(LAT) * Math.cos(decl) * Math.cos(H));
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(LAT) - Math.tan(decl) * Math.cos(LAT)) + Math.PI;
  return { el, az };
}

const elDeg = (day: number, h: number) => sunAt(day, h).el / RAD;

export function solarNoon(day: number) {
  let lo = 9, hi = 16;
  for (let i = 0; i < 30; i++) { const m = (lo + hi) / 2; if (elDeg(day, m + 1e-3) > elDeg(day, m)) lo = m; else hi = m; }
  return lo;
}

export function hourAt(day: number, deg: number, evening: boolean) {
  const noon = solarNoon(day);
  let lo = evening ? noon : noon - 12, hi = evening ? noon + 12 : noon;
  for (let i = 0; i < 30; i++) {
    const m = (lo + hi) / 2, above = elDeg(day, m) > deg;
    if (above === evening) lo = m; else hi = m;
  }
  return (lo + hi) / 2;
}

// Clear-sky transmittance of the direct beam at 610, 550, 465 nm: Rayleigh (Bucholtz), urban haze (Ångström β 0.05,
// α 1.3), ozone (Chappuis band); Kasten-Young air mass
const TAU = [0.191, 0.244, 0.339];
export const airMass = (elDeg: number) => { const h = Math.max(elDeg, -0.5); return 1 / (Math.sin(h * RAD) + 0.50572 * (h + 6.07995) ** -1.6364); };
export const beam = (elDeg: number) => TAU.map((t) => Math.exp(-airMass(elDeg) * t)) as [number, number, number];
