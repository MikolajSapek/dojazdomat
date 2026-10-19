/* Dojazdomat: dojazd × czynsz w Warszawie. Mapa na całym ekranie, filtry, legenda i ranking pływają nad nią. */
'use strict';

// ---------- constants ----------
const BASE = new URLSearchParams(location.search).get('fixtures') === '1' ? 'fixtures/' : 'data/';
const LAT0 = 51.98, LAT1 = 52.48, LON0 = 20.62, LON1 = 21.48;  // city + commuter belt
const DLAT = 0.001, DLON = 0.0016;                     // ~111 m x ~109 m cells
const ROWS = Math.ceil((LAT1 - LAT0) / DLAT - 1e-9);
const COLS = Math.ceil((LON1 - LON0) / DLON - 1e-9);
const M_LAT = 110540, M_LON = 111320 * Math.cos(52.23 * Math.PI / 180);
const DEFAULT_ORIGIN = [52.2329, 20.9986];             // Rondo ONZ
const WALK_DEFAULT = { mPerMin: 70, circuity: 1, stopWalkCap: 14, originWalkCap: 25 };
const STOP_CUT = 90,  // cells slower than 1.5 h are left blank
      NEAR_NAME_MIN = 20;
const BANDS = [
  { max: 15, hex: '#ECB96A', label: '≤15' }, { max: 25, hex: '#74B25C', label: '15–25' },
  { max: 35, hex: '#D883AE', label: '25–35' }, { max: 45, hex: '#9179E8', label: '35–45' },
  { max: 60, hex: '#35A08C', label: '45–60' }, { max: Infinity, hex: '#7FB3EF', label: '60–90' },
].map(b => ({ ...b, rgb: [1, 3, 5].map(i => parseInt(b.hex.slice(i, i + 2), 16)) }));
const RAMP = ['#4A3518', '#775420', '#A47428', '#D29638', '#FFC058'];
const RAMP_RGB = RAMP.map(h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)));
const ALPHA = 158, GHOST_ALPHA = 55, VALUE_FACTORS = [1, 0.62, 0.34];
const BIN_M = 350, BIN_MIN = 3, HOOD_R = 900, HOOD_MIN = 5, BREAK_MIN = 20, TERCILE_MIN = 6;
const LABEL_ALL_ZOOM = 13;
const REF_MIN_ZOOM = 14;  // basemap place-name tiles below this zoom cover the neighborhood labels
const RANK_MIN_N = 20;
const STACKED_PIN = 12;   // ponytail: >= this many ads on one exact point = geocoder fallback, not a building; big rental blocks can hit it too
// Time is money: 2026 minimum hourly wage (31,40 zł brutto, Dz.U. 2025 poz. 1242), 2 trips x 21 workdays a month.
const WAGE_PER_MIN = 31.40 / 60, TRIPS_PER_MONTH = 42;
const commuteCost = min => Math.round(min * TRIPS_PER_MONTH * WAGE_PER_MIN);    // neighborhoods with fewer listings are too noisy to rank (outliers like Wesoła)
let refLayer;
// Pliki portali są sprawdzane po nazwie (statyczny serwer nie listuje katalogu); 404 ignorowane.
const PORTALS = ['otodom', 'olx', 'gratka', 'morizon', 'adresowo', 'domiporta', 'nieruchomosci-online',
  'sprzedajemy-pl-rental-section-private-owners', 'szybko-pl-og-oszenia-nieruchomos-ci', 'allegrolokalnie-pl-nieruchomosci-wynajem'];
const PORTAL_NAMES = { otodom: 'Otodom', olx: 'OLX', gratka: 'Gratka', morizon: 'Morizon', adresowo: 'Adresowo', domiporta: 'Domiporta', 'nieruchomosci-online': 'Nieruchomości-online',
  'sprzedajemy-pl-rental-section-private-owners': 'Sprzedajemy.pl', 'szybko-pl-og-oszenia-nieruchomos-ci': 'Szybko.pl', 'allegrolokalnie-pl-nieruchomosci-wynajem': 'Allegro Lokalnie' };
const AMENITY_PL = { furnished: 'umeblowane', balcony: 'balkon', dishwasher: 'zmywarka', washer: 'pralka', ac: 'klimatyzacja', elevator: 'winda', parking: 'parking', pets: 'zwierzęta OK', private: 'bez pośrednika' };
const LS_ORIGIN = 'cmw-origin', LS_SOURCE = 'cmw-source';

// ---------- small helpers ----------
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const nf1 = new Intl.NumberFormat('pl-PL', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf0 = new Intl.NumberFormat('pl-PL');
const distM = (la1, lo1, la2, lo2) => Math.hypot((la1 - la2) * M_LAT, (lo1 - lo2) * M_LON);
const median = a => { if (!a.length) return null; const s = Float64Array.from(a).sort(); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const lsGet = k => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage blocked: not needed */ } };
const bandOf = t => BANDS.findIndex(b => t <= b.max);
const validOrigin = (lat, lon) => Number.isFinite(lat) && Number.isFinite(lon) && lat > LAT0 - 0.2 && lat < LAT1 + 0.2 && lon > LON0 - 0.3 && lon < LON1 + 0.3;

function fmtVal(v, metric, short) {
  if (v == null) return '–';
  if (metric === 'ppm') return short ? nf0.format(Math.round(v)) : `${nf0.format(Math.round(v))} zł/m²`;
  if (short) return `${nf1.format(v / 1000).replace(',0', '')} tys.`;
  return v >= 1000 ? `${nf1.format(v / 1000)} tys. zł` : `${nf0.format(Math.round(v))} zł`;
}

async function fetchOk(name, kind) {
  const res = await fetch(BASE + name, { cache: 'no-cache' });  // always revalidate: stops.json and matrix.bin must match
  if (!res.ok) throw new Error(`${name} (HTTP ${res.status})`);
  return kind === 'bin' ? res.arrayBuffer() : res.json();
}
const fetchOpt = (name, kind) => fetchOk(name, kind).catch(() => null);

// ---------- state ----------
const S = {
  stops: [], n: 0, M: null, walk: WALK_DEFAULT, walkFromFile: false, lines: [], hoods: [],
  sources: {}, sourceKeys: [], source: null,
  origin: DEFAULT_ORIGIN, originName: 'biuro',
  grid: new Float32Array(ROWS * COLS), stopBest: null,
  mode: 'commute', metric: 'price', rooms: 'any', areaMin: null, areaMax: null, checked: new Set(),
  filtered: [], breaks: null, binVal: null, binRows: 0, binCols: 0, terciles: null, hoodStats: [],
  budget: null, maxT: null, sort: 'cost', rank: [], cellRent: null, bandMed: [], tab: 'rank',
};

// ---------- commute engine (SPEC 2.6, Warsaw constants) ----------
// walk.json mPerMin is already a straight-line speed (network speed / circuity), so circuity is not applied again.
const walkMin = m => m / S.walk.mPerMin;

