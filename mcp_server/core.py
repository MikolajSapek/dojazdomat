"""Dojazdomat query logic. Aggregates only: no listing row or listing coordinate ever leaves this module."""
import json
import math
import os
import unicodedata
from collections import OrderedDict
from pathlib import Path
from statistics import median

import numpy as np

from api import engine as eng
from pipeline import city

C = city.load()
EN = C["name"]["en"]

MEDIAN_MIN_N = eng.K["medianMinN"]  # owner decision D2: the one smallest group a price is shown from (web/rent.js, web/app.js, api)
MIN_GROUP = HOOD_MIN = RANK_MIN_N = MEDIAN_MIN_N  # old names, one value: rent_stats / hood median / ranking
IQR_MIN_N = 10       # p25 / p75 are an interpolation between two listings below this: publish the rounded median only
FEE_KNOWN = eng.K["feeKnownBit"]  # median rows: prec < 2 and the full cost known (web/rent.js inMedian)
WAGE_PER_MIN = eng.K["cost"]["wagePerHour"] / 60
TRIPS_PER_MONTH = eng.K["cost"]["tripsPerMonth"]
SORTS = ("value", "best", "commute", "rent")
PIN_CAP = eng.K["stackedPin"] - 1  # most rows of one identical pin (5 decimals) a hood median takes; web/rent.js computeHoodMed
HOOD_R = eng.K["hoodR"]  # metres; web/model-constants.json is the one source (web/app.js reads it too)
ROUND_PLN, SNAP_DEG = 50, 0.001   # output rounding; query-centre grid (~111 m)
BIN_M, BIN_MIN, TERCILE_MIN = eng.K["bins"]["binM"], MEDIAN_MIN_N, eng.K["bins"]["tercileMin"]  # a heatmap bin: its 3x3 block counts against the same minimum
BANDS = (*eng.K["bandMax"], math.inf)
RADIUS_STEPS = (300, 500, 750, 1000, 1500, 2000, 3000, 5000)  # rent_stats snaps to these: no free radius to difference
RADIUS = (200, RADIUS_STEPS[-1])
NEAR_STOPS = 3  # nearest stops listed per endpoint
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "web" / "data"
MODES = ("transit", "bike", "walk", "car")
HISTORY_FILE = "rent-history.json"  # pipeline/rent_history.py: one snapshot per day of offers.json
MODEL = {"transit": "door-to-door, transit + walk, weekday 07:00-09:00 mean, home -> office, ~110 m grid cells",
         "bike": "OpenStreetMap bike network, speed by road class, +2 min to unlock and park, home -> office",
         "walk": "OpenStreetMap footpath network, 4.8 km/h, home -> office",
         "car": "OpenStreetMap road network, morning rush-hour speeds, driving only (no parking), home -> office"}
CAVEATS = [
    "Rent = monthly total: rent plus czynsz administracyjny where the ad states it separately (asking prices, not signed leases).",
    "Czynsz (rent) zawiera czynsz administracyjny tylko gdy ogloszenie podaje go osobno; ceny ofertowe, nie transakcyjne.",
    "Cross-portal duplicates are merged one-to-one (same rooms, area within max(2 m2, 3 %), <= 300 m, price within 2 % or within the admin-fee gap); a flat posted on three or more portals may still count more than once.",
    "Medians use only ads whose price is the full monthly cost (rent + czynsz administracyjny) and whose pin is precise; n counts those ads.",
    "Rent figures are rounded to 50 PLN (1 PLN/m2 for per_m2), the query centre is snapped to a ~100 m grid and rent_stats radius to a fixed step (300, 500, 750, 1000, 1500, 2000, 3000, 5000 m) to protect individual ads.",
    "Listings with coordPrecision 2 (district centroid only) are excluded from every statistic.",
    "Commute = minutes from home to the office. transit: mean door-to-door by public transport plus walking, weekday departures 07:00-09:00 (ZTM, SKM, metro, KM, WKD GTFS); bike and walk: OpenStreetMap paths (bike +2 min to unlock and park); car: rush-hour driving on the OSM road network, no parking time. Same minutes as the map.",
    f"A price is published only from groups of at least {MEDIAN_MIN_N} listings (map, ranking, API and this server alike); the p25-p75 range only from {IQR_MIN_N}.",
    "A neighborhood median takes exactly the ads whose pin lies inside the neighborhood's boundary (the city's MSI areas; a suburban town: its gmina), so it never mixes in a flat from the next area. rent_stats is a plain circle around a point and ignores boundaries.",
    "Inputs are data/offers.json, the same file the map reads: portals older than 7 days, price outliers and cross-portal duplicates are already removed, and pins shared by 12 or more ads count as district-level (excluded).",
]


