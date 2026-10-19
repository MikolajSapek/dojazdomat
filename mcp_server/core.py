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

MIN_GROUP = 5        # smallest group any statistic is computed from
HOOD_MIN = 10        # smallest neighborhood ranked
HOOD_R = 900         # metres, as web/app.js
BIN_M, BIN_MIN, TERCILE_MIN = 350, 3, 6
BANDS = (15, 25, 35, 45, 60, math.inf)
RADIUS = (200, 5000)
NEAR_STOPS = 3  # nearest stops listed per endpoint
DEFAULT_DATA = Path(__file__).resolve().parents[1] / "web" / "data"
CAVEATS = [
    "Rent = monthly total: rent plus czynsz administracyjny where the ad states it separately (asking prices, not signed leases).",
    "Czynsz (rent) zawiera czynsz administracyjny tylko gdy ogloszenie podaje go osobno; ceny ofertowe, nie transakcyjne.",
    "Morizon overlaps Gratka (same inventory); portals are not deduplicated against each other, so some flats count twice.",
    "Listings with coordPrecision 2 (district centroid only) are excluded from every statistic.",
    "Commute = mean door-to-door minutes by public transport plus walking, weekday departures 07:00-09:00 (ZTM, SKM, metro, KM, WKD GTFS); no bike or car.",
    "Statistics are published only for groups of at least 5 listings; neighborhoods need at least 10.",
]


def _fold(s):
    s = unicodedata.normalize("NFKD", s.casefold().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _check_point(lat, lon, name=""):
    if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))) or math.isnan(lat) or math.isnan(lon):
        raise ValueError(f"{name}lat/lon must be numbers")
    if not eng.in_bbox(lat, lon):
        raise ValueError(f"{name}point outside Warsaw bbox (lat {eng.LAT0}-{eng.LAT1}, lon {eng.LON0}-{eng.LON1})")


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


def load_listings(data_dir):
    """-> (rows [lat, lon, price, rooms, area, flags, prec] with prec < 2, per-portal info dict)."""
    d = Path(data_dir)
    files = [d / "listings.json"] if (d / "listings.json").exists() else sorted(d.glob("listings-*.json"))
    rows, info = [], {}
    for f in files:
        j = json.loads(f.read_text())
        kept = [r for r in j["listings"] if r[6] < 2]
        rows += kept
        info[j.get("source", f.stem)] = {"asOf": j.get("asOf"), "listings": len(j["listings"]), "used": len(kept)}
    return rows, info


def _pct(a, q):
    return float(np.percentile(a, q))


def _stats(vals, measure):
    """vals ndarray (>= MIN_GROUP) -> dict, rounded for output."""
    nd = 1 if measure == "per_m2" else 0
    return {"n": int(len(vals)), "median": round(float(np.median(vals)), nd),
            "p25": round(_pct(vals, 25), nd), "p75": round(_pct(vals, 75), nd), "suppressed": False}