function paintDisc(lat, lon, capMin, base) {
  const rM = capMin * S.walk.mPerMin;
  const r0 = Math.max(0, Math.floor((lat - rM / M_LAT - LAT0) / DLAT));
  const r1 = Math.min(ROWS - 1, Math.floor((lat + rM / M_LAT - LAT0) / DLAT));
  const c0 = Math.max(0, Math.floor((lon - rM / M_LON - LON0) / DLON));
  const c1 = Math.min(COLS - 1, Math.floor((lon + rM / M_LON - LON0) / DLON));
  const g = S.grid;
  for (let r = r0; r <= r1; r++) {
    const dy = (LAT0 + (r + 0.5) * DLAT - lat) * M_LAT;
    for (let c = c0; c <= c1; c++) {
      const d = Math.hypot(dy, (LON0 + (c + 0.5) * DLON - lon) * M_LON);
      if (d > rM) continue;
      const t = base + walkMin(d), i = r * COLS + c;
      if (t < g[i]) g[i] = t;
    }
  }
}

function computeCommute() {
  const [lat, lon] = S.origin, n = S.n, M = S.M, best = new Float32Array(n).fill(Infinity);
  for (let a = 0; a < n; a++) {
    const st = S.stops[a], w = walkMin(distM(lat, lon, st.lat, st.lon));
    if (w > S.walk.stopWalkCap) continue;
    const off = 4 + a * n;
    for (let s = 0; s < n; s++) {
      const v = M[off + s];
      if (v !== 255 && w + v < best[s]) best[s] = w + v;
    }
  }
  S.stopBest = best;
  S.grid.fill(Infinity);
  paintDisc(lat, lon, S.walk.originWalkCap, 0);
  for (let s = 0; s < n; s++) if (best[s] < STOP_CUT) paintDisc(S.stops[s].lat, S.stops[s].lon, S.walk.stopWalkCap, best[s]);
}

function cellAt(lat, lon) {
  const r = Math.floor((lat - LAT0) / DLAT), c = Math.floor((lon - LON0) / DLON);
  return r < 0 || c < 0 || r >= ROWS || c >= COLS ? -1 : r * COLS + c;
}
function commuteAt(lat, lon) { const i = cellAt(lat, lon); return i < 0 || !isFinite(S.grid[i]) ? null : S.grid[i]; }

