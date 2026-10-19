"""Build stops.json, matrix.bin, lines.json for the Warsaw commute map.

Usage:  .venv/bin/python -I pipeline/transit/build.py [--date YYYYMMDD] [--download]
Inputs: data/raw/gtfs/{warsaw,wkd,polish_trains}.zip (mkuran.pl GTFS; --download refreshes them).
Outputs: web/data/{stops.json,matrix.bin,lines.json} (written via tmp + rename).
"""
import argparse, datetime as dt, io, json, math, os, re, sys, time, unicodedata, urllib.request, zipfile
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW = os.path.join(ROOT, "data", "raw", "gtfs")
OUT = os.path.join(ROOT, "web", "data")
FEEDS = {  # name -> url
    "warsaw": "https://mkuran.pl/gtfs/warsaw.zip",              # ZTM: bus, tram, metro, SKM
    "wkd": "https://mkuran.pl/gtfs/wkd.zip",                    # WKD
    "polish_trains": "https://mkuran.pl/gtfs/polish_trains.zip",  # all PL rail; we keep agency KM only (km.zip is 404)
}
BBOX = (51.98, 52.48, 20.62, 21.48)            # output: lat min/max, lon min/max (city + commuter belt; listings stay city-only)
ROUTE_BBOX = (51.90, 52.60, 20.45, 21.75)       # routing area (trips may pass outside the city)
WALK_M_PER_MIN, CIRCUITY, WALK_RADIUS_M = 80.0, 1.3, 400.0
IN_GROUP_TRANSFER_S = 120
SAMPLES = [7 * 3600 + 600 * k for k in range(13)]  # 07:00..09:00 inclusive, every 10 min
HORIZON_S = 180 * 60
CUTOFF_MIN = 120                                 # CONTRACT: 255 = unreachable or > 120 min
MAX_N = 3200
LAT0 = 52.23
MX, MY = 111320.0 * math.cos(math.radians(LAT0)), 110574.0
COLORS = {"M1": "#0000bb", "M2": "#bb0000", "tram": "#e8a33d", "bus": "#7fa7cf", "KM": "#008800", "WKD": "#990099"}


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def tsec(s):
    """'HH:MM:SS' Series (hours may be >= 24) -> int seconds."""
    p = s.str.split(":", expand=True).astype(np.int32)
    return (p[0] * 3600 + p[1] * 60 + p[2]).astype(np.int32)


def read(z, name, **kw):
    with z.open(name) as f:
        return pd.read_csv(f, dtype=str, keep_default_na=False, **kw)


def norm(name):
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")
    return re.sub(r"[^a-z0-9]", "", s.replace("warszawa", ""))


def in_box(lat, lon, b):
    return (lat >= b[0]) & (lat <= b[1]) & (lon >= b[2]) & (lon <= b[3])


# ---------------------------------------------------------------- feeds

def download():
    os.makedirs(RAW, exist_ok=True)
    for name, url in FEEDS.items():
        tmp = os.path.join(RAW, name + ".zip.part")
        log("download", url)
        urllib.request.urlretrieve(url, tmp)
        zipfile.ZipFile(tmp).testzip()
        os.replace(tmp, os.path.join(RAW, name + ".zip"))


def pick_date(zips, wanted):
    if wanted:
        return wanted
    common = None
    for z in zips.values():
        d = set(read(z, "calendar_dates.txt", usecols=["date"])["date"])
        common = d if common is None else common & d
    today = dt.date.today()
    for k in range(1, 60):
        d = today + dt.timedelta(days=k)
        # ponytail: skips only the Nov 11 holiday; add a holiday list if dates move into Dec/May
        if d.weekday() == 1 and d.strftime("%Y%m%d") in common and d.strftime("%m%d") != "1111":
            return d.strftime("%Y%m%d")
    sys.exit("no Tuesday present in all feeds")


