"""Car routing: one-to-all Dijkstra on the OSM drive graph (pipeline/car/build_graph.py), rasterised onto the app grid.

Edge seconds already carry the congestion factor, so the grid is rush-hour drive minutes from each cell (home) to the office (owner decision D1: home -> office, morning).
Dijkstra runs from the office over the transposed graph. The straight legs (cell -> nearest road node, office -> its node) are
added here; the app adds none.
"""
from collections import OrderedDict
from pathlib import Path
from threading import Lock

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from .engine import COLS, DLAT, DLON, LAT0, LON0, M_LAT, M_LON, ROWS, UNREACHED

LIMIT_S = 95 * 60        # beyond the app's 90 min cut, stop expanding
ACCESS_MPM = 250.0       # cell centre -> nearest road node, straight line at 15 km/h
ACCESS_CAP_M = 2000.0    # a cell further than this from any road node is unreached (600 m left holes along long junction-to-junction edges)
SNAP_CAP_M = 1500.0      # office further than this from the graph: no car routing


class CarRouter:
    """Also serves walk and bike (same one-to-all Dijkstra): pass the walk/bike constants. office_leg adds the straight leg from the
    snapped office point to its graph node (all modes as served by api/main.py)."""
    def __init__(self, path, access_mpm=ACCESS_MPM, access_cap_m=ACCESS_CAP_M, snap_cap_m=SNAP_CAP_M, office_leg=False):
        self.access_mpm, self.snap_cap_m, self.office_leg = access_mpm, snap_cap_m, office_leg
        z = np.load(path)
        lat, lon = z["lat"].astype(np.float64), z["lon"].astype(np.float64)
        n = len(lat)
        self.factor = float(z["factor"])
        self.g = csr_matrix((z["sec"], (z["u"], z["v"])), shape=(n, n))
        self.gt = self.g.T.tocsr()  # home -> office times = one-to-all from the office over reversed edges
        # Snap only to the largest strongly connected component: one-way islands (1-4 nodes) would leave holes in the grid.
        _, lab = connected_components(self.g, connection="strong")
        self.main = np.flatnonzero(lab == np.bincount(lab).argmax())
        self.tree = cKDTree(np.c_[(lon - LON0) * M_LON, (lat - LAT0) * M_LAT][self.main])
        cx = (LON0 + (np.arange(COLS) + 0.5) * DLON - LON0) * M_LON
        cy = (LAT0 + (np.arange(ROWS) + 0.5) * DLAT - LAT0) * M_LAT
        gx, gy = np.meshgrid(cx, cy)
        d, self.cell_node = self.tree.query(np.c_[gx.ravel(), gy.ravel()], distance_upper_bound=access_cap_m)
        self.cell_node = np.where(np.isfinite(d), self.main[np.minimum(self.cell_node, len(self.main) - 1)], n)  # n = sentinel: no road near this cell
        self.cell_access = np.where(np.isfinite(d), d / access_mpm, 0).astype(np.float32)
        self.n = n
        self.cache, self.lock = OrderedDict(), Lock()  # ponytail: per-process LRU, 32 offices x 268 KB

    def grid(self, lat, lon):
        """uint8 ROWS x COLS minutes (255 = unreached). The office is snapped to its 110 m grid cell, so equal cells share a cache entry."""
        r, c = int((lat - LAT0) // DLAT), int((lon - LON0) // DLON)
        key = (r, c)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
        x, y = (LON0 + (c + 0.5) * DLON - LON0) * M_LON, (LAT0 + (r + 0.5) * DLAT - LAT0) * M_LAT
        d, src = self.tree.query([x, y])
        if d > self.snap_cap_m:
            return None
        sec = dijkstra(self.gt, indices=int(self.main[src]), limit=LIMIT_S)
        sec = np.append(sec, np.inf)  # sentinel node
        t = sec[self.cell_node] / 60.0 + self.cell_access + (d / self.access_mpm if self.office_leg else 0.0)
        out = np.full(t.shape, UNREACHED, np.uint8)
        fin = np.isfinite(t)
        out[fin] = np.clip(np.rint(t[fin]), 0, 254).astype(np.uint8)
        out = out.reshape(ROWS, COLS)
        with self.lock:
            self.cache[key] = out
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
        return out


def load(data_dir, name="car", **kw):
    p = Path(data_dir) / f"{name}-graph.npz"
    return CarRouter(p, **kw) if p.exists() else None
