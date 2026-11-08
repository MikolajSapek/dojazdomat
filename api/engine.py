"""Commute engine: same model as web/app.js (SPEC 2.6), vectorised with numpy."""
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# One source for the web app and this engine: web/model-constants.json (docker images copy it next to web/data, see the Dockerfiles).
K = json.loads((Path(__file__).resolve().parents[1] / "web" / "model-constants.json").read_text())
LAT0, LAT1, LON0, LON1 = (K["grid"][k] for k in ("lat0", "lat1", "lon0", "lon1"))  # city + commuter belt
DLAT, DLON = K["grid"]["dlat"], K["grid"]["dlon"]
ROWS = int(np.ceil((LAT1 - LAT0) / DLAT - 1e-9))
COLS = int(np.ceil((LON1 - LON0) / DLON - 1e-9))
M_LAT = float(K["grid"]["mLat"])
M_LON = K["grid"]["mLonEquator"] * np.cos(np.radians(K["grid"]["refLat"]))
STOP_CUT = K["stopCut"]
UNREACHED = 255
WALK_DEFAULT = {k: float(v) for k, v in K["walkDefault"].items()}  # caps only; walk.json may override them
WALK_NET, BIKE_NET = K["walk"], K["bike"]


def net_min(d, net=WALK_NET):
    """Walk/bike minutes for straight-line metres d (scalar or array): d * circuity(d) / mPerMin, circuity linear between the
    model-constants.json points and flat beyond the ends. Same function as web/engine.js netMin."""
    xs, cs = zip(*net["circuity"])
    return np.asarray(d, dtype=np.float64) * np.interp(d, xs, cs) / net["mPerMin"]


def reach_m(cap_min, net=WALK_NET):
    """No straight line longer than this can be covered within cap_min (circuity >= its minimum)."""
    return cap_min * net["mPerMin"] / min(c for _, c in net["circuity"])

_cell_lat = LAT0 + (np.arange(ROWS) + 0.5) * DLAT
_cell_lon = LON0 + (np.arange(COLS) + 0.5) * DLON


def in_bbox(lat, lon):
    return LAT0 <= lat <= LAT1 and LON0 <= lon <= LON1