class Dojazdomat:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or os.environ.get("DOJAZDOMAT_DATA") or DEFAULT_DATA)
        stops, matrix = eng.read_files(self.data_dir)
        self.stops = stops
        self.engine = eng.Engine(stops, matrix, eng.read_walk(self.data_dir))
        self.hoods = json.loads((self.data_dir / "neighborhoods.json").read_text())
        rows, self.portals = load_listings(self.data_dir)
        a = np.array(rows, dtype=np.float64).reshape(-1, 7)
        self.lat, self.lon = a[:, 0], a[:, 1]
        self.price, self.rooms, self.area = a[:, 2], a[:, 3], a[:, 4]
        self._stop_fold = [_fold(s["name"]) for s in stops]
        self._hood_fold = [_fold(h["name"]) for h in self.hoods]
        self._grids = OrderedDict()

    # ---------- helpers ----------
    def _grid(self, lat, lon):
        key = (round(lat, 5), round(lon, 5))
        if key in self._grids:
            self._grids.move_to_end(key)
        else:
            self._grids[key] = self.engine.grid(lat, lon)
            if len(self._grids) > 16:
                self._grids.popitem(last=False)
        return self._grids[key]

    def _mask(self, rooms, min_area, max_area, measure):
        m = np.ones(len(self.lat), bool)
        if rooms is not None:
            m &= self.rooms == rooms
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

    def _near_stops(self, lat, lon):
        d = np.hypot((self.engine.slat - lat) * eng.M_LAT, (self.engine.slon - lon) * eng.M_LON)
        mpm = self.engine.walk["mPerMin"]
        return [{"name": self.stops[i]["name"], "modes": self.stops[i].get("modes", []), "walk_min": round(float(d[i] / mpm), 1)}
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

    def commute_time(self, from_lat, from_lon, to_lat, to_lon):
        _check_point(from_lat, from_lon, "from_"), _check_point(to_lat, to_lon, "to_")
        cell = eng.cell_of(to_lat, to_lon)
        v = int(self._grid(from_lat, from_lon)[cell]) if cell else eng.UNREACHED
        return {"minutes": None if v == eng.UNREACHED else v, "reachable": v != eng.UNREACHED,
                "model": "door-to-door, transit + walk, weekday 07:00-09:00 mean, ~110 m grid cells",
                "origin_stops": self._near_stops(from_lat, from_lon),
                "destination_stops": self._near_stops(to_lat, to_lon)}

    def rent_stats(self, lat, lon, radius_m=800, rooms=None, min_area=None, max_area=None, measure="total"):
        _check_point(lat, lon)
        if not RADIUS[0] <= radius_m <= RADIUS[1]:
            raise ValueError(f"radius_m must be {RADIUS[0]}-{RADIUS[1]}")
        _check_filters(rooms, min_area, max_area, measure)
        m = self._mask(rooms, min_area, max_area, measure) & (self._dist(lat, lon) <= radius_m)
        vals = self._values(measure)[m]
        base = {"center": {"lat": lat, "lon": lon}, "radius_m": radius_m, "measure": measure,
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
                           measure="total", limit=10):
        _check_point(office_lat, office_lon, "office_")
        _check_filters(rooms, None, None, measure)
        for name, v in (("max_commute_min", max_commute_min), ("max_rent", max_rent)):
            if v is not None and not v > 0:
                raise ValueError(f"{name} must be positive")
        limit = max(1, min(int(limit), 50))
        grid = self._grid(office_lat, office_lon).astype(float)
        grid[grid == eng.UNREACHED] = np.nan
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
        nd = 1 if measure == "per_m2" else 0
        out = []
        for h in self.hoods:
            m = mask & (self._dist(h["lat"], h["lon"]) <= HOOD_R)
            v = vals[m]
            cell = eng.cell_of(h["lat"], h["lon"])
            t = grid[cell] if cell else np.nan
            if len(v) < HOOD_MIN or np.isnan(t):
                continue
            med, b = float(np.median(v)), int(band[cell])
            tier = score = None
            if b in terc:
                tier = "cheap" if med <= terc[b][0] else "typical" if med <= terc[b][1] else "expensive"
                score = round(100 * (1 - med / bmed[b]))
            out.append({"name": h["name"], "district": h["district"], "lat": h["lat"], "lon": h["lon"],
                        "n": int(len(v)), "median_rent": round(med, nd), "p25": round(_pct(v, 25), nd),
                        "p75": round(_pct(v, 75), nd), "commute_min": int(t), "value_score": score,
                        "value_tier": tier, "pareto": False, "measure": measure})
        for p in out:
            p["pareto"] = not any(q["commute_min"] <= p["commute_min"] and q["median_rent"] <= p["median_rent"]
                                  and (q["commute_min"] < p["commute_min"] or q["median_rent"] < p["median_rent"])
                                  for q in out)
        sel = [h for h in out if (max_commute_min is None or h["commute_min"] <= max_commute_min)
               and (max_rent is None or h["median_rent"] <= max_rent)]
        sel.sort(key=lambda h: (h["value_score"] is None, -(h["value_score"] or 0), h["commute_min"]))
        return {"office": {"lat": office_lat, "lon": office_lon}, "measure": measure,
                "unit": "PLN/m2/month" if measure == "per_m2" else "PLN/month",
                "value_score": "100*(1 - median/typical median of grid cells in the same commute band); positive = cheaper than typical",
                "pareto": "true = no other eligible neighborhood is both faster and cheaper (computed before max_* filters)",
                "eligible": len(out), "returned": min(limit, len(sel)), "neighborhoods": sel[:limit]}

    def dataset_info(self):
        return {"asOf": max((p["asOf"] for p in self.portals.values() if p["asOf"]), default=None),
                "portals": self.portals,
                "listings_used": int(len(self.lat)),
                "transit": {"stop_groups": len(self.stops), "walk": {k: self.engine.walk[k] for k in ("mPerMin", "stopWalkCap", "originWalkCap")}},
                "neighborhoods": len(self.hoods),
                "bbox": {"lat": [eng.LAT0, eng.LAT1], "lon": [eng.LON0, eng.LON1]},
                "privacy": f"Only aggregates from groups of >= {MIN_GROUP} listings; no listing rows or listing coordinates are exposed.",
                "caveats": CAVEATS}
