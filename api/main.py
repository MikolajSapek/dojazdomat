"""FastAPI app. Run: .venv/bin/uvicorn api.main:app --port 8766"""
import ipaddress
import logging
import math
import os
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

import anyio.to_thread
import numpy as np
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from mcp_server.core import Dojazdomat, _fold

from . import car as carmod
from . import engine as eng
from . import queries

log = logging.getLogger("api")
ROOT = Path(__file__).resolve().parents[1]


def client_ip(request):
    """Real client behind Caddy/cloudflared. Proxy headers are honoured only when the TCP peer is a
    loopback/private address (the proxy); a public peer cannot spoof them."""
    peer = request.client.host if request.client else "?"
    try:
        a = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not (a.is_private or a.is_loopback):
        return peer
    h = request.headers
    if h.get("cf-connecting-ip"):
        return h["cf-connecting-ip"].strip()[:45] or peer
    # XFF: the client controls the left side, each proxy appends on the right. Take the rightmost non-private hop.
    hops = [x.strip() for x in h.get("x-forwarded-for", "").split(",") if x.strip()]
    for hop in reversed(hops):
        try:
            if not (ipaddress.ip_address(hop).is_private or ipaddress.ip_address(hop).is_loopback):
                return hop
        except ValueError:
            return peer  # garbage in the trusted-proxy part: do not let it pick a bucket
    return hops[0][:45] if hops else peer  # all private (local dev)


# Grids depend only on (lat, lon) and the matrix, which changes on a transit rebuild, not nightly: let Cloudflare
# keep them a day (stale for another if the origin is down) so a traffic spike never reaches the home uplink.
GRID_CACHE = "public, max-age=300, s-maxage=86400, stale-while-revalidate=600, stale-if-error=86400"


class ApiError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


# Rent/neighborhood/stop answers depend only on snapped inputs and data that changes daily: let the CDN keep them briefly.
API_CACHE = {"Cache-Control": "public, s-maxage=300"}
RETRY_AFTER_FAILED_RELOAD = 60  # seconds before a failed (half-written) data load is tried again


def envelope(data=None, error=None, status=200, headers=None):
    return JSONResponse({"ok": error is None, "data": data, "error": error}, status_code=status, headers=headers)


class Origin(BaseModel):
    lat: float = Field(ge=eng.LAT0, le=eng.LAT1)
    lon: float = Field(ge=eng.LON0, le=eng.LON1)


Measure = Literal["total", "per_m2"]


class RentQuery(Origin):
    """Same inputs as the MCP tool rent_stats."""
    radius_m: int = Field(800, ge=200, le=5000)
    rooms: int | None = Field(None, ge=1, le=4)
    min_area: int | None = Field(None, ge=0, le=1000)
    max_area: int | None = Field(None, ge=0, le=1000)
    measure: Measure = "total"


class HoodsQuery(Origin):
    """Same inputs as the MCP tool rank_neighborhoods (lat/lon = office)."""
    max_commute_min: int | None = Field(None, ge=1, le=120)
    max_rent: float | None = Field(None, gt=0)
    rooms: int | None = Field(None, ge=1, le=4)
    measure: Measure = "total"
    limit: int = Field(10, ge=1, le=50)
    sort: Literal["value", "best", "commute", "rent"] = "best"


OriginQ = Annotated[Origin, Query()]
RentQ = Annotated[RentQuery, Query()]
HoodsQ = Annotated[HoodsQuery, Query()]


# Per-request DB limits on every pool connection: a stuck query or lock must not hold one of the 8 connections for good.
POOL_KWARGS = {"connect_timeout": 3, "options": "-c statement_timeout=%d -c lock_timeout=%d" % (
    int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "5000")), int(os.environ.get("DB_LOCK_TIMEOUT_MS", "2000")))}


def pool_kwargs(dsn):
    """POOL_KWARGS, but the timeouts are appended to any `options` already in the DSN instead of replacing them."""
    try:
        from psycopg.conninfo import conninfo_to_dict
        have = (conninfo_to_dict(dsn).get("options") or "").strip()
    except Exception:  # an unparsable DSN fails later, at connect time, with a clearer error
        have = ""
    return {**POOL_KWARGS, "options": f"{have} {POOL_KWARGS['options']}".strip()}


