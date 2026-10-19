"""FastAPI app. Run: .venv/bin/uvicorn api.main:app --port 8766"""
import logging
import os
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

import numpy as np
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from . import engine as eng
from . import queries

log = logging.getLogger("api")
ROOT = Path(__file__).resolve().parents[1]


class ApiError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def envelope(data=None, error=None, status=200):
    return JSONResponse({"ok": error is None, "data": data, "error": error}, status_code=status)


class Origin(BaseModel):
    lat: float = Field(ge=eng.LAT0, le=eng.LAT1)
    lon: float = Field(ge=eng.LON0, le=eng.LON1)


class RentFilters(BaseModel):
    rooms: int | None = Field(None, ge=1, le=4)
    min_area: int | None = Field(None, ge=0, le=1000)
    max_area: int | None = Field(None, ge=0, le=1000)
    flags: int = Field(0, ge=0, le=511)
    portal: str | None = Field(None, max_length=40)
    measure: Literal["total", "per_m2"] = "total"


class OriginRent(Origin, RentFilters):
    pass


OriginQ = Annotated[Origin, Query()]
OriginRentQ = Annotated[OriginRent, Query()]
RentQ = Annotated[RentFilters, Query()]


def create_app(database_url=None, data_dir=None, rate_limit=None, cors=None):
    database_url = database_url or os.environ.get("DATABASE_URL", "postgresql://localhost/commute_map")
    data_dir = data_dir or os.environ.get("DATA_DIR", str(ROOT / "web" / "data"))
    rate_limit = rate_limit or int(os.environ.get("RATE_LIMIT_PER_MIN", "240"))
    cors = cors or os.environ.get("CORS_ORIGINS", "http://localhost:8765").split(",")
    st = {"pool": None, "engine": None, "source": None, "stops_db": False}
    cache, cache_lock = OrderedDict(), Lock()
    hits = defaultdict(deque)  # ponytail: per-process memory; use Caddy/redis limiter if scaled out

    @asynccontextmanager
    async def lifespan(app):
        pool = ConnectionPool(database_url, min_size=1, max_size=8, open=False, kwargs={"connect_timeout": 3})
        try:
            pool.open(wait=True, timeout=5)
            st["pool"] = pool
        except Exception:
            log.exception("database unavailable; DB endpoints will return 503")
        loaded = None
        if st["pool"]:
            with pool.connection() as c:
                loaded = eng.read_db(c)
                st["stops_db"] = c.execute("SELECT EXISTS (SELECT 1 FROM core.stop_group)").fetchone()[0]
        if loaded:
            st["source"] = "db"
        else:
            loaded, st["source"] = eng.read_files(data_dir), "files"
        st["engine"] = eng.Engine(*loaded, eng.read_walk(data_dir))
        st["files_stops"] = loaded[0]
        yield
        if st["pool"]:
            pool.close()

    app = FastAPI(title="commute-map", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=cors, allow_methods=["GET"], allow_headers=["*"],
                       expose_headers=["X-Grid-Rows", "X-Grid-Cols"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        ip, now = request.client.host if request.client else "?", time.monotonic()
        q = hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= rate_limit:
            return envelope(error="rate limit exceeded", status=429)
        q.append(now)
        try:
            return await call_next(request)
        except Exception:
            log.exception("unhandled error on %s", request.url.path)
            return envelope(error="internal error", status=500)

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

    def grid(lat, lon):
        key = (round(lat, 5), round(lon, 5))
        with cache_lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        g = st["engine"].grid(lat, lon)
        with cache_lock:
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
        return envelope({"db": ok_db, "stops": e.n, "matrix_source": st["source"]})

    @app.get("/api/commute")
    def commute(o: OriginQ):
        g = grid(o.lat, o.lon)
        head = np.array([eng.ROWS, eng.COLS], "<u2").tobytes()
        return Response(head + g.tobytes(), media_type="application/octet-stream",
                        headers={"X-Grid-Rows": str(eng.ROWS), "X-Grid-Cols": str(eng.COLS),
                                 "X-Grid-Lat0": str(eng.LAT0), "X-Grid-Lon0": str(eng.LON0),
                                 "X-Grid-Dlat": str(eng.DLAT), "X-Grid-Dlon": str(eng.DLON)})

    @app.get("/api/rent")
    def rent(f: RentQ):
        with db() as c:
            return envelope(queries.rent(c, f.model_dump()))

    @app.get("/api/neighborhoods")
    def neighborhoods(q: OriginRentQ):
        o, f = q, q.model_dump()
        with db() as c:
            hoods = queries.neighborhoods(c, f)
        g = grid(o.lat, o.lon)
        for h in hoods:
            cell = eng.cell_of(h["lat"], h["lon"])
            v = int(g[cell]) if cell else eng.UNREACHED
            h["commute"] = None if v == eng.UNREACHED else v
            h["front"] = False
        pts = [h for h in hoods if h["median"] is not None and h["commute"] is not None]
        for p in pts:
            p["front"] = not any(
                q["commute"] <= p["commute"] and q["median"] <= p["median"]
                and (q["commute"] < p["commute"] or q["median"] < p["median"]) for q in pts)
        return envelope(hoods)

    @app.get("/api/stops")
    def stops(q: Annotated[str, Query(min_length=2, max_length=60)]):
        if st["pool"] and st["stops_db"]:
            with db() as c:
                return envelope(queries.stops(c, q))
        ql = q.lower()  # ponytail: substring match, no trigram ranking when stop_group table is empty
        hits_ = [{"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"]}
                 for s in st["files_stops"] if ql in s["name"].lower()]
        return envelope(hits_[:10])

    return app


app = create_app()
