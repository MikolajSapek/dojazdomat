"""Rent SQL (parameterised) and binning, same model as web/app.js buildBins / hood stats."""
from collections import defaultdict
from statistics import median

from .engine import K, LAT0, LON0, M_LAT, M_LON

BIN_M, BIN_MIN, HOOD_MIN = K["bins"]["binM"], K["medianMinN"], K["medianMinN"]  # one minimum for bins and hoods (D2)
_LAT = "ST_Y(ST_Transform(a.geom, 4326))"
_LON = "ST_X(ST_Transform(a.geom, 4326))"


def _where(f):
    """-> (sql fragment, params, value expression). Only fixed fragments are interpolated."""
    w, p = ["a.coord_precision < 2", f"(a.flags & {int(K['feeKnownBit'])}) <> 0"], []  # the median rule, as web/rent.js inMedian
    if f["rooms"] is not None:
        w.append("a.rooms = %s"); p.append(f["rooms"])
    if f["min_area"] is not None:
        w.append("a.area_m2 >= %s"); p.append(f["min_area"])
    if f["max_area"] is not None:
        w.append("a.area_m2 <= %s"); p.append(f["max_area"])
    if f["flags"]:
        w.append("(a.flags & %s) = %s"); p += [f["flags"], f["flags"]]
    if f["portal"]:
        w.append("po.name = %s"); p.append(f["portal"])
    if f["measure"] == "per_m2":
        w.append("a.area_m2 IS NOT NULL")
        val = "a.price::numeric / a.area_m2"
    else:
        val = "a.price::numeric"
    return " AND ".join(w), p, val


_FROM = "FROM mart.mv_active_listing a JOIN core.portal po ON po.id = a.portal_id"


def rent(conn, f):
    where, params, val = _where(f)
    rows = conn.execute(f"SELECT {_LAT}, {_LON}, {val} {_FROM} WHERE {where}", params).fetchall()
    lists = defaultdict(list)
    for lat, lon, v in rows:
        lists[(int((lat - LAT0) * M_LAT // BIN_M), int((lon - LON0) * M_LON // BIN_M))].append(float(v))
    bins = []
    for by, bx in {(y + dy, x + dx) for y, x in lists for dy in (-1, 0, 1) for dx in (-1, 0, 1)}:
        vals = [v for dy in (-1, 0, 1) for dx in (-1, 0, 1) for v in lists.get((by + dy, bx + dx), ())]
        if len(vals) >= BIN_MIN:
            bins.append({"by": by, "bx": bx, "median": median(vals), "n": len(vals)})
    bins.sort(key=lambda b: (b["by"], b["bx"]))
    return {"bin_m": BIN_M, "lat0": LAT0, "lon0": LON0, "n_listings": len(rows),
            "bins": bins, "neighborhoods": neighborhoods(conn, f)}


def neighborhoods(conn, f):
    where, params, val = _where(f)
    rows = conn.execute(
        f"""SELECT n.id, n.name, d.name, ST_Y(ST_Transform(coalesce(n.label_geom, ST_PointOnSurface(n.geom)), 4326)),
                   ST_X(ST_Transform(coalesce(n.label_geom, ST_PointOnSurface(n.geom)), 4326)),
                   count(a.id), percentile_cont(0.5) WITHIN GROUP (ORDER BY {val})
            FROM core.neighborhood n JOIN core.district d ON d.id = n.district_id
            LEFT JOIN mart.mv_active_listing a ON a.neighborhood_id = n.id AND ({where.replace('po.name', '(SELECT name FROM core.portal WHERE id = a.portal_id)')})
            GROUP BY n.id, n.name, d.name, n.label_geom, n.geom ORDER BY n.id""", params).fetchall()
    return [{"id": i, "name": nm, "district": ds, "lat": la, "lon": lo, "n": int(n),
             "median": float(m) if m is not None and n >= HOOD_MIN else None}
            for i, nm, ds, la, lo, n, m in rows]


def stops(conn, q):
    return [{"id": i, "name": n, "lat": la, "lon": lo} for i, n, la, lo in conn.execute(
        """SELECT id, name, ST_Y(ST_Transform(geom,4326)), ST_X(ST_Transform(geom,4326))
           FROM core.stop_group WHERE name ILIKE %s OR name %% %s
           ORDER BY similarity(name, %s) DESC, name LIMIT 10""",
        (f"%{q.replace('%', '').replace('_', '')}%", q, q)).fetchall()]