def cell_of(lat, lon):
    r, c = int((lat - LAT0) // DLAT), int((lon - LON0) // DLON)
    return (r, c) if 0 <= r < ROWS and 0 <= c < COLS else None


def read_walk(data_dir):
    walk = dict(WALK_DEFAULT)
    p = Path(data_dir) / "walk.json"
    if p.exists():
        try:
            raw = json.loads(p.read_text())
            raw.items()
        except (ValueError, AttributeError) as e:  # half-written or wrong shape: callers answer 503 instead of crashing
            raise ValueError(f"walk.json unreadable: {e}") from None
        walk.update({k: float(v) for k, v in raw.items() if k in walk and isinstance(v, (int, float)) and v > 0})
    return walk


def read_files(data_dir):
    """-> (stops [{id,name,lat,lon,...}], matrix uint8 n x n)"""
    d = Path(data_dir)
    stops = json.loads((d / "stops.json").read_text())
    raw = (d / "matrix.bin").read_bytes()
    n = int(np.frombuffer(raw, "<u4", 1)[0])
    if n != len(stops) or len(raw) != 4 + n * n:
        raise ValueError(f"matrix.bin n={n} does not match stops.json ({len(stops)})")
    return stops, np.frombuffer(raw, np.uint8, offset=4).reshape(n, n)


def read_db(conn):
    """-> (stops, matrix) from core tables, or None when empty/inconsistent."""
    stops = conn.execute(
        "SELECT id, name, ST_Y(ST_Transform(geom,4326)), ST_X(ST_Transform(geom,4326)) "
        "FROM core.stop_group ORDER BY id").fetchall()
    rows = conn.execute("SELECT origin, row FROM core.travel_matrix ORDER BY origin").fetchall()
    n = len(stops)
    if not n or len(rows) != n or [r[0] for r in rows] != [s[0] for s in stops] or any(len(r[1]) != n for r in rows):
        return None
    m = np.frombuffer(b"".join(bytes(r[1]) for r in rows), np.uint8).reshape(n, n)
    return [{"id": s[0], "name": s[1], "lat": s[2], "lon": s[3]} for s in stops], m


@dataclass
class Engine:
    stops: list
    matrix: np.ndarray
    walk: dict

    def __post_init__(self):
        self.n = len(self.stops)
        self.slat = np.array([s["lat"] for s in self.stops])
        self.slon = np.array([s["lon"] for s in self.stops])
        self._build_cover()

    def _build_cover(self):
        """Per cell, the stops whose disc (stopWalkCap) covers it, padded with a sentinel stop = n."""
        cap = self.walk["stopWalkCap"]
        rad = reach_m(cap)
        kr, kc = int(rad // (DLAT * M_LAT)) + 2, int(rad // (DLON * M_LON)) + 2
        dr, dc = np.meshgrid(np.arange(-kr, kr + 1), np.arange(-kc, kc + 1), indexing="ij")
        dr, dc = dr.ravel(), dc.ravel()
        r0 = np.floor((self.slat - LAT0) / DLAT).astype(int)
        c0 = np.floor((self.slon - LON0) / DLON).astype(int)
        r, c = r0[:, None] + dr, c0[:, None] + dc
        d = np.hypot((_cell_lat[np.clip(r, 0, ROWS - 1)] - self.slat[:, None]) * M_LAT,
                     (_cell_lon[np.clip(c, 0, COLS - 1)] - self.slon[:, None]) * M_LON)
        wmin = net_min(d)
        ok = (r >= 0) & (r < ROWS) & (c >= 0) & (c < COLS) & (d <= rad) & (wmin <= cap)
        s_idx = np.broadcast_to(np.arange(self.n)[:, None], r.shape)[ok]
        cell, walk = (r * COLS + c)[ok], wmin[ok]
        order = np.lexsort((walk, cell))
        cell, s_idx, walk = cell[order], s_idx[order], walk[order]
        first = np.r_[0, np.flatnonzero(np.diff(cell)) + 1]
        rank = np.arange(len(cell)) - np.repeat(first, np.diff(np.r_[first, len(cell)]))
        k = int(rank.max()) + 1 if len(rank) else 1
        self.cover = np.full((ROWS * COLS, k), self.n, np.uint16)
        self.cover_walk = np.zeros((ROWS * COLS, k), np.float32)
        self.cover[cell, rank] = s_idx
        self.cover_walk[cell, rank] = walk

    def grid(self, lat, lon):
        """uint8 ROWS x COLS minutes (255 = unreached)."""
        w = self.walk
        walk_to = net_min(np.hypot((self.slat - lat) * M_LAT, (self.slon - lon) * M_LON))
        acc = np.flatnonzero(walk_to <= w["stopWalkCap"])
        best = np.full(self.n + 1, np.inf, np.float32)
        if len(acc):
            # matrix[i, j] = minutes departing stop i, arriving stop j; home -> office reads the COLUMNS of the office stops
            m = self.matrix[:, acc].astype(np.float32)
            m[m == UNREACHED] = np.inf
            best[: self.n] = (m + walk_to[acc][None, :].astype(np.float32)).min(axis=1)
        best[best >= STOP_CUT] = np.inf
        t = (best[self.cover] + self.cover_walk).min(axis=1).reshape(ROWS, COLS)
        d = np.hypot((_cell_lat[:, None] - lat) * M_LAT, (_cell_lon[None, :] - lon) * M_LON)
        dmin = net_min(d)
        direct = np.where(dmin <= w["originWalkCap"], dmin, np.inf)
        t = np.minimum(t, direct)
        t[t > STOP_CUT] = np.inf  # door-to-door above stopCut is unreachable, as web/engine.js commuteGrid (cut before rounding)
        out = np.full(t.shape, UNREACHED, np.uint8)
        fin = np.isfinite(t)
        out[fin] = np.floor(t[fin] + 0.5).astype(np.uint8)  # half up, as Math.floor(t + 0.5) in web/engine.js; t <= STOP_CUT < 255
        return out