def load_feed(feed, z, date, agency=None):
    """Return (stops df, trips df, stop_times df, routes df) for services on `date`."""
    cal = read(z, "calendar_dates.txt")
    services = set(cal.loc[(cal.date == date) & (cal.exception_type == "1"), "service_id"])
    routes = read(z, "routes.txt")
    if agency:
        routes = routes[routes.agency_id == agency]
    trips = read(z, "trips.txt")
    trips = trips[trips.service_id.isin(services) & trips.route_id.isin(set(routes.route_id))]
    if "shape_id" not in trips:
        trips["shape_id"] = ""
    keep = set(trips.trip_id)
    cols = ["trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time", "pickup_type", "drop_off_type"]
    parts = []
    with z.open("stop_times.txt") as f:
        hdr = f.readline().decode().strip().split(",")
        use = [c for c in cols if c in hdr]
        f.seek(0)
        for ch in pd.read_csv(f, dtype=str, keep_default_na=False, usecols=use, chunksize=2_000_000):
            parts.append(ch[ch.trip_id.isin(keep)])
    st = pd.concat(parts, ignore_index=True)
    for c in ("pickup_type", "drop_off_type"):
        if c not in st:
            st[c] = "0"
    st["stop_sequence"] = st.stop_sequence.astype(int)
    st["arr"], st["dep"] = tsec(st.arrival_time), tsec(st.departure_time)
    st = st.drop(columns=["arrival_time", "departure_time"])
    if "frequencies.txt" in z.namelist():
        st, trips = expand_frequencies(read(z, "frequencies.txt"), st, trips)
    stops = read(z, "stops.txt")
    log(f"{feed}: {len(services)} services, {len(trips)} trips, {len(st)} stop_times")
    return stops, trips, st, routes


def expand_frequencies(fq, st, trips):
    """Frequency-based template trips (metro) -> concrete trips. exact_times=0 treated as exact."""
    fq = fq[fq.trip_id.isin(set(trips.trip_id))]
    if fq.empty:
        return st, trips
    tmpl = st[st.trip_id.isin(set(fq.trip_id))].sort_values(["trip_id", "stop_sequence"])
    base = tmpl.groupby("trip_id").dep.transform("min")
    tmpl = tmpl.assign(arr=tmpl.arr - base, dep=tmpl.dep - base)
    new_st, new_trips = [], []
    tinfo = trips.set_index("trip_id")
    for r in fq.itertuples():
        start, end = (int(tsec(pd.Series([x]))[0]) for x in (r.start_time, r.end_time))
        hw = int(r.headway_secs)
        t = tmpl[tmpl.trip_id == r.trip_id]
        for t0 in range(start, end, hw):
            if not (5 * 3600 <= t0 <= 12 * 3600):
                continue
            tid = f"{r.trip_id}@{t0}"
            new_st.append(t.assign(trip_id=tid, arr=t.arr + t0, dep=t.dep + t0))
            row = tinfo.loc[r.trip_id].copy()
            row["trip_id"] = tid
            new_trips.append(row)
    ids = set(fq.trip_id)
    st = pd.concat([st[~st.trip_id.isin(ids)]] + new_st, ignore_index=True)
    trips = pd.concat([trips[~trips.trip_id.isin(ids)], pd.DataFrame(new_trips)], ignore_index=True)
    return st, trips


# ---------------------------------------------------------------- stop groups

def ztm_key(sid):
    if re.fullmatch(r"\d{6}", sid):
        return sid[:4]
    m = re.fullmatch(r"(\d{4})M(:.*)?", sid)  # metro station / platform -> same zespół as surface stops
    return m.group(1) if m else sid