function nameOrigin(lat, lon) {
  let bi = -1, bd = Infinity;
  S.stops.forEach((s, i) => { const d = distM(lat, lon, s.lat, s.lon); if (d < bd) { bd = d; bi = i; } });
  return bi >= 0 && walkMin(bd) <= NEAR_NAME_MIN ? `okolice: ${S.stops[bi].name}` : `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
}

// ---------- rent engine (SPEC 3) ----------
function currentRows() { return S.source ? S.sources[S.source].rows : []; }

function availableMask() {
  let m = 0;
  for (const r of currentRows()) m |= r[5];
  return m;
}

function applyFilter() {
  const bits = S.flagBits || {}, avail = availableMask();
  let mask = 0;
  for (const k of S.checked) if (bits[k] && (avail & bits[k])) mask |= bits[k];
  const ppm = S.metric === 'ppm', out = [];
  for (const r of currentRows()) {
    const [lat, lon, price, rooms, area, flags, prec] = r;
    if (S.rooms !== 'any' && rooms !== +S.rooms) continue;
    if ((S.areaMin != null || S.areaMax != null) && area < 0) continue;
    if (S.areaMin != null && area < S.areaMin) continue;
    if (S.areaMax != null && area > S.areaMax) continue;
    if ((flags & mask) !== mask) continue;
    if (ppm && !(area > 0)) continue;
    out.push({ lat, lon, v: ppm ? price / area : price, prec });
  }
  S.filtered = out;
  S.breaks = null;
  if (out.length >= BREAK_MIN) {
    const s = Float64Array.from(out, o => o.v).sort();
    S.breaks = [0.2, 0.4, 0.6, 0.8].map(p => s[Math.min(s.length - 1, Math.floor(p * s.length))]);
  }
  buildBins();
}

function buildBins() {
  const br = S.binRows = Math.ceil(ROWS * DLAT * M_LAT / BIN_M), bc = S.binCols = Math.ceil(COLS * DLON * M_LON / BIN_M);
  const lists = Array.from({ length: br * bc }, () => []);
  for (const o of S.filtered) {
    if (o.prec >= 2) continue; // district centroids only: excluded from spatial stats
    const by = Math.floor((o.lat - LAT0) * M_LAT / BIN_M), bx = Math.floor((o.lon - LON0) * M_LON / BIN_M);
    if (by >= 0 && bx >= 0 && by < br && bx < bc) lists[by * bc + bx].push(o.v);
  }
  // Fix for SPEC Q-22: every bin whose 3x3 block holds >= 3 listings gets the block median, own bin empty or not.
  const val = new Float32Array(br * bc).fill(NaN);
  for (let y = 0; y < br; y++) for (let x = 0; x < bc; x++) {
    const vals = [];
    for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
      const yy = y + dy, xx = x + dx;
      if (yy >= 0 && xx >= 0 && yy < br && xx < bc) { const l = lists[yy * bc + xx]; for (let k = 0; k < l.length; k++) vals.push(l[k]); }
    }
    if (vals.length >= BIN_MIN) val[y * bc + x] = median(vals);
  }
  S.binVal = val;
}

function rentAt(lat, lon) {
  const by = Math.floor((lat - LAT0) * M_LAT / BIN_M), bx = Math.floor((lon - LON0) * M_LON / BIN_M);
  if (!S.binVal || by < 0 || bx < 0 || by >= S.binRows || bx >= S.binCols) return null;
  const v = S.binVal[by * S.binCols + bx];
  return Number.isNaN(v) ? null : v;
}

function buildCellRent() {
  const rent = new Float32Array(ROWS * COLS);
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
    const v = rentAt(LAT0 + (r + 0.5) * DLAT, LON0 + (c + 0.5) * DLON);
    rent[r * COLS + c] = v == null ? NaN : v;
  }
  return rent;
}

function computeTerciles(rent) {
  const per = BANDS.map(() => []);
  for (let i = 0; i < S.grid.length; i++) {
    const t = S.grid[i], v = rent[i];
    if (isFinite(t) && !Number.isNaN(v)) per[bandOf(t)].push(v);
  }
  return per.map(a => {
    if (a.length < TERCILE_MIN) return null;
    const s = Float64Array.from(a).sort();
    return [s[Math.floor(s.length / 3)], s[Math.floor(2 * s.length / 3)]];
  });
}

function computeHoodStats() {
  const has = !!S.source;
  S.hoodStats = S.hoods.map(h => {
    let vals = [];
    if (has) for (const o of S.filtered) if (o.prec < 2 && distM(h.lat, h.lon, o.lat, o.lon) <= HOOD_R) vals.push(o.v);
    return { n: vals.length, med: vals.length >= HOOD_MIN ? median(vals) : null, commute: commuteAt(h.lat, h.lon) };
  });
}


// ---------- rendering ----------
let map, overlay, lineLayers, stopLayers, hubLayer, labelLayer, officeMarker, selMarker, openStopPopup = null;
const MODES = ['metro', 'rail', 'tram', 'bus'];
const canvas = document.createElement('canvas');
canvas.width = COLS; canvas.height = ROWS;
const tysNum = v => nf1.format(v / 1000).replace(',0', '');
const tick = v => (S.metric === 'ppm' ? nf0.format(Math.round(v)) : tysNum(v));

function renderRaster() {
  const ctx = canvas.getContext('2d'), img = ctx.createImageData(COLS, ROWS), px = img.data;
  const rent = S.mode === 'commute' ? null : S.cellRent;
  if (S.mode === 'value') S.terciles = computeTerciles(rent);
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
    const i = r * COLS + c, t = S.grid[i];
    if (!isFinite(t) || t > STOP_CUT || (S.maxT != null && t > S.maxT)) continue;
    let rgb, a = ALPHA;
    const b = BANDS[bandOf(t)];
    if (S.mode === 'commute') rgb = b.rgb;
    else if (S.mode === 'rent') {
      const v = rent[i];
      if (Number.isNaN(v) || !S.breaks || (S.budget != null && v > S.budget)) continue;
      let k = S.breaks.findIndex(x => v <= x); if (k < 0) k = 4;
      rgb = RAMP_RGB[k];
    } else {
      const v = rent[i], ter = S.terciles[bandOf(t)];
      if (!Number.isNaN(v) && S.budget != null && v > S.budget) continue;
      if (Number.isNaN(v) || !ter) { rgb = b.rgb; a = GHOST_ALPHA; }
      else { const f = VALUE_FACTORS[v <= ter[0] ? 0 : v <= ter[1] ? 1 : 2]; rgb = b.rgb.map(x => Math.round(x * f)); }
    }
    const p = ((ROWS - 1 - r) * COLS + c) * 4;
    px[p] = rgb[0]; px[p + 1] = rgb[1]; px[p + 2] = rgb[2]; px[p + 3] = a;
  }
  ctx.putImageData(img, 0, 0);
  overlay.setUrl(canvas.toDataURL());
}

// Legenda: jeden pasek, podpisy pod segmentami, bez zawijania.
function renderLegend() {
  const el = $('legend'), bar = (cols, labels) =>
    `<div class="lg-bar">${cols.map(c => `<i style="background:${c}"></i>`).join('')}</div><div class="lg-ticks">${labels.map(l => `<span>${l}</span>`).join('')}</div>`;
  if (S.mode === 'commute') {
    el.innerHTML = `<div class="lg-title"><span>Czas dojazdu od drzwi do drzwi, min</span></div>${bar(BANDS.map(b => b.hex), BANDS.map(b => b.label))}`;
  } else if (S.mode === 'rent') {
    if (!S.breaks) { el.innerHTML = '<div class="lg-note">Za mało ogłoszeń dla tych filtrów (min. 20). Poluzuj filtry.</div>'; return; }
    const B = S.breaks.map(tick), u = S.metric === 'ppm' ? 'zł za m²' : 'tys. zł / mies.';
    el.innerHTML = `<div class="lg-title"><span>Czynsz (mediana w okolicy), ${u}</span></div>${bar(RAMP, [`<${B[0]}`, `${B[0]}–${B[1]}`, `${B[1]}–${B[2]}`, `${B[2]}–${B[3]}`, `${B[3]}+`])}`;
  } else {
    const rows = ['tanio', 'średnio', 'drogo'].map((ax, k) => `<span class="ax">${ax}</span>` + BANDS.map(b =>
      `<i style="background:rgb(${b.rgb.map(x => Math.round(x * VALUE_FACTORS[k])).join(',')})"></i>`).join('')).join('');
    el.innerHTML = `<div class="lg-title"><span>dojazd (min) × czynsz na tle strefy</span></div><div class="lg-grid">${rows}<span></span>${BANDS.map(b => `<span class="cl">${b.label}</span>`).join('')}</div><div class="lg-note lg-sub">jasno = najtaniej przy takim dojeździe</div>`;
  }
}

function labelHtml(h, st) {
  let line = '';
  if (S.source) line = st.med != null ? `<div class="p">${esc(fmtVal(st.med, S.metric))} · ${nf0.format(st.n)} ogł.</div>` : '<div class="p muted">n &lt; 5</div>';
  return `<div class="n">${esc(h.name)}</div>${line}`;
}

// Label text is always rebuilt via setIcon, which also updates markers that are off the map,
// so re-added labels (zoom or toggle) never come back blank (fixes SPEC Q-01, Q-02).
function renderLabels() {
  const showAll = map.getZoom() >= LABEL_ALL_ZOOM, on = $('t-labels').checked;
  const primary = new Map(); // per district: hood with the largest sample (or first) shows when zoomed out
  S.hoods.forEach((h, i) => { const p = primary.get(h.district); if (p == null || S.hoodStats[i].n > S.hoodStats[p].n) primary.set(h.district, i); });
  // Greedy collision pass: biggest samples first; a label that would overlap a placed one (or the office pin) is hidden.
  const o = map.latLngToContainerPoint(S.origin), taken = [{ x: o.x - 30, y: o.y - 10, w: 60, h: 34 }];
  const hit = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  const order = S.hoods.map((h, i) => i).sort((a, b) => (S.hoodStats[b].n || 0) - (S.hoodStats[a].n || 0));
  order.forEach(i => {
    const h = S.hoods[i], m = h.marker;
    m.setIcon(L.divIcon({ className: 'hood-label', html: labelHtml(h, S.hoodStats[i]), iconSize: [160, 32], iconAnchor: [80, 16] }));
    let show = on && (showAll || primary.get(h.district) === i);
    if (show) {
      const pt = map.latLngToContainerPoint([h.lat, h.lon]), w = Math.max(h.name.length * 7, 90);
      const box = { x: pt.x - w / 2, y: pt.y - 15, w, h: 30 };
      show = !taken.some(t => hit(box, t));
      if (show) taken.push(box);
    }
    if (show && !labelLayer.hasLayer(m)) labelLayer.addLayer(m);
    if (!show && labelLayer.hasLayer(m)) labelLayer.removeLayer(m);
  });
}

// ---------- ranking osiedli ----------
const pct = x => `${Math.round(Math.abs(x) * 100)}%`;
function computeBandMed() {
  const per = BANDS.map(() => []);
  if (S.cellRent) for (let i = 0; i < S.grid.length; i++) { const t = S.grid[i], v = S.cellRent[i]; if (isFinite(t) && !Number.isNaN(v)) per[bandOf(t)].push(v); }
  S.bandMed = per.map(a => (a.length >= TERCILE_MIN ? median(a) : null));
}

function computeRank() {
  const pts = S.hoods.map((h, i) => ({ i, h, ...S.hoodStats[i] }))
    .filter(p => p.med != null && p.commute != null && p.n >= RANK_MIN_N && (S.maxT == null || p.commute <= S.maxT) && (S.budget == null || p.med <= S.budget));
  let low = Infinity;
  for (const p of [...pts].sort((a, b) => a.commute - b.commute)) { if (p.med < low) { low = p.med; p.front = true; } }
  for (const p of pts) { const bm = S.bandMed[bandOf(p.commute)]; p.rel = bm ? p.med / bm - 1 : null; p.cost = p.med + commuteCost(p.commute); }
  const by = { value: (a, b) => (a.rel ?? 9) - (b.rel ?? 9), commute: (a, b) => a.commute - b.commute, rent: (a, b) => a.med - b.med, cost: (a, b) => a.cost - b.cost }[S.sort];
  S.rank = pts.sort(by);
}

function renderRank() {
  const ol = $('rank-list');
  if (!S.source) { ol.innerHTML = '<li class="empty">Brak ogłoszeń: ranking niedostępny.</li>'; return; }
  if (!S.rank.length) { ol.innerHTML = '<li class="empty">Żadne osiedle nie pasuje. Poluzuj budżet, dojazd lub filtry.</li>'; return; }
  ol.innerHTML = S.rank.map((p, k) => {
    const band = BANDS[bandOf(p.commute)], rel = p.rel == null ? '' : Math.abs(p.rel) < 0.03 ? '<span>typowo</span>' : p.rel < 0 ? `<span class="good">${pct(p.rel)} taniej</span>` : `<span class="bad">${pct(p.rel)} drożej</span>`;
    return `<li><button type="button" class="row${S.sel === p.i ? ' on' : ''}" data-i="${p.i}" title="${esc(p.h.district)}: ${nf0.format(p.n)} ogłoszeń">
      <span class="n">${k + 1}</span><span class="nm">${esc(p.h.name)}</span><span class="pr">${esc(fmtVal(p.med, S.metric))}</span>
      <span class="sub"><span class="dot" style="background:${band.hex}" aria-hidden="true"></span><span>~${Math.round(p.commute)} min</span>${S.sort === 'value' ? rel : ''}${S.metric === 'ppm' ? '' : `<span class="cnt" title="czynsz + ${nf0.format(commuteCost(p.commute))} zł: czas dojazdu liczony stawką minimalną 31,40 zł/h, 42 przejazdy w miesiącu">razem ${esc(fmtVal(p.cost, 'price'))}</span>`}<span class="cnt">${nf0.format(p.n)} ogł.</span></span></button></li>`;
  }).join('');
}

function stopPopupHtml(i) {
  const s = S.stops[i], t = S.stopBest ? S.stopBest[i] : Infinity;
  return `<b>${esc(s.name)}</b><br>${esc((s.routes || []).join(' · '))}<br>${isFinite(t) ? `~${Math.round(t)} min od biura` : 'poza zasięgiem'}`;
}

function renderScatter() {
  const svg = $('scatter');
  const W = 320, H = 340, L0 = 44, B0 = 32, T0 = 10, R0 = 12, pw = W - L0 - R0, ph = H - T0 - B0;
  const pts = [...S.rank].sort((a, b) => a.commute - b.commute);
  if (pts.length < 3) { svg.innerHTML = `<text class="emptyt" x="${W / 2}" y="${H / 2}" text-anchor="middle">za mało osiedli pasuje do filtrów</text>`; return; }
  const front = pts.filter(p => p.front);
  const xMax = Math.min(95, Math.max(...pts.map(p => p.commute)) + 5);
  const meds = pts.map(p => p.med).sort((a, b) => a - b), q95 = meds[Math.floor(0.95 * (meds.length - 1))];
  const yTop = Math.max(q95, ...front.map(p => p.med)), span = yTop - meds[0];
  const step = niceStep(span / 5), y0 = Math.floor(meds[0] / step) * step;
  let y1 = Math.ceil(yTop / step) * step; if (y1 <= y0) y1 = y0 + step;
  const X = v => L0 + Math.min(v, xMax) / xMax * pw, Y = v => T0 + (1 - (Math.min(v, y1) - y0) / (y1 - y0)) * ph;
  let s = '';
  for (let v = 15; v < xMax; v += 15) s += `<line class="grid" x1="${X(v)}" x2="${X(v)}" y1="${T0}" y2="${T0 + ph}"/><text class="tick" x="${X(v)}" y="${T0 + ph + 13}" text-anchor="middle">${v}</text>`;
  for (let v = y0; v <= y1 + 1e-9; v += step) s += `<line class="grid" x1="${L0}" x2="${L0 + pw}" y1="${Y(v)}" y2="${Y(v)}"/><text class="tick" x="${L0 - 5}" y="${Y(v) + 3}" text-anchor="end">${tick(v)}</text>`;
  s += `<text class="axt" x="${L0 + pw / 2}" y="${H - 4}" text-anchor="middle">dojazd, min</text>`;
  s += `<text class="axt" x="4" y="${T0 + 4}">${S.metric === 'ppm' ? 'zł/m²' : 'tys. zł'}</text>`;
  if (front.length > 1) {
    let d = `M${X(front[0].commute)},${Y(front[0].med)}`;
    for (let k = 1; k < front.length; k++) d += ` H${X(front[k].commute)} V${Y(front[k].med)}`;
    s += `<path class="fline" d="${d}"/>`;
  }
  pts.forEach(p => {
    const col = BANDS[bandOf(p.commute)].hex, clipped = p.med > y1;
    const label = `${p.h.name}: ${fmtVal(p.med, S.metric)}, ~${Math.round(p.commute)} min (${p.n} ogł.)${clipped ? ', poza skalą' : ''}`;
    const attrs = `class="dot${p.front ? ' front' : ''}" tabindex="0" role="button" data-i="${p.i}" aria-label="${esc(label)}"`;
    const cx = X(p.commute), cy = Y(p.med);
    s += clipped
      ? `<path ${attrs} d="M${cx - 5},${cy + 4} L${cx + 5},${cy + 4} L${cx},${cy - 5} Z" fill="${col}"><title>${esc(label)}</title></path>`
      : `<circle ${attrs} cx="${cx}" cy="${cy}" r="${p.front ? 5 : 3.6}" fill="${col}"><title>${esc(label)}</title></circle>`;
  });
  s += placeLabels(front.slice(0, 5).map(p => ({ ...p, h: p.h })), X, Y, { L0, T0, R: W - 2, B: T0 + ph });
  svg.innerHTML = s;
}

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(Math.max(raw, 1e-9)));
  return [1, 2, 2.5, 5, 10].map(m => m * p).find(v => v >= raw);
}

// Greedy label placement: try four spots around each dot, keep the first that stays inside and hits no earlier label.
function placeLabels(items, X, Y, box) {
  const CH = 4.8, LH = 11, taken = items.map(p => ({ x: X(p.commute) - 5, y: Y(p.med) - 5, w: 10, h: 10 }));
  const hit = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
  let s = '';
  for (const p of items) {
    const text = `${p.h.name} · ${Math.round(p.commute)} min`, w = text.length * CH, cx = X(p.commute), cy = Y(p.med);
    const spots = [[cx + 7, cy - 13], [cx + 7, cy + 2], [cx - 7 - w, cy - 13], [cx - 7 - w, cy + 2], [cx + 7, cy - 24], [cx - 7 - w, cy + 13], [cx + 7, cy + 13]];
    const r = spots.map(([x, y]) => ({ x, y, w, h: LH }))
      .find(b => b.x >= box.L0 && b.x + b.w <= box.R && b.y >= box.T0 - 8 && b.y + b.h <= box.B && !taken.some(t => hit(b, t)));
    if (!r) continue;  // ponytail: no room = no label; the dot keeps its tooltip
    taken.push(r);
    s += `<text class="flab" x="${r.x}" y="${r.y + 8}">${esc(text)}</text>`;
  }
  return s;
}

function renderCount() {
  if (!S.source) return;
  const src = S.sources[S.source];
  const txt = `${nf0.format(S.filtered.length)} z ${nf0.format(src.rows.length)} ogłoszeń pasuje`;
  $('count').textContent = $('count-d').textContent = txt;
  $('note').textContent = `Opłacalność: czynsz osiedla na tle typowego w tej samej strefie dojazdu. Dane: ${src.label}, ${nf0.format(src.rows.length)} ogłoszeń, stan na ${src.asOf}. Rozkłady GTFS ZTM, SKM, KM, WKD. ${S.walkFromFile ? 'Pieszo wg walk.json.' : 'Pieszo 70 m/min.'}`;
}

function renderAmenities() {
  const fs = $('amenities'), bits = S.flagBits || {}, avail = availableMask();
  fs.innerHTML = '';
  let any = false;
  for (const [k, bit] of Object.entries(bits)) {
    const ok = (avail & bit) !== 0; any = any || ok;
    const lab = document.createElement('label');
    lab.className = 'chk chip-chk' + (ok ? '' : ' off');
    if (!ok) lab.title = 'brak tej informacji w tym źródle';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.disabled = !ok; cb.checked = ok && S.checked.has(k);
    cb.addEventListener('change', () => { cb.checked ? S.checked.add(k) : S.checked.delete(k); refreshRent(); });
    lab.append(cb, AMENITY_PL[k] || k);
    fs.append(lab);
  }
  $('amenity-msg').textContent = any ? '' : 'brak danych o udogodnieniach w tym portalu';
}

// ---------- update pipeline ----------
// Pełne odświeżenie: nowy punkt biura albo nowe filtry ogłoszeń.
function refreshAll() {
  S.cellRent = S.source ? buildCellRent() : null;
  computeBandMed(); computeHoodStats(); renderLabels(); updateView();
}
// Lekkie odświeżenie: suwaki budżetu i dojazdu oraz tryb mapy.
function updateView() { renderRaster(); renderLegend(); computeRank(); renderRank(); renderScatter(); }
function refreshRent() { applyFilter(); renderCount(); refreshAll(); }

function setOrigin(lat, lon, name) {
  document.body.classList.remove('start');
  clearSel();
  S.origin = [lat, lon];
  S.originName = name || nameOrigin(lat, lon);
  computeCommute();
  officeMarker.setLatLng(S.origin);
  if (document.activeElement !== $('search')) $('search').value = S.originName;
  lsSet(LS_ORIGIN, JSON.stringify(S.origin));
  history.replaceState(null, '', `#${lat.toFixed(5)},${lon.toFixed(5)}`);
  if (openStopPopup) openStopPopup.popup.setContent(stopPopupHtml(openStopPopup.i));
  refreshAll();
}

