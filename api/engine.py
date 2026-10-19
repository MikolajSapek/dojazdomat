"""Commute engine: same model as web/app.js (SPEC 2.6), vectorised with numpy."""
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LAT0, LAT1, LON0, LON1 = 51.98, 52.48, 20.62, 21.48  # city + commuter belt, same grid as web/app.js
DLAT, DLON = 0.001, 0.0016
ROWS = int(np.ceil((LAT1 - LAT0) / DLAT - 1e-9))
COLS = int(np.ceil((LON1 - LON0) / DLON - 1e-9))
M_LAT = 110540.0
M_LON = 111320.0 * np.cos(np.radians(52.23))
STOP_CUT = 90
UNREACHED = 255
WALK_DEFAULT = {"mPerMin": 70.0, "circuity": 1.0, "stopWalkCap": 14.0, "originWalkCap": 25.0}

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
        raw = json.loads(p.read_text())
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
        mpm, cap = self.walk["mPerMin"], self.walk["stopWalkCap"]
        rad = cap * mpm
        kr, kc = int(rad // (DLAT * M_LAT)) + 2, int(rad // (DLON * M_LON)) + 2
        dr, dc = np.meshgrid(np.arange(-kr, kr + 1), np.arange(-kc, kc + 1), indexing="ij")
        dr, dc = dr.ravel(), dc.ravel()
        r0 = np.floor((self.slat - LAT0) / DLAT).astype(int)
        c0 = np.floor((self.slon - LON0) / DLON).astype(int)
        r, c = r0[:, None] + dr, c0[:, None] + dc
        d = np.hypot((_cell_lat[np.clip(r, 0, ROWS - 1)] - self.slat[:, None]) * M_LAT,
                     (_cell_lon[np.clip(c, 0, COLS - 1)] - self.slon[:, None]) * M_LON)
        ok = (r >= 0) & (r < ROWS) & (c >= 0) & (c < COLS) & (d <= rad)
        s_idx = np.broadcast_to(np.arange(self.n)[:, None], r.shape)[ok]
        cell, walk = (r * COLS + c)[ok], (d / mpm)[ok]
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
        mpm = w["mPerMin"]
        walk_to = np.hypot((self.slat - lat) * M_LAT, (self.slon - lon) * M_LON) / mpm
        acc = np.flatnonzero(walk_to <= w["stopWalkCap"])
        best = np.full(self.n + 1, np.inf, np.float32)
        if len(acc):
            m = self.matrix[acc].astype(np.float32)
            m[m == UNREACHED] = np.inf
            best[: self.n] = (m + walk_to[acc, None].astype(np.float32)).min(axis=0)
        best[best >= STOP_CUT] = np.inf
        t = (best[self.cover] + self.cover_walk).min(axis=1).reshape(ROWS, COLS)
        d = np.hypot((_cell_lat[:, None] - lat) * M_LAT, (_cell_lon[None, :] - lon) * M_LON)
        direct = np.where(d <= w["originWalkCap"] * mpm, d / mpm, np.inf)
        t = np.minimum(t, direct)
        out = np.full(t.shape, UNREACHED, np.uint8)
        fin = np.isfinite(t)
        out[fin] = np.clip(np.rint(t[fin]), 0, 254).astype(np.uint8)
        return out