def _fold(s):
    s = unicodedata.normalize("NFKD", s.casefold().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _check_point(lat, lon, name=""):
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))) or math.isnan(lat) or math.isnan(lon):
        raise ValueError(f"{name}lat/lon must be numbers")
    if not eng.in_bbox(lat, lon):
        raise ValueError(f"{name}point outside {EN} bbox (lat {eng.LAT0}-{eng.LAT1}, lon {eng.LON0}-{eng.LON1})")


def _check_filters(rooms, min_area, max_area, measure):
    if measure not in ("total", "per_m2"):
        raise ValueError("measure must be 'total' or 'per_m2'")
    if rooms is not None and rooms not in (1, 2, 3, 4):
        raise ValueError("rooms must be 1, 2, 3 or 4 (4 = 4+)")
    for a in (min_area, max_area):
        if a is not None and not 0 <= a <= 1000:
            raise ValueError("area must be between 0 and 1000 m2")
    if min_area is not None and max_area is not None and min_area > max_area:
        raise ValueError("min_area must be <= max_area")


def load_offers(data_dir):
    """-> (median rows [lat, lon, price, rooms, area, flags, prec, dist, hood], offers asOf, per-portal info dict, district names,
    hood names). dist indexes the district names (merge_listings.py: the 18 Warsaw districts, then "Okolice"); hood indexes the
    named areas (pipeline/walk/areas.py), -1 = none.

    offers.json is what the map loads (pipeline/merge_listings.py): quality rules, stale portals and duplicates are applied
    there. The rows kept here follow the map's one median rule (web/rent.js inMedian): prec < 2 and the feeKnownBit set.
    """
    d = Path(data_dir)
    try:
        o = json.loads((d / "offers.json").read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"{d / 'offers.json'} missing: run pipeline/merge_listings.py (refresh.sh does)") from None
    if not all(isinstance(o.get(k), list) for k in ("dist", "districts", "hood", "hoods")):
        raise ValueError(f"{d / 'offers.json'} has no dist/districts/hood/hoods columns: rerun pipeline/merge_listings.py")
    cols = [o[c] for c in ("lat", "lon", "price", "rooms", "area", "flags", "prec", "dist", "hood")]
    rows = [list(r) for r in zip(*cols) if r[6] < 2 and int(r[5]) & FEE_KNOWN]
    return rows, o.get("asOf"), o.get("portals", {}), o["districts"], o["hoods"]


def _round(x, measure):
    """Rounded output: with small n a quantile is an exact listing price."""
    return round(x) if measure == "per_m2" else round(x / ROUND_PLN) * ROUND_PLN


def _snap(x):
    return round(x / SNAP_DEG) * SNAP_DEG


def _pct(a, q):
    return float(np.percentile(a, q))


def _stats(vals, measure):
    """vals ndarray (>= MIN_GROUP) -> dict, rounded for output. p25 / p75 only from IQR_MIN_N values: with fewer they sit between two ads."""
    iqr = len(vals) >= IQR_MIN_N
    return {"n": int(len(vals)), "median": _round(float(np.median(vals)), measure),
            "p25": _round(_pct(vals, 25), measure) if iqr else None, "p75": _round(_pct(vals, 75), measure) if iqr else None, "suppressed": False}