function setMode(mode) {
  if (mode !== 'commute' && !S.source) return;
  S.mode = mode;
  document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
  renderRaster(); renderLegend();
}

function setSource(key) {
  S.source = key;
  lsSet(LS_SOURCE, key);
  document.querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.source === key)));
  renderAmenities();
  refreshRent();
}

function resolveOrigin() {
  const m = location.hash.match(/^#(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)$/);
  if (m && validOrigin(+m[1], +m[2])) return [+m[1], +m[2]];
  try {
    const v = JSON.parse(lsGet(LS_ORIGIN));
    if (Array.isArray(v) && validOrigin(+v[0], +v[1])) return [+v[0], +v[1]];
  } catch { /* bad saved value: fall through to default */ }
  return null;
}

// ---------- data loading ----------
function parseListings(obj, key, label) {
  if (!obj || !Array.isArray(obj.listings)) return null;
  const rows = obj.listings.filter(r => Array.isArray(r) && r.length >= 7 && Number.isFinite(r[0]) && Number.isFinite(r[1]) && Number.isFinite(r[2]));
  return { key, label, asOf: obj.asOf || '?', flagBits: obj.flagBits || {}, rows };
}

async function loadAll() {
  const [stops, matrix, lines, hoods, walk, merged, ...portals] = await Promise.all([
    fetchOk('stops.json'), fetchOk('matrix.bin', 'bin'), fetchOpt('lines.json'), fetchOpt('neighborhoods.json'),
    fetchOpt('walk.json'), fetchOpt('listings.json'), ...PORTALS.map(p => fetchOpt(`listings-${p}.json`)),
  ]);
  const n = new DataView(matrix).getUint32(0, true);
  if (!Array.isArray(stops) || n !== stops.length || matrix.byteLength !== 4 + n * n) throw new Error(`matrix.bin (n=${n}) nie pasuje do stops.json (${stops.length})`);
  S.stops = stops; S.n = n; S.M = new Uint8Array(matrix);
  S.lines = Array.isArray(lines) ? lines : [];
  S.hoods = Array.isArray(hoods) ? hoods.filter(h => Number.isFinite(h.lat) && Number.isFinite(h.lon)) : [];
  if (walk && walk.mPerMin > 0) {
    S.walk = { ...WALK_DEFAULT, ...Object.fromEntries(Object.entries(walk).filter(([k, v]) => k in WALK_DEFAULT && Number.isFinite(v) && v > 0)) };
    S.walkFromFile = true;
  }
  const src = {};
  const m = parseListings(merged, 'merged', 'Wszystkie (scalone)');
  if (m) src.merged = m;
  PORTALS.forEach((p, i) => { const s = parseListings(portals[i], p, PORTAL_NAMES[p] || p); if (s) src[p] = s; });
  const portalKeys = PORTALS.filter(p => src[p]);
  // A coordinate shared by many ads (geocoder fallback, agency HQ, district pin) is not a real location:
  // demote those rows to precision 2 so they never skew neighborhood medians.
  const pinCount = new Map(), pinKey = r => `${r[0].toFixed(4)},${r[1].toFixed(4)}`;
  portalKeys.forEach(p => src[p].rows.forEach(r => { if (r[6] < 2) pinCount.set(pinKey(r), (pinCount.get(pinKey(r)) || 0) + 1); }));
  portalKeys.forEach(p => { src[p] = { ...src[p], rows: src[p].rows.map(r => (r[6] < 2 && pinCount.get(pinKey(r)) >= STACKED_PIN ? [...r.slice(0, 6), 2] : r)) }; });
  if (!m && portalKeys.length > 1) {
    // ponytail: plain concatenation, no cross-portal dedupe (that is the merge agent's listings.json).
    src.all = { key: 'all', label: 'Wszystkie (bez deduplikacji)', asOf: portalKeys.map(p => src[p].asOf).sort().pop(), flagBits: src[portalKeys[0]].flagBits, rows: portalKeys.flatMap(p => src[p].rows) };
  }
  S.sources = src;
  S.sourceKeys = [...(src.merged ? ['merged'] : []), ...(src.all ? ['all'] : []), ...portalKeys];
  S.flagBits = S.sourceKeys.length ? src[S.sourceKeys[0]].flagBits : {};
}

// ---------- map setup ----------
function initMap() {
  map = L.map('map', { center: [52.235, 21.01], zoom: 12, zoomControl: false, attributionControl: false, minZoom: 10, maxZoom: 18 });
  L.control.attribution({ position: 'bottomright', prefix: '<a href="https://leafletjs.com">Leaflet</a>' }).addTo(map);
  [['raster', 350], ['lines', 400], ['ref', 450], ['labels', 600]].forEach(([p, z]) => { map.createPane(p).style.zIndex = z; });
  map.getPane('ref').style.pointerEvents = 'none';
  map.getPane('labels').style.pointerEvents = 'none';
  const esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
  L.tileLayer(esri + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', { maxNativeZoom: 16, maxZoom: 18, attribution: 'Podkład © Esri, HERE, Garmin, © OpenStreetMap' }).addTo(map);
  // Basemap place names (Warszawa, districts) would sit on top of our labels; show them only for street-level zoom.
  refLayer = L.tileLayer(esri + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}', { maxNativeZoom: 16, maxZoom: 18, pane: 'ref', opacity: 0.75 });
  const syncRef = () => (map.getZoom() >= REF_MIN_ZOOM ? refLayer.addTo(map) : refLayer.remove());
  map.on('zoomend', syncRef); syncRef();
  const bounds = [[LAT0, LON0], [LAT0 + ROWS * DLAT, LON0 + COLS * DLON]];
  // ponytail: linear image overlay over Mercator; <1 cell skew across the 0.28° bbox. Tile the raster if it ever matters.
  overlay = L.imageOverlay(canvas.toDataURL(), bounds, { pane: 'raster', className: 'raster', interactive: false }).addTo(map);
  map.attributionControl.addAttribution('Rozkłady: ZTM Warszawa, SKM, KM, WKD (GTFS) · ogłoszenia: portale');
}

function buildMapLayers() {

  lineLayers = Object.fromEntries(MODES.map(m => [m, L.layerGroup()]));
  addLines(S.lines);
  const renderer = L.canvas({ pane: 'lines' });
  stopLayers = Object.fromEntries(MODES.map(m => [m, L.layerGroup()]));
  S.stops.forEach((s, i) => {
    const mk = L.circleMarker([s.lat, s.lon], { renderer, radius: 2.4, color: 'rgba(255,255,255,0.75)', weight: 1, fillColor: '#1a1a1a', fillOpacity: 0.9 });
    mk.bindPopup(() => stopPopupHtml(i));
    mk.on('popupopen', e => { openStopPopup = { i, popup: e.popup }; });
    mk.on('popupclose', () => { openStopPopup = null; });
    stopLayers[MODES.find(m => (s.modes || ['bus']).includes(m)) || 'bus'].addLayer(mk);
  });
  hubLayer = L.layerGroup();
  findHubs().forEach(h => {
    const mk = L.circleMarker([h.lat, h.lon], { pane: 'ref', radius: 6, color: '#fff', weight: 2, fillColor: '#111', fillOpacity: 1, interactive: false });
    mk.bindTooltip(h.name, { permanent: true, direction: 'right', offset: [7, 0], className: 'hub-label' });
    hubLayer.addLayer(mk);
  });
  labelLayer = L.layerGroup().addTo(map);
  S.hoods.forEach(h => { h.marker = L.marker([h.lat, h.lon], { pane: 'labels', interactive: false, keyboard: false, icon: L.divIcon({ className: 'hood-label', html: '' }) }); });
  officeMarker = L.marker(S.origin, {
    draggable: true, keyboard: true, title: 'Biuro: przeciągnij, aby przenieść', alt: 'Biuro', zIndexOffset: 1000,
    icon: L.divIcon({ className: 'office', html: '<div class="office-wrap"><div class="office-dot"></div><div class="office-tag">biuro</div></div>', iconSize: [60, 36], iconAnchor: [30, 8] }),
  }).addTo(map);
  officeMarker.on('dragend', () => { const p = officeMarker.getLatLng(); setOrigin(p.lat, p.lng); });
  syncLineLayers();
}

function addLines(lines) {
  for (const ln of lines) {
    const rail = ln.mode === 'metro' || ln.mode === 'rail', bus = ln.mode === 'bus';
    const style = { pane: 'lines', color: ln.color || '#fff', weight: ln.mode === 'metro' ? 2 : rail ? 1.4 : 0.8, opacity: ln.mode === 'metro' ? 0.75 : rail ? 0.55 : 0.3, interactive: false };
    for (const p of ln.paths || []) L.polyline(p, style).addTo(lineLayers[ln.mode] || lineLayers.bus);
  }
}

// Transfer hubs: stop groups within 250 m pooled; a hub links two rail-type modes, or metro/rail with 6+ routes, or 25+ routes.
function findHubs() {
  const near = (a, b) => distM(a.lat, a.lon, b.lat, b.lon), cand = [];
  S.stops.forEach(s => {
    const pool = S.stops.filter(t => Math.abs(t.lat - s.lat) < 0.003 && near(s, t) <= 250);
    const modes = new Set(pool.flatMap(t => t.modes || [])), routes = new Set(pool.flatMap(t => t.routes || []));
    const heavy = ['metro', 'rail', 'tram'].filter(m => modes.has(m));
    if (heavy.length >= 2 || ((modes.has('metro') || modes.has('rail')) && routes.size >= 6) || routes.size >= 25)
      cand.push({ name: s.name, lat: s.lat, lon: s.lon, w: routes.size + 10 * heavy.length });
  });
  cand.sort((a, b) => b.w - a.w);
  return cand.reduce((out, c) => (out.every(o => near(o, c) > 400) ? [...out, c] : out), []);
}

let busLinesLoaded = false;
function syncLineLayers() {
  const z = map.getZoom(), r = z >= 14 ? 3.5 : z >= 12 ? 2.4 : 1.4;
  if ($('t-bus').checked && !busLinesLoaded) {
    busLinesLoaded = true;
    fetchOpt('lines-bus.json').then(d => { if (Array.isArray(d)) { addLines(d); syncLineLayers(); } });
  }
  MODES.forEach(m => {
    const on = $('t-' + m).checked;
    stopLayers[m].eachLayer(l => l.setRadius(m === 'bus' ? Math.max(1.4, r - 1) : r));
    on ? lineLayers[m].addTo(map) : lineLayers[m].remove();
    on && (m !== 'bus' || z >= 14) ? stopLayers[m].addTo(map) : stopLayers[m].remove();
  });
  $('t-hubs').checked && z >= 12 ? hubLayer.addTo(map) : hubLayer.remove();
  map.getContainer().classList.toggle('hub-names', z >= 14);
}

// ---------- UI ----------
const isPhone = () => matchMedia('(max-width: 700px)').matches;
const SNAPS = ['peek', 'half', 'full'];
const snapPx = k => ({ peek: 120, half: Math.round(innerHeight * 0.5), full: Math.round(innerHeight * 0.88) }[k]);

function setSnap(k) {
  const sh = $('sheet');
  sh.dataset.snap = k; document.body.dataset.snap = k;
  document.documentElement.style.setProperty('--sheet-h', snapPx(k) + 'px');
  $('grip-state').textContent = `Panel: ${{ peek: 'zwinięty', half: 'do połowy', full: 'rozwinięty' }[k]}`;
}

function setTab(tab, fromUser) {
  S.tab = tab;
  $('sheet').dataset.tab = tab;
  document.querySelectorAll('#tabs [role=tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  if (tab === 'chart') renderScatter();
  if (fromUser && isPhone() && $('sheet').dataset.snap === 'peek') setSnap('half');
}

function initSheet() {
  document.querySelectorAll('#tabs [role=tab]').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab, true)));
  $('tabs').addEventListener('keydown', e => {
    const tabs = [...document.querySelectorAll('#tabs [role=tab]')].filter(b => b.offsetParent), i = tabs.indexOf(document.activeElement);
    if (i < 0 || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]; n.focus(); setTab(n.dataset.tab, true);
  });
  matchMedia('(max-width: 700px)').addEventListener('change', () => { if (!isPhone() && S.tab === 'filters') setTab('rank'); setSnap($('sheet').dataset.snap); });
  addEventListener('resize', () => setSnap($('sheet').dataset.snap));
  setSnap('peek');
  // Uchwyt: klik przełącza punkty zatrzymania, przeciągnięcie przyciąga do najbliższego, strzałki działają z klawiatury.
  const grip = $('grip'), sh = $('sheet');
  let drag = null;
  grip.addEventListener('pointerdown', e => { drag = { y: e.clientY, h: sh.offsetHeight, moved: false }; grip.setPointerCapture(e.pointerId); sh.classList.add('dragging'); });
  grip.addEventListener('pointermove', e => {
    if (!drag) return;
    const dy = drag.y - e.clientY; if (Math.abs(dy) > 4) drag.moved = true;
    const h = Math.min(snapPx('full'), Math.max(snapPx('peek'), drag.h + dy));
    document.documentElement.style.setProperty('--sheet-h', h + 'px');
  });
  grip.addEventListener('pointerup', e => {
    if (!drag) return;
    sh.classList.remove('dragging');
    const h = sh.offsetHeight, was = drag.moved; drag = null;
    if (!was) { setSnap(SNAPS[(SNAPS.indexOf(sh.dataset.snap) + 1) % 3]); return; }
    setSnap(SNAPS.reduce((a, k) => (Math.abs(snapPx(k) - h) < Math.abs(snapPx(a) - h) ? k : a)));
  });
  grip.addEventListener('keydown', e => {
    const i = SNAPS.indexOf(sh.dataset.snap);
    if (e.key === 'ArrowUp' && i < 2) { e.preventDefault(); setSnap(SNAPS[i + 1]); }
    if (e.key === 'ArrowDown' && i > 0) { e.preventDefault(); setSnap(SNAPS[i - 1]); }
  });
  $('collapse').addEventListener('click', () => { document.body.classList.remove('results'); if (!isPhone()) map.panBy([-innerWidth / 4, 0]); });
  $('results-btn').addEventListener('click', () => {
    document.body.classList.add('results');
    if (isPhone()) setSnap('half'); else { renderRank(); renderScatter(); map.panBy([innerWidth / 4, 0]); }  // keep the office visible left of the panel
  });
  $('f-toggle').addEventListener('click', () => {
    const shut = $('tab-filters').classList.toggle('shut');
    $('f-toggle').setAttribute('aria-expanded', String(!shut)); $('f-toggle').textContent = shut ? 'rozwiń' : 'zwiń';
  });
}