def build_groups(feeds):
    """Map (feed, stop_id) -> group key; return mapping and group info dict."""
    member = {}   # (feed, sid) -> key
    info = defaultdict(lambda: {"pts": [], "names": Counter(), "rail_name": None})
    s = feeds["warsaw"][0]
    rail_ids = set(feeds["warsaw"][2].merge(feeds["warsaw"][1][["trip_id", "route_id"]])
                   .merge(feeds["warsaw"][3][["route_id", "route_type"]]).query("route_type == '2'").stop_id)
    surface = defaultdict(list)  # zespół prefix -> surface stop coords
    for r in s.itertuples():
        if re.fullmatch(r"\d{6}", r.stop_id):
            surface[r.stop_id[:4]].append((float(r.stop_lat), float(r.stop_lon)))
    for r in s.itertuples():
        if r.location_type not in ("", "0"):
            continue
        k, name, lat, lon = ztm_key(r.stop_id), r.stop_name_stem or r.stop_name, float(r.stop_lat), float(r.stop_lon)
        if "M" in r.stop_id and not re.fullmatch(r"\d{6}", r.stop_id):  # metro platform
            if not any(dist_m(lat, lon, *p) < 500 for p in surface.get(k, [])):
                k = r.stop_id.split(":")[0]  # metro station id does not share a nearby zespół
            name = "Metro " + r.stop_name
        member[("warsaw", r.stop_id)] = k
        g = info[k]
        g["pts"].append((lat, lon))
        g["names"][name] += 1
        if r.stop_id in rail_ids:
            g["rail_name"] = r.stop_name
    rail_groups = {k: g for k, g in info.items() if g["rail_name"]}

    def attach(feed, sid, name, lat, lon, prefix, is_rail_merge):
        best, bd = None, 1e9
        if is_rail_merge:
            n = norm(name)
            for k, g in rail_groups.items():
                d = dist_m(lat, lon, *g["pts"][0])
                if (norm(g["rail_name"]) == n and d < 300) or d < 150:
                    if d < bd:
                        best, bd = k, d
        k = best or f"{prefix}:{sid}"
        member[(feed, sid)] = k
        g = info[k]
        g["pts"].append((lat, lon))
        g["names"][name] += 1
        g["rail_name"] = g["rail_name"] or name

    s = feeds["polish_trains"][0]
    parent = dict(zip(s.stop_id, s.parent_station))
    pos = {r.stop_id: (r.stop_name, float(r.stop_lat), float(r.stop_lon)) for r in s.itertuples()}
    for sid in set(feeds["polish_trains"][2].stop_id):
        p = parent.get(sid) or sid
        name, lat, lon = pos[p]
        if (feed_sid := ("polish_trains", p)) in member:
            member[("polish_trains", sid)] = member[feed_sid]
            continue
        attach("polish_trains", p, name, lat, lon, "KM", not sid.endswith("_BUS"))
        member[("polish_trains", sid)] = member[("polish_trains", p)]
    for r in feeds["wkd"][0].itertuples():
        attach("wkd", r.stop_id, r.stop_name, float(r.stop_lat), float(r.stop_lon), "WKD", False)
    return member, info


def dist_m(lat1, lon1, lat2, lon2):
    return math.hypot((lon1 - lon2) * MX, (lat1 - lat2) * MY)


# ---------------------------------------------------------------- CSA (numba)