class Dojazdomat:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get("DOJAZDOMAT_DATA") or DEFAULT_DATA)
        stops, matrix = eng.read_files(self.data_dir)
        self.stops = stops
        self.engine = eng.Engine(stops, matrix, eng.read_walk(self.data_dir))
        self.hoods = json.loads((self.data_dir / "neighborhoods.json").read_text())
        rows, self.offers_as_of, self.portals, self.districts, self.hood_names = load_offers(self.data_dir)
        self._set_rows(rows)
        self._stop_fold = [_fold(s["name"]) for s in stops]
        self._hood_fold = [_fold(h["name"]) for h in self.hoods]
        self._grids = OrderedDict()
        self._routers = {}

    def _set_rows(self, rows):
        a = np.array(rows, dtype=np.float64).reshape(-1, 9)
        self.dist = a[:, 7].astype(int)
        self.hood = a[:, 8].astype(int)
        self.lat, self.lon = a[:, 0], a[:, 1]
        self.pin = np.floor(self.lat * 1e5 + 0.5).astype(np.int64) * 10_000_000 + np.floor(self.lon * 1e5 + 0.5).astype(np.int64)  # 5-decimal pin id, as web/rent.js
        self.price, self.rooms, self.area = a[:, 2], a[:, 3], a[:, 4]

    # ---------- helpers ----------
    def _grid(self, lat, lon, mode="transit"):
        """uint8 ROWS x COLS minutes from every cell (home) to lat/lon (office), 255 = unreached. Same minutes as the map
        (web/app.js computeCommute): transit from the stop matrix; bike/walk/car from the OSM graphs (api/car.py, as /api/<mode>), bike
        plus addMin; a walk/bike cell outside the graph and an office off every graph fall back to the straight-line formula,
        as the map does when /api/<mode> has no answer."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        key = (mode, round(lat, 5), round(lon, 5))
        if key in self._grids:
            self._grids.move_to_end(key)
        else:
            self._grids[key] = self.engine.grid(lat, lon) if mode == "transit" else self._routed_grid(lat, lon, mode)
            if len(self._grids) > 16:
                self._grids.popitem(last=False)
        return self._grids[key]

    def _router(self, mode):
        if mode not in self._routers:  # lazy: a graph loads on its first query (walk ~8 MB npz)
            from api import car as carmod  # scipy: only the MCP/API images have it, the pipeline image (rent_history.py) does not
            kw = {"office_leg": True} if mode == "car" else dict(access_mpm=eng.K[mode]["mPerMin"], access_cap_m=800.0, snap_cap_m=600.0, office_leg=True)
            self._routers[mode] = carmod.load(self.data_dir, mode, **kw)  # same settings as api/main.py
        return self._routers[mode]

    def _routed_grid(self, lat, lon, mode):
        r = self._router(mode)
        g = r.grid(lat, lon) if r is not None else None
        add = eng.K["bike"]["addMin"] if mode == "bike" else 0
        d = np.hypot((eng._cell_lat[:, None] - lat) * eng.M_LAT, (eng._cell_lon[None, :] - lon) * eng.M_LON)
        if mode == "car":
            straight = d / (eng.K["car"]["mPerHour"] / 60 / eng.K["car"]["circuity"])
        else:
            straight = eng.net_min(d, eng.BIKE_NET if mode == "bike" else eng.WALK_NET) + add
        if g is None:
            t = straight
        else:
            t = np.where(g == eng.UNREACHED, np.inf if mode == "car" else straight, g.astype(float) + add)
        out = np.full(t.shape, eng.UNREACHED, np.uint8)
        ok = np.isfinite(t) & (t <= eng.STOP_CUT)
        out[ok] = np.floor(t[ok] + 0.5).astype(np.uint8)
        return out

    def _minutes_grid(self, lat, lon, mode):
        g = self._grid(lat, lon, mode).astype(float)
        g[g == eng.UNREACHED] = np.nan
        return g

    def _hood_by_name(self, name):
        f = _fold(name.strip())
        for h, hf in zip(self.hoods, self._hood_fold):
            if hf == f:
                return h
        hits = [h for h, hf in zip(self.hoods, self._hood_fold) if f and f in hf]
        if len(hits) == 1:
            return hits[0]
        raise ValueError(f"unknown neighborhood {name!r}" + (f"; did you mean: {', '.join(h['name'] for h in hits[:5])}" if hits else "; use find_place"))

    def _hood_stats(self, h, mask, vals, measure):
        v = vals[self.hood_mask(h, mask)]
        if len(v) < MEDIAN_MIN_N:
            return {"n": None, "median": None, "p25": None, "p75": None, "suppressed": True}
        return _stats(v, measure)

    def _mask(self, rooms, min_area, max_area, measure):
        m = np.ones(len(self.lat), bool)
        if rooms is not None:
            m &= (self.rooms >= 4) if rooms == 4 else (self.rooms == rooms)  # 4 = 4+, as roomsOk in web/rent.js
        if min_area is not None:
            m &= self.area >= min_area
        if max_area is not None:
            m &= self.area <= max_area
        if min_area is not None or max_area is not None or measure == "per_m2":
            m &= self.area > 0
        return m

    def _values(self, measure):
        return self.price / np.where(self.area > 0, self.area, np.nan) if measure == "per_m2" else self.price

    def _dist(self, lat, lon):
        return np.hypot((self.lat - lat) * eng.M_LAT, (self.lon - lon) * eng.M_LON)

    def hood_near(self, h):
        """Median rows of neighbourhood h: the rows whose pin lies in its area (offers.json hood: MSI osiedle or the town's gmina,
        pipeline/walk/areas.py). One rule with web/rent.js computeHoodMed. A place without an area has no rows."""
        i = self.hood_names.index(h["name"]) if h["name"] in self.hood_names else -2
        return self.hood == i

    def hood_mask(self, h, mask):
        """The rows a hood median takes: mask and hood_near, at most PIN_CAP of each identical 5-decimal pin, the first ones in
        file order (one building must not set a median; a street-confirmed pin shared by many ads stays precise). Same rule as
        web/rent.js computeHoodMed."""
        m = mask & self.hood_near(h)
        idx = np.flatnonzero(m)
        if len(idx) <= PIN_CAP:
            return m
        order = np.argsort(self.pin[idx], kind="stable")  # stable: file order inside a pin
        p = self.pin[idx][order]
        start = np.r_[0, np.flatnonzero(p[1:] != p[:-1]) + 1]
        rank = np.arange(len(p)) - np.repeat(start, np.diff(np.r_[start, len(p)]))
        out = np.zeros(len(m), bool)
        out[idx[order[rank < PIN_CAP]]] = True
        return out

    def _near_stops(self, lat, lon):
        d = np.hypot((self.engine.slat - lat) * eng.M_LAT, (self.engine.slon - lon) * eng.M_LON)
        return [{"name": self.stops[i]["name"], "modes": self.stops[i].get("modes", []), "walk_min": round(float(eng.net_min(d[i])), 1)}
                for i in np.argsort(d)[:NEAR_STOPS]]

    # ---------- tools ----------
    def find_place(self, query, limit=10):
        q = _fold(query.strip())
        if len(q) < 2 or len(query) > 60:
            raise ValueError("query must be 2-60 characters")
        limit = max(1, min(int(limit), 25))

        def rank(folded):
            return 0 if folded == q else 1 if folded.startswith(q) else 2

        def pick(items, folds):
            hits = sorted(((rank(f), len(f), i) for i, f in enumerate(folds) if q in f))
            return [items[i] for _, _, i in hits[:limit]]
        return {"query": query,
                "stops": [{"name": s["name"], "lat": s["lat"], "lon": s["lon"], "modes": s.get("modes", []),
                           "routes": s.get("routes", [])[:8]} for s in pick(self.stops, self._stop_fold)],
                "neighborhoods": [{"name": h["name"], "district": h["district"], "lat": h["lat"], "lon": h["lon"]}
                                  for h in pick(self.hoods, self._hood_fold)]}

    def commute_time(self, from_lat, from_lon, to_lat, to_lon, mode="transit"):
        """from = home, to = office: the grid is built at the office and read at the home cell, as the map does."""
        _check_point(from_lat, from_lon, "from_"), _check_point(to_lat, to_lon, "to_")
        cell = eng.cell_of(from_lat, from_lon)
        v = int(self._grid(to_lat, to_lon, mode)[cell]) if cell else eng.UNREACHED
        out = {"minutes": None if v == eng.UNREACHED else v, "reachable": v != eng.UNREACHED, "mode": mode,
               "model": MODEL[mode]}
        if mode == "transit":
            out["origin_stops"] = self._near_stops(from_lat, from_lon)
            out["destination_stops"] = self._near_stops(to_lat, to_lon)
        return out

    def rent_stats(self, lat, lon, radius_m=800, rooms=None, min_area=None, max_area=None, measure="total"):
        # A free circle around any point, not a neighbourhood median: it has no district, so it ignores district lines (use
        # rank_neighborhoods for the boundary-respecting neighbourhood medians). Deliberate: the runtime has no polygons.
        _check_point(lat, lon)
        # Differencing guard: the centre sits on a ~111 m grid AND the radius on a fixed step, so the set of circles a client can
        # ask for is finite and each is a published figure; the grid alone does not stop r=768/770/772 from isolating one ad.
        lat, lon = _snap(lat), _snap(lon)
        if not RADIUS[0] <= radius_m <= RADIUS[1]:
            raise ValueError(f"radius_m must be {RADIUS[0]}-{RADIUS[1]}")
        radius_m = min(RADIUS_STEPS, key=lambda s: (abs(s - radius_m), s))
        _check_filters(rooms, min_area, max_area, measure)
        m = self._mask(rooms, min_area, max_area, measure) & (self._dist(lat, lon) <= radius_m)
        vals = self._values(measure)[m]
        base = {"center": {"lat": round(lat, 3), "lon": round(lon, 3)}, "radius_m": radius_m, "measure": measure,
                "unit": "PLN/m2/month" if measure == "per_m2" else "PLN/month"}
        if len(vals) < MIN_GROUP:
            return {**base, "n": None, "median": None, "p25": None, "p75": None, "suppressed": True,
                    "note": f"fewer than {MIN_GROUP} matching listings: widen radius or relax filters"}
        return {**base, **_stats(vals, measure)}

    def _cell_rent(self, mask, vals):
        """Rent per grid cell: 350 m bins, 3x3 block median when block holds >= 3 listings (web/app.js buildBins)."""
        cos = eng.M_LON
        by = np.floor((self.lat[mask] - eng.LAT0) * eng.M_LAT / BIN_M).astype(int)
        bx = np.floor((self.lon[mask] - eng.LON0) * cos / BIN_M).astype(int)
        br, bc = int(np.ceil(eng.ROWS * eng.DLAT * eng.M_LAT / BIN_M)), int(np.ceil(eng.COLS * eng.DLON * cos / BIN_M))
        ok = (by >= 0) & (bx >= 0) & (by < br) & (bx < bc)
        lists = {}
        for y, x, v in zip(by[ok], bx[ok], vals[mask][ok]):
            lists.setdefault((y, x), []).append(v)
        binv = np.full((br, bc), np.nan)
        for y, x in {(y + dy, x + dx) for y, x in lists for dy in (-1, 0, 1) for dx in (-1, 0, 1)}:
            if 0 <= y < br and 0 <= x < bc:
                block = [v for dy in (-1, 0, 1) for dx in (-1, 0, 1) for v in lists.get((y + dy, x + dx), ())]
                if len(block) >= BIN_MIN:
                    binv[y, x] = median(block)
        clat = eng.LAT0 + (np.arange(eng.ROWS) + 0.5) * eng.DLAT
        clon = eng.LON0 + (np.arange(eng.COLS) + 0.5) * eng.DLON
        ry = np.clip(np.floor((clat - eng.LAT0) * eng.M_LAT / BIN_M).astype(int), 0, br - 1)
        rx = np.clip(np.floor((clon - eng.LON0) * cos / BIN_M).astype(int), 0, bc - 1)
        return binv[ry[:, None], rx[None, :]]

    def rank_neighborhoods(self, office_lat, office_lon, max_commute_min=None, max_rent=None, rooms=None,
                           measure="total", limit=10, sort="best", mode="transit"):
        """Same inputs and rules as the map (web/app.js computeRank): median and rank from >= medianMinN ads within hoodR (all in the
        neighbourhood's own district), rooms filter, max_rent on the published (50 PLN rounded) median, max_commute_min on whole minutes. sort: best (default, the map's order: rent + commute
        valued at the minimum wage), value (this tool's band score), commute, rent."""
        _check_point(office_lat, office_lon, "office_")
        _check_filters(rooms, None, None, measure)
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}")
        if sort == "best" and measure != "total":
            raise ValueError("sort=best needs measure=total (commute cost is in PLN/month)")
        for name, v in (("max_commute_min", max_commute_min), ("max_rent", max_rent)):
            if v is not None and not v > 0:
                raise ValueError(f"{name} must be positive")
        limit = max(1, min(int(limit), 50))
        grid = self._minutes_grid(office_lat, office_lon, mode)
        mask = self._mask(rooms, None, None, measure)
        vals = self._values(measure)
        cell_rent = self._cell_rent(mask, vals)
        band = np.digitize(grid, BANDS[:-1], right=True)  # band index per cell, as bandOf (t <= max)
        terc, bmed = {}, {}
        for b in range(len(BANDS)):
            r = cell_rent[(band == b) & ~np.isnan(grid) & ~np.isnan(cell_rent)]
            if len(r) >= TERCILE_MIN:
                s = np.sort(r)
                terc[b] = (s[len(s) // 3], s[2 * len(s) // 3])
                bmed[b] = float(np.median(r))
        raw = []  # (hood, n, median, p25, p75, commute, score, tier), unrounded
        for h in self.hoods:
            m = self.hood_mask(h, mask)
            v = vals[m]
            cell = eng.cell_of(h["lat"], h["lon"])
            t = grid[cell] if cell else np.nan
            if len(v) < MEDIAN_MIN_N or np.isnan(t):
                continue
            med, b = float(np.median(v)), int(band[cell])
            tier = score = None
            if b in terc:
                tier = "cheap" if med <= terc[b][0] else "typical" if med <= terc[b][1] else "expensive"
                score = round(100 * (1 - med / bmed[b]))
            raw.append((h, int(len(v)), med, _pct(v, 25), _pct(v, 75), int(t), score, tier))
        out = []
        for h, n, med, p25, p75, t, score, tier in raw:
            rm = _round(med, measure)  # pareto on the published (rounded) rent, so a client can verify it
            dom = any(q[5] <= t and _round(q[2], measure) <= rm and (q[5] < t or _round(q[2], measure) < rm) for q in raw)
            cost = med + round(t * TRIPS_PER_MONTH * WAGE_PER_MIN) if measure == "total" else None
            out.append(({"name": h["name"], "district": h["district"], "lat": h["lat"], "lon": h["lon"],
                         "n": n, "median_rent": _round(med, measure), "p25": _round(p25, measure),
                         "p75": _round(p75, measure), "commute_min": t, "value_score": score,
                         "value_tier": tier, "pareto": not dom, "monthly_cost": None if cost is None else _round(cost, measure),
                         "measure": measure}, med, cost))
        sel = [x for x in out if (max_commute_min is None or x[0]["commute_min"] <= max_commute_min)
               and (max_rent is None or x[0]["median_rent"] <= max_rent)]  # rounded: a bisect on max_rent resolves only the published median
        key = {"value": lambda x: (x[0]["value_score"] is None, -(x[0]["value_score"] or 0), x[0]["commute_min"]),
               "best": lambda x: x[2], "commute": lambda x: x[0]["commute_min"], "rent": lambda x: x[1]}[sort]
        sel.sort(key=key)  # stable: ties keep neighborhoods.json order, as the map does
        return {"office": {"lat": office_lat, "lon": office_lon}, "mode": mode, "measure": measure, "sort": sort,
                "unit": "PLN/m2/month" if measure == "per_m2" else "PLN/month",
                "value_score": "100*(1 - median/typical median of grid cells in the same commute band); positive = cheaper than typical",
                "monthly_cost": "median rent + commute valued at the minimum wage (42 trips/month), the map's default order; total measure only",
                "pareto": "true = no other eligible neighborhood is both faster and cheaper (computed before max_* filters)",
                "eligible": len(out), "returned": min(limit, len(sel)), "neighborhoods": [x[0] for x in sel[:limit]]}

    def rank_for_two(self, a_lat, a_lon, b_lat, b_lon, mode_a="transit", mode_b="transit", max_commute_min=None,
                     max_rent=None, rooms=None, limit=10, sort="best"):
        """Two workplaces (a couple): every neighborhood with >= medianMinN ads and both commutes. best = median rent + both commutes
        valued at the minimum wage (42 trips/month each), the map's cost rule twice; fair = the longer of the two commutes first;
        rent = cheapest. max_commute_min applies to each person."""
        _check_point(a_lat, a_lon, "a_"), _check_point(b_lat, b_lon, "b_")
        _check_filters(rooms, None, None, "total")
        if sort not in ("best", "fair", "rent"):
            raise ValueError("sort must be best, fair or rent")
        for name, v in (("max_commute_min", max_commute_min), ("max_rent", max_rent)):
            if v is not None and not v > 0:
                raise ValueError(f"{name} must be positive")
        limit = max(1, min(int(limit), 50))
        ga, gb = self._minutes_grid(a_lat, a_lon, mode_a), self._minutes_grid(b_lat, b_lon, mode_b)
        mask, vals = self._mask(rooms, None, None, "total"), self._values("total")
        out = []
        for h in self.hoods:
            cell = eng.cell_of(h["lat"], h["lon"])
            if not cell or np.isnan(ga[cell]) or np.isnan(gb[cell]):
                continue
            v = vals[self.hood_mask(h, mask)]
            if len(v) < MEDIAN_MIN_N:
                continue
            ta, tb, med = int(ga[cell]), int(gb[cell]), float(np.median(v))
            cost = med + round((ta + tb) * TRIPS_PER_MONTH * WAGE_PER_MIN)
            out.append(({"name": h["name"], "district": h["district"], "lat": h["lat"], "lon": h["lon"], "n": int(len(v)),
                         "median_rent": _round(med, "total"), "commute_a_min": ta, "commute_b_min": tb,
                         "monthly_cost": _round(cost, "total")}, med, cost))
        sel = [x for x in out if (max_commute_min is None or max(x[0]["commute_a_min"], x[0]["commute_b_min"]) <= max_commute_min)
               and (max_rent is None or x[0]["median_rent"] <= max_rent)]
        key = {"best": lambda x: x[2], "rent": lambda x: x[1],
               "fair": lambda x: (max(x[0]["commute_a_min"], x[0]["commute_b_min"]), x[2])}[sort]
        sel.sort(key=key)
        return {"office_a": {"lat": a_lat, "lon": a_lon, "mode": mode_a}, "office_b": {"lat": b_lat, "lon": b_lon, "mode": mode_b},
                "sort": sort, "unit": "PLN/month",
                "monthly_cost": "median rent + both commutes valued at the minimum wage (42 trips/month each)",
                "eligible": len(out), "returned": min(limit, len(sel)), "neighborhoods": [x[0] for x in sel[:limit]]}

    def compare_neighborhoods(self, names, rooms=None, measure="total", office_lat=None, office_lon=None, mode="transit"):
        """2-4 neighborhoods side by side: median, p25, p75, n (suppressed below medianMinN) and, with an office, the commute."""
        if not 2 <= len(names) <= 4:
            raise ValueError("give 2-4 neighborhood names")
        _check_filters(rooms, None, None, measure)
        hoods = [self._hood_by_name(n) for n in names]
        grid = None
        if office_lat is not None or office_lon is not None:
            _check_point(office_lat, office_lon, "office_")
            grid = self._minutes_grid(office_lat, office_lon, mode)
        mask, vals = self._mask(rooms, None, None, measure), self._values(measure)
        rows = []
        for h in hoods:
            r = {"name": h["name"], "district": h["district"], "lat": h["lat"], "lon": h["lon"], **self._hood_stats(h, mask, vals, measure)}
            if grid is not None:
                cell = eng.cell_of(h["lat"], h["lon"])
                t = grid[cell] if cell else np.nan
                r["commute_min"] = None if np.isnan(t) else int(t)
            rows.append(r)
        return {"measure": measure, "unit": "PLN/m2/month" if measure == "per_m2" else "PLN/month", "rooms": rooms,
                "mode": mode if grid is not None else None, "neighborhoods": rows}

    def reachable_neighborhoods(self, lat, lon, max_minutes=30, mode="transit", rooms=None, limit=50):
        """Neighborhoods whose centre is within max_minutes of lat/lon (home -> that point), fastest first. Unlike
        rank_neighborhoods it lists every neighborhood in reach, also those with too few ads for a price (median null)."""
        _check_point(lat, lon)
        _check_filters(rooms, None, None, "total")
        if not 1 <= max_minutes <= 120:
            raise ValueError("max_minutes must be 1-120")
        limit = max(1, min(int(limit), 100))
        grid = self._minutes_grid(lat, lon, mode)
        mask, vals = self._mask(rooms, None, None, "total"), self._values("total")
        hits = []
        for h in self.hoods:
            cell = eng.cell_of(h["lat"], h["lon"])
            t = grid[cell] if cell else np.nan
            if np.isnan(t) or t > max_minutes:
                continue
            st = self._hood_stats(h, mask, vals, "total")
            hits.append({"name": h["name"], "district": h["district"], "lat": h["lat"], "lon": h["lon"], "commute_min": int(t),
                         "median_rent": st["median"], "n": st["n"]})
        hits.sort(key=lambda x: (x["commute_min"], x["name"]))
        return {"point": {"lat": lat, "lon": lon}, "mode": mode, "max_minutes": max_minutes, "unit": "PLN/month",
                "found": len(hits), "returned": min(limit, len(hits)), "neighborhoods": hits[:limit]}

    def rent_trend(self, area=None, rooms=None):
        """Median asking rent per day of data for the whole city, a district or a neighborhood (pipeline/rent_history.py), with the
        change from the first to the last day. Days with fewer than medianMinN ads show null."""
        if rooms is not None and rooms not in (1, 2, 3, 4):
            raise ValueError("rooms must be 1, 2, 3 or 4 (4 = 4+)")
        p = self.data_dir / HISTORY_FILE
        if not p.exists():
            raise ValueError("rent history not built yet")
        hist = json.loads(p.read_text())
        f = _fold((area or C["name"]["nom"]).strip())
        scope = next((k for k in ("city", "district", "hood") for name in hist["names"][k] if _fold(name) == f), None)
        if scope is None:
            hits = [n for k in ("district", "hood") for n in hist["names"][k] if f and f in _fold(n)]
            raise ValueError(f"unknown area {area!r}" + (f"; did you mean: {', '.join(hits[:5])}" if hits else f"; give {C['name']['nom']}, a district or a neighborhood"))
        name = next(n for n in hist["names"][scope] if _fold(n) == f)
        rk = "all" if rooms is None else str(rooms)
        series = [{"date": d["date"], **(d[scope].get(name, {}).get(rk) or {"median": None, "n": None})} for d in hist["days"]]
        known = [s for s in series if s["median"] is not None]
        change = None
        if len(known) >= 2:
            a, b = known[0], known[-1]
            change = {"from": a["date"], "to": b["date"], "pln": b["median"] - a["median"], "pct": round(100 * (b["median"] / a["median"] - 1), 1)}
        return {"area": name, "scope": scope, "rooms": rooms, "unit": "PLN/month", "series": series, "change": change,
                "note": f"daily snapshots of the map's data since {hist['days'][0]['date'] if hist['days'] else '-'}; medians from >= {MEDIAN_MIN_N} ads, rounded to {ROUND_PLN} PLN; asking prices, short series: read small changes as noise"}

    def dataset_info(self):
        return {"asOf": self.offers_as_of,
                "portals": self.portals,
                "listings_used": int(len(self.lat)),
                "transit": {"stop_groups": len(self.stops), "walk": {**{k: self.engine.walk[k] for k in ("stopWalkCap", "originWalkCap")},
                                     "networkMPerMin": eng.WALK_NET["mPerMin"], "circuityByDistanceM": eng.WALK_NET["circuity"]}},
                "neighborhoods": len(self.hoods),
                "bbox": {"lat": [eng.LAT0, eng.LAT1], "lon": [eng.LON0, eng.LON1]},
                "privacy": f"Only aggregates from groups of >= {MIN_GROUP} listings; no listing rows or listing coordinates are exposed.",
                "caveats": CAVEATS}


def snapshot(offers_dir, hoods):
    """One day of rent history from an offers.json dir: medians (rounded, n >= medianMinN) for the city, each district and each
    neighborhood, all rooms and 1/2/3/4+, with the same rows and pin cap as the map and rank_neighborhoods."""
    rows, as_of, _, districts, hood_names = load_offers(offers_dir)
    s = Dojazdomat.__new__(Dojazdomat)
    s.hood_names, s.districts, s.hoods = hood_names, districts, hoods
    s._set_rows(rows)
    vals = s._values("total")

    def med(m):
        v = vals[m]
        return {"median": _round(float(np.median(v)), "total"), "n": int(len(v))} if len(v) >= MEDIAN_MIN_N else None

    def by_rooms(base):
        return {"all" if r is None else str(r): med(base & s._mask(r, None, None, "total")) for r in (None, 1, 2, 3, 4)}
    allm = np.ones(len(s.lat), bool)
    out = {"date": as_of, "city": {C["name"]["nom"]: by_rooms(allm)},
           "district": {d: by_rooms(s.dist == i) for i, d in enumerate(districts)},
           "hood": {}}
    for h in hoods:
        r = {"all" if k is None else str(k): med(s.hood_mask(h, s._mask(k, None, None, "total"))) for k in (None, 1, 2, 3, 4)}
        if any(r.values()):
            out["hood"][h["name"]] = r
    return out
