"""Dojazdomat MCP server (mcp 1.x FastMCP): streamable HTTP at /mcp, or stdio."""
import functools
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from .core import Dojazdomat

log = logging.getLogger("mcp_server")

INSTRUCTIONS = """Dojazdomat (Dojazd x Czynsz, Warszawa) combines public-transport commute times with asking rents \
from Polish rental portals, so you can find where in Warsaw rent is low for a given commute. \
Dojazdomat laczy czas dojazdu komunikacja miejska z cenami ofertowymi wynajmu w Warszawie.

Suggested workflow: 1) find_place to turn a workplace/stop/neighborhood name into lat/lon; \
2) rank_neighborhoods with that office location (optionally max_commute_min, max_rent, rooms) to see \
neighborhoods by value, commute and Pareto flag; 3) rent_stats around a chosen point for the median and \
quartiles; commute_time for any door-to-door check. dataset_info lists sources, dates and caveats.

Only aggregates (median, p25, p75, counts from >= 5 listings) are returned, never individual listings. \
Rents are monthly asking prices in PLN (incl. czynsz administracyjny where stated). Area: Warsaw only, \
lat 52.09-52.37, lon 20.85-21.28. Commute is weekday morning transit + walking, not driving."""

Lat = Annotated[float, Field(description="Latitude, WGS84, 52.09-52.37 (Warszawa)", ge=52.09, le=52.37)]
Lon = Annotated[float, Field(description="Longitude, WGS84, 20.85-21.28 (Warszawa)", ge=20.85, le=21.28)]
Rooms = Annotated[int | None, Field(description="Rooms 1-4 (4 = 4+); omit for any / liczba pokoi", ge=1, le=4)]
Measure = Annotated[Literal["total", "per_m2"], Field(description="'total' PLN/month or 'per_m2' PLN/m2/month")]


class RateLimit:
    """Sliding window per key. ponytail: in-memory, single process; use Redis/Caddy if scaled out."""

    def __init__(self, limit=60, window=60.0):
        self.limit, self.window, self.hits, self.lock = limit, window, defaultdict(deque), Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self.hits) > 10000:  # drop idle keys
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                    del self.hits[k]
            return True


class RateLimitMiddleware:
    """Pure ASGI, keyed on CF-Connecting-IP (Cloudflare tunnel) else socket peer."""

    def __init__(self, app, rl):
        self.app, self.rl = app, rl

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = dict(scope["headers"])
            ip = h.get(b"cf-connecting-ip", b"").decode()[:45] or (scope.get("client") or ("?",))[0]
            if not self.rl.allow(ip):
                body = json.dumps({"error": "rate limit exceeded, retry later"}).encode()
                await send({"type": "http.response.start", "status": 429, "headers": [
                    (b"content-type", b"application/json"), (b"retry-after", b"60"), (b"content-length", str(len(body)).encode())]})
                return await send({"type": "http.response.body", "body": body})
        await self.app(scope, receive, send)


def _safe(fn):
    """Validation errors reach the client verbatim; anything else is logged and hidden."""
    @functools.wraps(fn)
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except ValueError as e:
            raise ValueError(str(e)) from None
        except Exception:
            log.exception("tool %s failed", fn.__name__)
            raise RuntimeError("Internal error. Try again or change the input.") from None
    return run


def build_server(data_dir=None, host="0.0.0.0", port=8790):
    dj = Dojazdomat(data_dir)
    hosts = [h for h in os.environ.get("DOJAZDOMAT_ALLOWED_HOSTS", "").split(",") if h]
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=bool(hosts), allowed_hosts=hosts, allowed_origins=["*"] if hosts else [])
    mcp = FastMCP("dojazdomat", instructions=INSTRUCTIONS, host=host, port=port, streamable_http_path="/mcp",
                  stateless_http=True, json_response=True, transport_security=sec)

    @mcp.tool()
    @_safe
    def find_place(query: Annotated[str, Field(description="Name fragment, e.g. 'Kabaty', 'Rondo ONZ', 'Mokotow'. Diacritics optional.", min_length=2, max_length=60)],
                   limit: Annotated[int, Field(ge=1, le=25)] = 10) -> dict[str, Any]:
        """Find transit stop groups and neighborhoods by name and get their lat/lon (znajdz przystanek lub osiedle po nazwie).
        Use the coordinates as input for the other tools."""
        return dj.find_place(query, limit)

    @mcp.tool()
    @_safe
    def commute_time(from_lat: Lat, from_lon: Lon, to_lat: Lat, to_lon: Lon) -> dict[str, Any]:
        """Door-to-door minutes by public transport plus walking, weekday morning 7-9 average, same model as the Dojazdomat map
        (czas dojazdu komunikacja miejska + pieszo, usredniony dla dni roboczych 7-9). Also lists nearest stops at both ends.
        minutes is null when the destination is unreachable (over ~2 h)."""
        return dj.commute_time(from_lat, from_lon, to_lat, to_lon)

    @mcp.tool()
    @_safe
    def rent_stats(lat: Lat, lon: Lon, radius_m: Annotated[int, Field(description="Radius in metres, 200-5000", ge=200, le=5000)] = 800,
                   rooms: Rooms = None, min_area: Annotated[int | None, Field(description="Min area m2", ge=0, le=1000)] = None,
                   max_area: Annotated[int | None, Field(description="Max area m2", ge=0, le=1000)] = None,
                   measure: Measure = "total") -> dict[str, Any]:
        """Asking-rent statistics (n, median, p25, p75) of rental ads within radius_m of a point (statystyki cen najmu w promieniu).
        Aggregates only; if fewer than 5 ads match, the result is suppressed (widen radius or relax filters)."""
        return dj.rent_stats(lat, lon, radius_m, rooms, min_area, max_area, measure)

    @mcp.tool()
    @_safe
    def rank_neighborhoods(office_lat: Lat, office_lon: Lon,
                           max_commute_min: Annotated[int | None, Field(description="Max commute minutes", ge=1, le=120)] = None,
                           max_rent: Annotated[float | None, Field(description="Max neighborhood median rent (PLN, or PLN/m2 if measure=per_m2)", gt=0)] = None,
                           rooms: Rooms = None, measure: Measure = "total",
                           limit: Annotated[int, Field(ge=1, le=50)] = 10) -> dict[str, Any]:
        """Rank Warsaw neighborhoods for a workplace: median rent, commute minutes, value score (rent vs typical rent in the same
        commute band, like the map) and Pareto flag (no neighborhood both faster and cheaper)
        (ranking osiedli: czynsz, dojazd, wartosc, front Pareto). Neighborhoods with fewer than 10 ads are skipped. Sorted by value score."""
        return dj.rank_neighborhoods(office_lat, office_lon, max_commute_min, max_rent, rooms, measure, limit)

    @mcp.tool()
    @_safe
    def dataset_info() -> dict[str, Any]:
        """Data dates, sources, listing counts per portal, method notes and caveats (zrodla, daty, ograniczenia danych)."""
        return dj.dataset_info()

    return mcp


def build_http_app(data_dir=None):
    """ASGI app: /mcp behind a per-IP rate limit (env DOJAZDOMAT_RATE_PER_MIN, default 60)."""
    app = build_server(data_dir).streamable_http_app()
    return RateLimitMiddleware(app, RateLimit(int(os.environ.get("DOJAZDOMAT_RATE_PER_MIN", "60"))))