def make_csa():
    from numba import njit, prange

    @njit(cache=True)
    def one(o, T, horizon, c_from, c_to, c_dep, c_arr, c_trip, c_board, c_alight,
            fp_ptr, fp_to, fp_t, arr, varr, ready, reached):
        arr[:] = 2_000_000_000
        varr[:] = 2_000_000_000
        ready[:] = 2_000_000_000
        reached[:] = False
        arr[o] = T
        ready[o] = T
        for j in range(fp_ptr[o], fp_ptr[o + 1]):
            w = fp_to[j]
            arr[w] = min(arr[w], T + fp_t[j])
            ready[w] = min(ready[w], T + fp_t[j])
        c = np.searchsorted(c_dep, T)
        end = T + horizon
        n = c_dep.shape[0]
        while c < n and c_dep[c] <= end:
            k = c_trip[c]
            if reached[k] or (c_board[c] and ready[c_from[c]] <= c_dep[c]):
                reached[k] = True
                v = c_to[c]
                ta = c_arr[c]
                if c_alight[c] and ta < varr[v]:  # relax footpaths on best vehicle arrival, not best overall
                    varr[v] = ta
                    if ta < arr[v]:
                        arr[v] = ta
                    if ta + 120 < ready[v]:
                        ready[v] = ta + 120
                    for j in range(fp_ptr[v], fp_ptr[v + 1]):
                        w = fp_to[j]
                        t = ta + fp_t[j]
                        if t < arr[w]:
                            arr[w] = t
                        t2 = ta + max(fp_t[j], 120)
                        if t2 < ready[w]:
                            ready[w] = t2
            c += 1

    @njit(parallel=True, cache=True)
    def all_pairs(origins, samples, horizon, cutoff, G, ntrips, c_from, c_to, c_dep, c_arr, c_trip,
                  c_board, c_alight, fp_ptr, fp_to, fp_t):
        n = origins.shape[0]
        out = np.full((n, n), 255, np.uint8)
        for i in prange(n):
            arr = np.empty(G, np.int64)
            varr = np.empty(G, np.int64)
            ready = np.empty(G, np.int64)
            reached = np.empty(ntrips, np.bool_)
            tot = np.zeros(n, np.int64)
            bad = np.zeros(n, np.bool_)
            for T in samples:
                one(origins[i], T, horizon, c_from, c_to, c_dep, c_arr, c_trip, c_board, c_alight,
                    fp_ptr, fp_to, fp_t, arr, varr, ready, reached)
                for j in range(n):
                    a = arr[origins[j]]
                    if a > T + horizon:
                        bad[j] = True
                    else:
                        tot[j] += a - T
            for j in range(n):
                if not bad[j]:
                    m = int(tot[j] / samples.shape[0] / 60.0 + 0.5)
                    if m <= cutoff:
                        out[i, j] = m
        return out

    return all_pairs


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--lines-only", action="store_true", help="rebuild lines.json + lines-bus.json, skip the matrix")
    a = ap.parse_args()
    if a.download or not all(os.path.exists(os.path.join(RAW, f + ".zip")) for f in FEEDS):
        download()
    zips = {f: zipfile.ZipFile(os.path.join(RAW, f + ".zip")) for f in FEEDS}
    date = pick_date(zips, a.date)
    log("service date", date)
    feeds = {
        "warsaw": load_feed("warsaw", zips["warsaw"], date),
        "wkd": load_feed("wkd", zips["wkd"], date),
        "polish_trains": load_feed("polish_trains", zips["polish_trains"], date, agency="KM"),
    }
    if a.lines_only:
        return write_lines(feeds)
    member, info = build_groups(feeds)

    # group table
    keys = sorted(info)
    gidx = {k: i for i, k in enumerate(keys)}
    glat = np.array([np.mean([p[0] for p in info[k]["pts"]]) for k in keys])
    glon = np.array([np.mean([p[1] for p in info[k]["pts"]]) for k in keys])
    for k in keys:  # collision check
        far = max(dist_m(p[0], p[1], glat[gidx[k]], glon[gidx[k]]) for p in info[k]["pts"])
        if far > 600:
            log(f"WARN group {k} ({info[k]['names'].most_common(1)[0][0]}) member {far:.0f} m from centroid")

    # connections
    conns, gmodes, groutes, trip_meta = [], defaultdict(set), defaultdict(set), []
    mode_of = {"0": "tram", "1": "metro", "2": "rail", "3": "bus"}
    for feed, (stops, trips, st, routes) in feeds.items():
        rt = routes.set_index("route_id")
        trips = trips.merge(routes[["route_id", "route_short_name", "route_type"]], on="route_id")
        st = st.merge(trips[["trip_id", "route_short_name", "route_type"]], on="trip_id")
        st = st.sort_values(["trip_id", "stop_sequence"], kind="stable")
        st["g"] = [gidx[member[(feed, s)]] for s in st.stop_id]
        st["mode"] = st.route_type.map(mode_of).fillna("bus")
        nxt = st.shift(-1)
        ok = (nxt.trip_id == st.trip_id).values
        c = pd.DataFrame({
            "trip": (feed + "|") + st.trip_id[ok].values, "fr": st.g.values[ok], "to": nxt.g.values[ok].astype(int),
            "dep": st.dep.values[ok], "arr": nxt.arr.values[ok].astype(int),
            "board": (st.pickup_type.values[ok] != "1"), "alight": (nxt.drop_off_type.values[ok] != "1")})
        conns.append(c)
        win = st[(st.dep >= 6 * 3600) & (st.dep <= 10 * 3600)]
        for g, r, m in zip(win.g.values, win.route_short_name.values, win["mode"].values):
            groutes[g].add(r)
            gmodes[g].add(m)
    C = pd.concat(conns, ignore_index=True)
    inr = in_box(glat, glon, ROUTE_BBOX)
    C = C[inr[C.fr.values] & inr[C.to.values] & (C.dep >= SAMPLES[0]) & (C.dep <= SAMPLES[-1] + HORIZON_S)]
    C = C[C.arr >= C.dep].sort_values("dep", kind="stable")
    C["trip_i"] = pd.factorize(C.trip)[0]
    log(f"connections in window: {len(C)}, trips: {C.trip_i.max() + 1}, groups total: {len(keys)}")

    # output groups: in city bbox and served in the morning
    served = np.zeros(len(keys), bool)
    served[list(gmodes)] = True
    cand = np.where(in_box(glat, glon, BBOX) & served)[0]
    if len(cand) > MAX_N:  # ponytail: drops bus-only groups by fewest routes; smarter merging if this triggers
        pri = [(0 if gmodes[g] - {"bus"} else 1, -len(groutes[g])) for g in cand]
        cand = cand[np.lexsort(np.array(pri).T[::-1])][:MAX_N]
    origins = np.sort(cand).astype(np.int64)
    log(f"output stop groups: {len(origins)}")

    # footpaths between groups (centroid distance)
    fps = [[] for _ in keys]
    order = np.argsort(glat)
    lat_s = glat[order]
    rad_deg = WALK_RADIUS_M / MY
    for i in range(len(keys)):
        lo, hi = np.searchsorted(lat_s, glat[i] - rad_deg), np.searchsorted(lat_s, glat[i] + rad_deg)
        for j in order[lo:hi]:
            if j != i:
                d = dist_m(glat[i], glon[i], glat[j], glon[j])
                if d <= WALK_RADIUS_M:
                    fps[i].append((j, int(round(d * CIRCUITY / WALK_M_PER_MIN * 60))))
    fp_ptr = np.zeros(len(keys) + 1, np.int64)
    fp_ptr[1:] = np.cumsum([len(f) for f in fps])
    fp_to = np.array([j for f in fps for j, _ in f], np.int64)
    fp_t = np.array([t for f in fps for _, t in f], np.int64)
    log(f"footpaths: {len(fp_to)}")

    all_pairs = make_csa()
    t0 = time.time()
    M = all_pairs(origins, np.array(SAMPLES, np.int64), HORIZON_S, CUTOFF_MIN, len(keys), int(C.trip_i.max() + 1),
                  C.fr.values.astype(np.int64), C.to.values.astype(np.int64), C.dep.values.astype(np.int64),
                  C.arr.values.astype(np.int64), C.trip_i.values.astype(np.int64),
                  C.board.values.astype(np.bool_), C.alight.values.astype(np.bool_), fp_ptr, fp_to, fp_t)
    log(f"matrix {M.shape} in {time.time() - t0:.0f}s")
    assert (np.diag(M) == 0).all()
    live = ((M < 255).sum(1) > 1) & ((M < 255).sum(0) > 1)  # drop groups with no usable departure/arrival
    if not live.all():
        log("dropping dead groups:", [keys[g] for g in origins[~live]])
        origins, M = origins[live], np.ascontiguousarray(M[live][:, live])

    # stops.json
    def gname(g):
        k = keys[g]
        return info[k]["rail_name"] if info[k]["rail_name"] and gmodes[g] & {"rail"} else info[k]["names"].most_common(1)[0][0]

    def rkey(r):
        return (0, int(r)) if r.isdigit() else (1, r)
    stops_out = [{"id": keys[g], "name": gname(g), "lat": round(float(glat[g]), 6), "lon": round(float(glon[g]), 6),
                  "routes": sorted(groutes[g], key=rkey),
                  "modes": [m for m in ("metro", "rail", "tram", "bus") if m in gmodes[g]]} for g in origins]

    os.makedirs(OUT, exist_ok=True)
    write(os.path.join(OUT, "stops.json"), json.dumps(stops_out, ensure_ascii=False, separators=(",", ":")).encode())
    write(os.path.join(OUT, "matrix.bin"), np.uint32(len(origins)).astype("<u4").tobytes() + M.tobytes())
    write_lines(feeds)
    report(M, stops_out)