def create_app(database_url=None, data_dir=None, rate_limit=None, cors=None, clock=time.monotonic):
    database_url = database_url or os.environ.get("DATABASE_URL", "postgresql://localhost/commute_map")
    data_dir = data_dir or os.environ.get("DATA_DIR", str(ROOT / "web" / "data"))
    rate_limit = rate_limit or int(os.environ.get("RATE_LIMIT_PER_MIN", "240"))
    cors = cors or os.environ.get("CORS_ORIGINS", "http://localhost:8765").split(",")
    st = {"pool": None, "engine": None, "source": None, "stops_db": False, "sig": None, "car": None, "walk": None, "bike": None,
          "files_stops": None, "gen": 0, "retry_at": 0.0}
    cache, cache_lock = OrderedDict(), Lock()
    reload_lock = Lock()  # one engine rebuild at a time; the new engine is built aside and swapped under cache_lock
    prune = {"at": clock()}
    hits = defaultdict(deque)  # ponytail: per-process memory, so with N uvicorn workers the real limit is up to N x; use Caddy/redis limiter if that matters

    @asynccontextmanager
    async def lifespan(app):
        # Each grid computation holds ~35 MB; FastAPI's default 40 threads per worker let a burst grow the container to 7.5 GB
        # (load test 2026-10-26) with no extra throughput past the core count. 6 threads: the rest queue instead of allocating.
        anyio.to_thread.current_default_thread_limiter().total_tokens = int(os.environ.get("API_THREADS", "6"))
        if not os.environ.get("SKIP_DB"):  # prod runs without Postgres: do not create a pool that retries a dead DSN forever
            pool = ConnectionPool(database_url, min_size=1, max_size=8, open=False, kwargs=pool_kwargs(database_url))
            try:
                pool.open(wait=True, timeout=5)
                st["pool"] = pool
            except Exception:
                log.exception("database unavailable; DB endpoints will return 503")
                pool.close()
        try:
            loaded = None
            if st["pool"]:
                with st["pool"].connection() as c:
                    c.execute("SET LOCAL statement_timeout = 0")  # startup bulk read (travel_matrix) may exceed the request limit
                    loaded = eng.read_db(c)
                    st["stops_db"] = c.execute("SELECT EXISTS (SELECT 1 FROM core.stop_group)").fetchone()[0]
            if loaded:
                st["source"] = "db"
                st["engine"] = eng.Engine(*loaded, eng.read_walk(data_dir))
                st["files_stops"], st["sig"] = loaded[0], files_sig()
            else:
                st["source"] = "files"
                sig = files_sig()
                loaded = eng.read_files(data_dir)
                st["engine"] = eng.Engine(*loaded, eng.read_walk(data_dir))
                st["files_stops"], st["sig"] = loaded[0], sig
        except Exception:
            log.exception("commute data unreadable (stops.json / matrix.bin / walk.json); data endpoints answer 503, retry in %ds",
                          RETRY_AFTER_FAILED_RELOAD)
            st["source"] = st["source"] or "files"  # pool opened but read_db failed: reload_files must retry from files
            st["retry_at"] = clock() + RETRY_AFTER_FAILED_RELOAD
        try:
            st["car"] = carmod.load(data_dir, office_leg=True)  # built once by pipeline/car/build_graph.py; absent = /api/car answers 503, app falls back
        except Exception:
            log.exception("car graph unreadable; /api/car will return 503")
        for mode in ("walk", "bike"):  # pipeline/walk/build_graph.py; absent = /api/<mode> answers 503, app keeps the straight-line formula
            try:
                st[mode] = carmod.load(data_dir, mode, access_mpm=eng.K[mode]["mPerMin"], access_cap_m=800.0, snap_cap_m=600.0, office_leg=True)
            except Exception:
                log.exception("%s graph unreadable; /api/%s will return 503", mode, mode)
        yield
        if st["pool"]:
            st["pool"].close()

    app = FastAPI(title="commute-map", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=cors, allow_methods=["GET"], allow_headers=["*"],
                       expose_headers=["X-Grid-Rows", "X-Grid-Cols"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method != "OPTIONS":  # CORS preflights are not counted
            ip, now = client_ip(request), clock()
            if now - prune["at"] > 60:  # once a minute, drop idle keys (O(keys), not per request)
                prune["at"] = now
                for k in [k for k, v in hits.items() if not v or now - v[-1] > 60]:
                    del hits[k]
            q = hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= rate_limit:
                wait = max(1, math.ceil(60 - (now - q[0])))
                return envelope(error="rate limit exceeded", status=429, headers={"Retry-After": str(wait)})
            q.append(now)
        try:
            return await call_next(request)
        except Exception:
            rid = uuid.uuid4().hex[:12]
            log.exception("unhandled error on %s (query keys: %s) [request %s]", request.url.path,
                          ",".join(sorted(request.query_params.keys())), rid)  # no values: lat/lon are a home address
            return envelope(error="internal error", status=500, headers={"X-Request-Id": rid})

    @app.exception_handler(ApiError)
    async def _api_err(_, e):
        return envelope(error=e.msg, status=e.status)

    @app.exception_handler(RequestValidationError)
    async def _val_err(_, e):
        msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'][1:])}: {err['msg']}" for err in e.errors())
        return envelope(error=f"invalid parameters: {msg}", status=422)

    @app.exception_handler(404)
    async def _nf(_, e):
        return envelope(error="not found", status=404)

    @app.exception_handler(405)
    async def _na(_, e):
        return envelope(error="method not allowed", status=405)

    def db():
        if not st["pool"]:
            raise ApiError(503, "database unavailable")
        return st["pool"].connection()

    def files_sig():
        d = Path(data_dir)
        return tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in (d / "stops.json", d / "matrix.bin", d / "walk.json"))

    def reload_files():
        """File-sourced matrix only: rebuild the engine when refresh.sh rsyncs new data (a DB matrix is static).
        Single-flight: one thread rebuilds aside while the rest keep serving the old engine; a failed load is retried after 60 s."""
        if st["source"] != "files" or files_sig() == st["sig"] or clock() < st["retry_at"]:
            return
        if not reload_lock.acquire(blocking=False):
            return
        try:
            sig = files_sig()
            if sig == st["sig"]:
                return
            loaded = eng.read_files(data_dir)
            new = eng.Engine(*loaded, eng.read_walk(data_dir))
            with cache_lock:  # swap and invalidate together: grid() only stores results computed on the current generation
                st["engine"], st["files_stops"], st["sig"] = new, loaded[0], sig
                st["gen"] += 1
                cache.clear()
        except Exception:  # half-written sync: keep the old engine
            log.exception("data reload failed; serving previous data, retry in %ds", RETRY_AFTER_FAILED_RELOAD)
            st["retry_at"] = clock() + RETRY_AFTER_FAILED_RELOAD
        finally:
            reload_lock.release()

    offers = {"cur": None, "retry_at": 0.0}  # cur = (Dojazdomat, sig), replaced as one tuple
    offers_build = Lock()

    def dojazdomat():
        """The MCP core (mcp_server/core.py), built on first use and rebuilt when refresh.sh rsyncs new data: /api/rent and
        /api/neighborhoods answer from offers.json with the same code as the MCP tools. Single-flight: the rebuild happens
        outside any lock readers take, the old core keeps serving meanwhile, a failed rebuild is retried after 60 s.
        ponytail: it holds a second Engine per worker (~200 MB); share st["engine"] if memory matters."""
        d = Path(data_dir)
        sig = tuple(f.stat().st_mtime_ns if f.exists() else 0
                    for f in (d / "offers.json", d / "neighborhoods.json", d / "stops.json", d / "matrix.bin", d / "walk.json"))
        cur = offers["cur"]
        if cur and cur[1] == sig:
            return cur[0]
        if clock() < offers["retry_at"]:
            if cur:
                return cur[0]
            raise ApiError(503, "offers data unavailable")
        if cur:
            if not offers_build.acquire(blocking=False):
                return cur[0]  # someone else is rebuilding
        else:
            offers_build.acquire()  # first build: nothing to serve yet, wait for it
        try:
            cur = offers["cur"]
            if cur and cur[1] == sig:
                return cur[0]
            if not cur and clock() < offers["retry_at"]:
                raise ApiError(503, "offers data unavailable")
            try:
                offers["cur"] = (Dojazdomat(d), sig)
            except Exception:
                log.exception("offers data unavailable; retry in %ds", RETRY_AFTER_FAILED_RELOAD)
                offers["retry_at"] = clock() + RETRY_AFTER_FAILED_RELOAD
                if not cur:
                    raise ApiError(503, "offers data unavailable") from None
                return cur[0]
            return offers["cur"][0]
        finally:
            offers_build.release()

    def grid(lat, lon):
        reload_files()
        key = (round(lat, 5), round(lon, 5))
        with cache_lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        with cache_lock:
            e, gen = st["engine"], st["gen"]
        if e is None:
            raise ApiError(503, "commute data unavailable")
        g = e.grid(lat, lon)
        with cache_lock:
            if gen == st["gen"]:  # an engine swap during the computation makes g stale: serve it once, do not cache it
                cache[key] = g
                while len(cache) > 32:
                    cache.popitem(last=False)
        return g

    @app.get("/api/health")
    def health():
        ok_db = False
        if st["pool"]:
            try:
                with db() as c:
                    c.execute("SELECT 1")
                ok_db = True
            except Exception:
                log.exception("health db check failed")
        e = st["engine"]
        return envelope({"db": ok_db, "stops": e.n if e else 0, "matrix_source": st["source"], "car": st["car"] is not None, "walk": st["walk"] is not None, "bike": st["bike"] is not None})

    @app.get("/api/ready")
    def ready():
        """Readiness: 503 until the commute engine is loaded (/api/health stays a 200 liveness probe)."""
        e = st["engine"]
        n = e.n if e else 0
        return envelope({"stops": n}, error=None if n else "commute data unavailable", status=200 if n else 503)

    @app.get("/api/commute")
    def commute(o: OriginQ):
        g = grid(o.lat, o.lon)
        head = np.array([eng.ROWS, eng.COLS], "<u2").tobytes()
        return Response(head + g.tobytes(), media_type="application/octet-stream",
                        headers={"X-Grid-Rows": str(eng.ROWS), "X-Grid-Cols": str(eng.COLS),
                                 "X-Grid-Lat0": str(eng.LAT0), "X-Grid-Lon0": str(eng.LON0),
                                 "X-Grid-Dlat": str(eng.DLAT), "X-Grid-Dlon": str(eng.DLON),
                                 "Cache-Control": GRID_CACHE})

    def routed(mode, o):
        if not st[mode]:
            raise ApiError(503, f"{mode} graph unavailable")
        g = st[mode].grid(o.lat, o.lon)
        if g is None:
            raise ApiError(404, f"office is not near a {mode} network")
        head = np.array([eng.ROWS, eng.COLS], "<u2").tobytes()
        return Response(head + g.tobytes(), media_type="application/octet-stream",
                        headers={"X-Grid-Rows": str(eng.ROWS), "X-Grid-Cols": str(eng.COLS), "Cache-Control": GRID_CACHE})

    @app.get("/api/car")
    def car(o: OriginQ):
        return routed("car", o)

    @app.get("/api/walk")
    def walk(o: OriginQ):
        """Walk minutes on the OSM footpath network (80 m/min), uint8 grid, 255 = unreached or outside the graph box."""
        return routed("walk", o)

    @app.get("/api/bike")
    def bike(o: OriginQ):
        """Bike minutes on the OSM bike network (250 m/min), without the app's +2 min; same grid layout as /api/car."""
        return routed("bike", o)

    @app.get("/api/rent")
    def rent(q: RentQ):
        """= MCP rent_stats: n, median, p25, p75 of ads within radius_m (suppressed below 20)."""
        dj = dojazdomat()
        try:
            return envelope(dj.rent_stats(q.lat, q.lon, q.radius_m, q.rooms, q.min_area, q.max_area, q.measure), headers=API_CACHE)
        except ValueError as e:
            raise ApiError(422, str(e)) from None

    @app.get("/api/neighborhoods")
    def neighborhoods(q: HoodsQ):
        """= MCP rank_neighborhoods, the map's ranking for an office at lat/lon."""
        dj = dojazdomat()
        try:
            return envelope(dj.rank_neighborhoods(q.lat, q.lon, q.max_commute_min, q.max_rent, q.rooms, q.measure, q.limit, q.sort), headers=API_CACHE)
        except ValueError as e:
            raise ApiError(422, str(e)) from None

    @app.get("/api/stops")
    def stops(q: Annotated[str, Query(min_length=2, max_length=60)]):
        if st["pool"] and st["stops_db"]:
            with db() as c:
                return envelope(queries.stops(c, q), headers=API_CACHE)
        if st["files_stops"] is None:
            raise ApiError(503, "stops unavailable")
        ql = _fold(q.strip())  # ponytail: folded substring match (same as MCP find_place), exact, then prefix, then shortest first
        hits_ = sorted((0 if f == ql else 1 if f.startswith(ql) else 2, len(f), i)
                       for i, f in ((i, _fold(s["name"])) for i, s in enumerate(st["files_stops"])) if ql in f)
        return envelope([{k: st["files_stops"][i][k] for k in ("id", "name", "lat", "lon")} for *_, i in hits_[:10]], headers=API_CACHE)

    return app


app = create_app()