function clearSel() {
  S.sel = null;
  if (selMarker) { selMarker.remove(); selMarker = null; }
}

function selectHood(i) {
  const h = S.hoods[i]; if (!h) return;
  S.sel = i;
  if (selMarker) selMarker.remove();
  selMarker = L.circleMarker([h.lat, h.lon], { radius: 16, color: '#ffd27a', weight: 3, fill: false, interactive: false })
    .bindTooltip(h.name, { permanent: true, direction: 'top', offset: [0, -16], className: 'hub-label' }).addTo(map);
  if (isPhone()) setSnap('peek');
  map.flyTo([h.lat, h.lon], Math.max(map.getZoom(), 14), { duration: 0.8 });
  renderRank();
}

function initSearch() {
  const dl = $('stop-list'), byLabel = new Map(), seen = new Map(), input = $('search');
  S.stops.forEach((s, i) => {
    const r = s.routes || [];
    let label = `${s.name} (${r.slice(0, 5).join(' · ')}${r.length > 5 ? ' …' : ''})`;
    const k = seen.get(label) || 0; seen.set(label, k + 1);
    if (k) label += ` #${k + 1}`;
    byLabel.set(label, i);
    const o = document.createElement('option'); o.value = label; dl.append(o);
  });
  const commit = () => {
    const q = input.value.trim(); if (!q || q === S.originName) { $('search-msg').textContent = ''; return; }
    let i = byLabel.get(q);
    if (i == null) {
      const ql = q.toLocaleLowerCase('pl'), hits = S.stops.map((s, j) => j).filter(j => S.stops[j].name.toLocaleLowerCase('pl') === ql);
      if (hits.length) i = hits.sort((a, b) => (S.stops[b].routes || []).length - (S.stops[a].routes || []).length)[0];
    }
    if (i == null) { $('search-msg').textContent = `Nie znaleziono przystanku „${q}”. Wybierz pozycję z listy.`; return; }
    $('search-msg').textContent = '';
    const s = S.stops[i];
    setOrigin(s.lat, s.lon, s.name);
    input.value = s.name; input.blur();
    map.setView([s.lat, s.lon], Math.max(map.getZoom(), 13));
  };
  input.addEventListener('change', commit);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') { input.value = S.originName; input.blur(); } });
  input.addEventListener('focus', () => input.select());
  input.addEventListener('blur', () => { setTimeout(() => { if (document.activeElement !== input) { input.value = S.originName; $('search-msg').textContent = ''; } }, 150); });
}