def write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    log(f"wrote {path} ({len(data)} B)")


# ---------------------------------------------------------------- lines

def write_lines(feeds):
    for name, types in (("lines.json", ("0", "1", "2")), ("lines-bus.json", ("3",))):
        data = build_lines(feeds, types)
        write(os.path.join(OUT, name), json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())


def build_lines(feeds, types=("0", "1", "2")):
    from shapely.geometry import LineString, box
    from shapely.ops import linemerge, unary_union
    clip = box(BBOX[2] - 0.15, BBOX[0] - 0.1, BBOX[3] + 0.15, BBOX[1] + 0.1)
    from shapely import affinity
    out = []
    for feed, (stops, trips, st, routes) in feeds.items():
        z = zipfile.ZipFile(os.path.join(RAW, feed + ".zip"))
        r = routes[routes.route_type.isin(list(types))]
        t = trips[trips.route_id.isin(set(r.route_id))]
        need = set(t.shape_id) - {""}
        shp = {}
        if need and "shapes.txt" in z.namelist():
            s = read(z, "shapes.txt")
            s = s[s.shape_id.isin(need)]
            s = s.assign(seq=s.shape_pt_sequence.astype(int)).sort_values(["shape_id", "seq"])
            for sid, g in s.groupby("shape_id"):
                shp[sid] = list(zip(g.shape_pt_lon.astype(float), g.shape_pt_lat.astype(float)))
        spos = {x.stop_id: (float(x.stop_lon), float(x.stop_lat)) for x in stops.itertuples()}
        par = dict(zip(stops.stop_id, stops.parent_station)) if "parent_station" in stops else {}
        for rr in r.itertuples():
            tr = t[t.route_id == rr.route_id]
            geoms = [shp[s] for s in set(tr.shape_id) if s in shp]
            for tid in tr.trip_id[tr.shape_id == ""].unique()[:20]:  # no shape: stop sequence polyline
                seq = st[st.trip_id == tid].sort_values("stop_sequence").stop_id
                geoms.append([spos[par.get(x) or x] for x in seq if (par.get(x) or x) in spos])
            geoms = [g for g in geoms if len(g) >= 2]
            if not geoms:
                continue
            ml = unary_union([affinity.scale(LineString(g), MX, MY, origin=(0, 0)) for g in geoms])
            ml = linemerge(ml) if ml.geom_type == "MultiLineString" else ml
            ml = affinity.scale(ml.simplify(10), 1 / MX, 1 / MY, origin=(0, 0)).intersection(clip)
            parts = list(ml.geoms) if hasattr(ml, "geoms") else [ml]
            paths = [[[round(y, 5), round(x, 5)] for x, y in p.coords] for p in parts if p.geom_type == "LineString" and len(p.coords) > 1]
            if not paths:
                continue
            mode = {"0": "tram", "1": "metro", "2": "rail", "3": "bus"}[rr.route_type]
            name = rr.route_short_name
            color = (COLORS.get(name) or (COLORS[mode] if mode in ("tram", "bus") else COLORS["KM"] if feed == "polish_trains"
                     else COLORS["WKD"] if feed == "wkd" else "#" + (rr.route_color or "888888").lower()))
            out.append({"route": name, "mode": mode, "color": color, "paths": paths})
    log(f"lines: {len(out)} routes, {sum(len(l['paths']) for l in out)} paths, "
        f"{sum(len(p) for l in out for p in l['paths'])} points")
    return out