// Suwaki: wartość na końcu skali = brak limitu.
function setBudgetRange() {
  const b = $('budget'), ppm = S.metric === 'ppm';
  Object.assign(b, { min: ppm ? 30 : 1500, max: ppm ? 150 : 10000, step: ppm ? 5 : 250 });
  b.value = b.max; S.budget = null; syncOutputs();
}
function syncOutputs() {
  const b = $('budget'), t = $('maxt');
  $('budget-out').textContent = S.budget == null ? 'bez limitu' : `do ${fmtVal(S.budget, S.metric)}`;
  $('maxt-out').textContent = S.maxT == null ? 'bez limitu' : `do ${S.maxT} min`;
  b.setAttribute('aria-valuetext', $('budget-out').textContent); t.setAttribute('aria-valuetext', $('maxt-out').textContent);
}

function initFilters() {
  if (!S.sourceKeys.length) {
    document.querySelectorAll('[data-mode=rent],[data-mode=value]').forEach(b => { b.disabled = true; b.title = 'brak plików z ogłoszeniami'; });
    $('count').textContent = $('count-d').textContent = 'Brak plików z ogłoszeniami: tylko widok dojazdu.';
    return;
  }
  const press = (sel, key, val) => document.querySelectorAll(sel).forEach(x => x.setAttribute('aria-pressed', String(x.dataset[key] === val)));
  if (S.sourceKeys.length > 1) {
    $('source-row').innerHTML = S.sourceKeys.map(k => `<button type="button" class="seg" data-source="${esc(k)}" aria-pressed="false">${k === 'all' || k === 'merged' ? 'wszystkie' : esc(S.sources[k].label)}</button>`).join('');
    $('source-row').addEventListener('click', e => { const b = e.target.closest('[data-source]'); if (b) setSource(b.dataset.source); });
  } else $('source-fs').hidden = true;
  $('rooms').addEventListener('click', e => { const b = e.target.closest('[data-rooms]'); if (!b) return; S.rooms = b.dataset.rooms; press('[data-rooms]', 'rooms', S.rooms); refreshRent(); });
  const area = (id, key) => $(id).addEventListener('change', e => { const v = parseFloat(e.target.value); S[key] = Number.isFinite(v) && v > 0 ? v : null; refreshRent(); });
  area('area-min', 'areaMin'); area('area-max', 'areaMax');
  document.querySelectorAll('[data-metric]').forEach(b => b.addEventListener('click', () => {
    S.metric = b.dataset.metric; press('[data-metric]', 'metric', S.metric); setBudgetRange(); refreshRent();
  }));
  let raf = 0;
  const slide = () => { syncOutputs(); cancelAnimationFrame(raf); raf = requestAnimationFrame(updateView); };
  $('budget').addEventListener('input', e => { S.budget = +e.target.value >= +e.target.max ? null : +e.target.value; slide(); });
  $('maxt').addEventListener('input', e => { S.maxT = +e.target.value >= +e.target.max ? null : +e.target.value; slide(); });
  $('more-btn').addEventListener('click', () => {
    const open = $('more').hidden; $('more').hidden = !open;
    $('more-btn').setAttribute('aria-expanded', String(open)); $('more-btn').textContent = open ? 'mniej filtrów' : 'więcej filtrów';
  });
  $('reset').addEventListener('click', () => {
    S.rooms = 'any'; S.areaMin = S.areaMax = null; S.checked.clear(); S.maxT = null; $('maxt').value = $('maxt').max;
    $('area-min').value = $('area-max').value = ''; press('[data-rooms]', 'rooms', 'any'); setBudgetRange(); renderAmenities(); refreshRent();
  });
  document.querySelectorAll('[data-sort]').forEach(b => b.addEventListener('click', () => {
    S.sort = b.dataset.sort; press('[data-sort]', 'sort', S.sort); computeRank(); renderRank();
  }));
  $('rank-list').addEventListener('click', e => { const b = e.target.closest('[data-i]'); if (b) selectHood(+b.dataset.i); });
  const go = d => selectHood(+d.dataset.i);
  const tip = e => { const d = e.target.closest('.dot'); if (d) $('scatter-tip').textContent = d.getAttribute('aria-label'); };
  $('scatter').addEventListener('mouseover', tip);
  $('scatter').addEventListener('focusin', tip);
  $('scatter').addEventListener('click', e => { const d = e.target.closest('.dot'); if (d) go(d); });
  $('scatter').addEventListener('keydown', e => { const d = e.target.closest('.dot'); if (d && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); go(d); } });
  setBudgetRange();
}

function initUi() {
  document.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
  $('t-labels').addEventListener('change', renderLabels);
  ['metro', 'rail', 'tram', 'bus', 'hubs'].forEach(k => $('t-' + k).addEventListener('change', syncLineLayers));
  $('t-overlay').addEventListener('change', e => { e.target.checked ? overlay.addTo(map) : overlay.remove(); });
  const lb = $('layers-btn'), menu = $('layers-menu');
  const toggle = open => { menu.hidden = !open; lb.setAttribute('aria-expanded', String(open)); };
  lb.addEventListener('click', () => toggle(menu.hidden));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.hidden) { toggle(false); lb.focus(); } });
  document.addEventListener('pointerdown', e => { if (!menu.hidden && !e.target.closest('#layers')) toggle(false); });
  map.on('zoomend', () => { renderLabels(); syncLineLayers(); });
  map.on('moveend', renderLabels);
  map.on('click', e => setOrigin(e.latlng.lat, e.latlng.lng));
  map.on('mousemove', e => {
    const t = commuteAt(e.latlng.lat, e.latlng.lng), v = S.source ? rentAt(e.latlng.lat, e.latlng.lng) : null;
    const parts = [];
    if (t != null) parts.push(`≈ ${Math.round(t)} min`);
    if (v != null) parts.push(`${fmtVal(v, S.metric)} mediana w pobliżu`);
    $('readout').textContent = parts.join(' · ');
  });
  map.getContainer().addEventListener('mouseleave', () => { $('readout').textContent = ''; });
  initSheet();
}

async function main() {
  initMap();
  try {
    await loadAll();
  } catch (err) {
    $('count').textContent = `Nie udało się wczytać danych: ${err.message}`;
    return;
  }
  buildMapLayers();
  initSearch();
  initFilters();
  initUi();
  const saved = lsGet(LS_SOURCE);
  if (S.sourceKeys.length) {
    S.source = S.sourceKeys.includes(saved) ? saved : S.sourceKeys[0];
    S.mode = 'value';
    document.querySelectorAll('[data-source]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.source === S.source)));
    renderAmenities();
    applyFilter();
    renderCount();
  } else {
    S.mode = 'commute';
  }
  document.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === S.mode)));
  const savedOrigin = resolveOrigin(), o = savedOrigin || DEFAULT_ORIGIN;
  setOrigin(o[0], o[1], o === DEFAULT_ORIGIN && !location.hash ? 'Rondo ONZ' : undefined);
  // First visit: only the question 'Gdzie pracujesz?'; everything else appears once a place is picked.
  if (!savedOrigin) { document.body.classList.add('start'); $('search').value = ''; $('search').focus(); }
  window.__cmw = S; window.__cmwMap = map;
}

main();