# ---------------------------------------------------------------- sanity

PAIRS = [("Centrum", "Metro Kabaty"), ("Metro Wilanowska", "Metro Młociny"), ("Warszawa Centralna", "Warszawa Lotnisko Chopina"),
         ("pl. Zbawiciela", "Rondo ONZ"), ("Warszawa Śródmieście", "Warszawa Wschodnia"),
         ("Metro Politechnika", "Metro Ratusz Arsenał"), ("Dw. Gdański", "Metro Bródno"),
         ("Warszawa Zachodnia", "Warszawa Ursus"), ("Metro Kabaty", "Centrum"), ("Metro Bemowo", "Metro Trocka")]


def report(M, stops):
    by = defaultdict(list)
    for i, s in enumerate(stops):
        by[norm(s["name"])].append(i)
    exact = {s["name"]: i for i, s in reversed(list(enumerate(stops)))}

    def find(n):
        if n in exact:
            return exact[n]
        k = norm(n)
        if by.get(k):
            return by[k][0]
        hits = [i for kk, v in by.items() if k in kk for i in v]
        return hits[0] if hits else None
    for a, b in PAIRS:
        i, j = find(a), find(b)
        if i is None or j is None:
            print(f"  ? {a} -> {b}: not found ({i}, {j})")
            continue
        print(f"  {stops[i]['name']} [{stops[i]['id']}] -> {stops[j]['name']} [{stops[j]['id']}]: {M[i, j]} min "
              f"(reverse {M[j, i]})")
    n = len(stops)
    fin = M[M < 255]
    print(f"  n={n}, file bytes={4 + n * n}, unreachable/over-cut cells={int((M == 255).sum())}, "
          f"max={int(fin.max())}, mean={fin.mean():.1f}, "
          f"mean |M-M.T|={np.abs(M.astype(int) - M.T.astype(int))[(M < 255) & (M.T < 255)].mean():.2f}")


if __name__ == "__main__":
    main()
