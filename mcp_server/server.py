"""Dojazdomat MCP server (mcp 1.x FastMCP): streamable HTTP at /mcp, or stdio."""
import functools
import ipaddress
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
from mcp.types import ToolAnnotations
from pydantic import Field

from api import engine as eng
from pipeline import city

from .core import Dojazdomat

log = logging.getLogger("mcp_server")
C = city.load()
NAME = C["name"]


def _city_doc(f):  # tool descriptions are the docstrings: put the city name in
    f.__doc__ = f.__doc__.replace("{city}", NAME["en"])
    return f

INSTRUCTIONS = """Dojazdomat (Dojazd x Czynsz, {NOM}) combines public-transport commute times with asking rents \
from Polish rental portals, so you can find where in {EN} rent is low for a given commute. \
Dojazdomat laczy czas dojazdu komunikacja miejska z cenami ofertowymi wynajmu w {LOC}.

Suggested workflow: 1) find_place to turn a workplace/stop/neighborhood name into lat/lon; \
2) rank_neighborhoods with that office location (optionally mode, max_commute_min, max_rent, rooms) to see \
neighborhoods by cost, value, commute and Pareto flag; rank_for_two when two people work in two places; \
3) compare_neighborhoods for 2-4 named neighborhoods side by side, reachable_neighborhoods for everything within N minutes, \
rent_stats around a chosen point, rent_trend for the median over time, commute_time for any home -> office check. \
dataset_info lists sources, dates and caveats.

Only aggregates (median, p25, p75, counts from >= 20 listings) are returned, never individual listings. \
Rents are monthly asking prices in PLN (incl. czynsz administracyjny where stated). Area: {EN} only, \
lat {LAT0}-{LAT1}, lon {LON0}-{LON1}. Commute modes: transit (weekday morning, door to door), bike, walk, car (rush hour); \
minutes always run from home to the office, as on the map."""

INSTRUCTIONS = INSTRUCTIONS.replace("{LAT0}", str(eng.LAT0)).replace("{LAT1}", str(eng.LAT1)) \
    .replace("{LON0}", str(eng.LON0)).replace("{LON1}", str(eng.LON1)) \
    .replace("{NOM}", NAME["nom"]).replace("{EN}", NAME["en"]).replace("{LOC}", NAME["loc"])
Lat = Annotated[float, Field(description=f"Latitude, WGS84, {eng.LAT0}-{eng.LAT1} ({NAME['nom']} and commuter belt)", ge=eng.LAT0, le=eng.LAT1)]
Lon = Annotated[float, Field(description=f"Longitude, WGS84, {eng.LON0}-{eng.LON1} ({NAME['nom']} and commuter belt)", ge=eng.LON0, le=eng.LON1)]
Rooms = Annotated[int | None, Field(description="Rooms 1-4 (4 = 4+); omit for any / liczba pokoi", ge=1, le=4)]
Measure = Annotated[Literal["total", "per_m2"], Field(description="'total' PLN/month or 'per_m2' PLN/m2/month")]
Mode = Annotated[Literal["transit", "bike", "walk", "car"], Field(description="transit (default, weekday morning public transport + walking), bike, walk or car (rush hour) / komunikacja, rower, pieszo, auto")]
RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


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


def _trusted_peer(peer):
    """cloudflared connects from loopback/private; a public peer must not choose its own rate-limit key."""
    try:
        a = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return a.is_loopback or a.is_private


class RateLimitMiddleware:
    """Pure ASGI, keyed on CF-Connecting-IP (only from a loopback/private peer, i.e. the tunnel) else socket peer."""

    def __init__(self, app, rl):
        self.app, self.rl = app, rl

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = dict(scope["headers"])
            peer = (scope.get("client") or ("?",))[0]
            ip = (h.get(b"cf-connecting-ip", b"").decode()[:45] if _trusted_peer(peer) else "") or peer
            if not self.rl.allow(ip):
                body = json.dumps({"error": "rate limit exceeded, retry later"}).encode()
                await send({"type": "http.response.start", "status": 429, "headers": [
                    (b"content-type", b"application/json"), (b"retry-after", b"60"), (b"content-length", str(len(body)).encode())]})
                return await send({"type": "http.response.body", "body": body})
        await self.app(scope, receive, send)


class DiscoverShim:
    """Pure ASGI. MCP 2026-07-28 clients probe `server/discover`; mcp 1.30.0 answers -32602 with a traceback in the log.
    Answer the JSON-RPC standard -32601 Method not found instead. Everything else passes through untouched."""

    MAX_BODY = 65536

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].startswith("/mcp"):
            return await self.app(scope, receive, send)
        chunks, more, size = [], True, 0
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            chunks.append(msg.get("body", b""))
            size += len(chunks[-1])
            more = msg.get("more_body", False)
            if size > self.MAX_BODY:
                break
        body = b"".join(chunks)
        req = None
        if size <= self.MAX_BODY:
            try:
                req = json.loads(body)
            except ValueError:
                pass
        if isinstance(req, dict) and req.get("method") == "server/discover" and "id" in req:
            out = json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "Method not found"}}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]})
            return await send({"type": "http.response.body", "body": out})
        sent = False

        async def replay():  # hand the buffered body to the app, then fall back to the real receive
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": more}
            return await receive()
        await self.app(scope, replay, send)


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


class Reloading:
    """Dojazdomat that rebuilds itself when a data file changes (refresh.sh rsyncs web/data under a live server)."""

    def __init__(self, data_dir=None):
        self.dj, self.lock = Dojazdomat(data_dir), Lock()
        self.sig = self._sig()

    def _sig(self):
        d = self.dj.data_dir
        return tuple((f.name, f.stat().st_mtime_ns) for f in sorted(d.glob("*.json")) + [d / "matrix.bin"] if f.exists())

    def __getattr__(self, name):
        with self.lock:
            sig = self._sig()
            if sig != self.sig:
                try:
                    self.dj, self.sig = Dojazdomat(self.dj.data_dir), sig
                except Exception:  # half-written sync: keep serving the old data, retry on the next call
                    log.exception("data reload failed; serving previous data")
            return getattr(self.dj, name)


# DNS-rebinding protection (mcp SDK: TransportSecuritySettings). Caddy passes the public Host header through unchanged, so the
# public name must be listed; native MCP clients send no Origin, browsers send one that must match allowed_origins (":*" = any port).
PUBLIC_HOSTS = ["dojazdomat.pl", "www.dojazdomat.pl", "dojazdomat.sapcorp.eu"]  # .pl is primary since 2026-10-25; the old name still serves /mcp
DEFAULT_HOSTS = [*PUBLIC_HOSTS, "localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
DEFAULT_ORIGINS = [*(f"https://{h}" for h in PUBLIC_HOSTS), "http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"]


def transport_security():
    """Env DOJAZDOMAT_ALLOWED_HOSTS (comma list) adds hosts (and their https origin) to the defaults; protection is always on."""
    extra = [h.strip() for h in os.environ.get("DOJAZDOMAT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=DEFAULT_HOSTS + extra,
                                     allowed_origins=DEFAULT_ORIGINS + [f"https://{h}" for h in extra])


def build_server(data_dir=None, host="127.0.0.1", port=8790):
    dj = Reloading(data_dir)
    sec = transport_security()
    mcp = FastMCP("dojazdomat", instructions=INSTRUCTIONS, host=host, port=port, streamable_http_path="/mcp",
                  stateless_http=True, json_response=True, transport_security=sec)

    @mcp.tool(annotations=RO)
    @_safe
    def find_place(query: Annotated[str, Field(description="Name fragment, e.g. 'Kabaty', 'Rondo ONZ', 'Mokotow'. Diacritics optional.", min_length=2, max_length=60)],
                   limit: Annotated[int, Field(ge=1, le=25)] = 10) -> dict[str, Any]:
        """Find transit stop groups and neighborhoods by name and get their lat/lon (znajdz przystanek lub osiedle po nazwie).
        Use the coordinates as input for the other tools."""
        return dj.find_place(query, limit)

    @mcp.tool(annotations=RO)
    @_safe
    def commute_time(from_lat: Lat, from_lon: Lon, to_lat: Lat, to_lon: Lon, mode: Mode = "transit") -> dict[str, Any]:
        """Minutes from home (from) to work (to), same model as the Dojazdomat map (czas dojazdu z domu do pracy). transit:
        door to door by public transport plus walking, weekday 7-9 average, with the nearest stops at both ends; bike, walk: OpenStreetMap
        paths (bike +2 min); car: rush-hour driving. minutes is null when unreachable (over ~2 h)."""
        return dj.commute_time(from_lat, from_lon, to_lat, to_lon, mode)

    @mcp.tool(annotations=RO)
    @_safe
    def rent_stats(lat: Lat, lon: Lon, radius_m: Annotated[int, Field(description="Radius in metres, 200-5000; snapped to the nearest of 300, 500, 750, 1000, 1500, 2000, 3000, 5000 (the answer states the radius used)", ge=200, le=5000)] = 800,
                   rooms: Rooms = None, min_area: Annotated[int | None, Field(description="Min area m2", ge=0, le=1000)] = None,
                   max_area: Annotated[int | None, Field(description="Max area m2", ge=0, le=1000)] = None,
                   measure: Measure = "total") -> dict[str, Any]:
        """Asking-rent statistics (n, median, p25, p75) of rental ads within radius_m of a point (statystyki cen najmu w promieniu).
        Aggregates only; if fewer than 20 ads match, the result is suppressed (widen radius or relax filters). Radius is snapped to a fixed step (see radius_m) so queries cannot be differenced to single ads."""
        return dj.rent_stats(lat, lon, radius_m, rooms, min_area, max_area, measure)

    @mcp.tool(annotations=RO)
    @_safe
    @_city_doc
    def rank_neighborhoods(office_lat: Lat, office_lon: Lon,
                           max_commute_min: Annotated[int | None, Field(description="Max commute minutes", ge=1, le=120)] = None,
                           max_rent: Annotated[float | None, Field(description="Max neighborhood median rent (PLN, or PLN/m2 if measure=per_m2)", gt=0)] = None,
                           rooms: Rooms = None, measure: Measure = "total",
                           limit: Annotated[int, Field(ge=1, le=50)] = 10,
                           sort: Annotated[Literal["value", "best", "commute", "rent"], Field(description="best = rent + commute valued at the minimum wage (default, the map's order, total measure only); value = score vs typical rent in the same commute band; commute = fastest first; rent = cheapest first")] = "best",
                           mode: Mode = "transit") -> dict[str, Any]:
        """Rank {city} neighborhoods for a workplace: median rent, commute minutes, value score (rent vs typical rent in the same
        commute band, like the map), monthly_cost (rent + commute valued at the minimum wage) and Pareto flag (no neighborhood both faster and cheaper)
        (ranking osiedli: czynsz, dojazd, wartosc, front Pareto). Same data and rules as the map: neighborhoods with fewer than 20 ads are skipped. Sorted by best (rent + commute cost, the map's order) unless sort is given."""
        return dj.rank_neighborhoods(office_lat, office_lon, max_commute_min, max_rent, rooms, measure, limit, sort, mode)

    @mcp.tool(annotations=RO)
    @_safe
    @_city_doc
    def rank_for_two(a_lat: Lat, a_lon: Lon, b_lat: Lat, b_lon: Lon, mode_a: Mode = "transit", mode_b: Mode = "transit",
                     max_commute_min: Annotated[int | None, Field(description="Max commute minutes for each person", ge=1, le=120)] = None,
                     max_rent: Annotated[float | None, Field(description="Max neighborhood median rent, PLN/month", gt=0)] = None,
                     rooms: Rooms = None, limit: Annotated[int, Field(ge=1, le=50)] = 10,
                     sort: Annotated[Literal["best", "fair", "rent"], Field(description="best = rent + both commutes valued at the minimum wage (default); fair = shortest longer commute first; rent = cheapest first")] = "best") -> dict[str, Any]:
        """Rank {city} neighborhoods for two people who work in two places (para, dwa biura): median rent, both commutes and
        monthly_cost (rent + both commutes valued at the minimum wage). Each person can use a different mode. Neighborhoods with
        fewer than 20 ads are skipped."""
        return dj.rank_for_two(a_lat, a_lon, b_lat, b_lon, mode_a, mode_b, max_commute_min, max_rent, rooms, limit, sort)

    @mcp.tool(annotations=RO)
    @_safe
    def compare_neighborhoods(names: Annotated[list[Annotated[str, Field(min_length=2, max_length=60)]], Field(description="2-4 neighborhood names, e.g. ['Bródno', 'Gocław']", min_length=2, max_length=4)],
                              rooms: Rooms = None, measure: Measure = "total",
                              office_lat: Annotated[float | None, Field(description="Optional workplace latitude, adds commute_min", ge=eng.LAT0, le=eng.LAT1)] = None,
                              office_lon: Annotated[float | None, Field(description="Optional workplace longitude", ge=eng.LON0, le=eng.LON1)] = None,
                              mode: Mode = "transit") -> dict[str, Any]:
        """Compare 2-4 neighborhoods side by side (porownaj osiedla): median rent, p25, p75 and number of ads, plus the commute to
        an optional workplace. A neighborhood with fewer than 20 matching ads shows suppressed instead of a price."""
        return dj.compare_neighborhoods(names, rooms, measure, office_lat, office_lon, mode)

    @mcp.tool(annotations=RO)
    @_safe
    def reachable_neighborhoods(lat: Lat, lon: Lon, max_minutes: Annotated[int, Field(description="Max minutes from home to this point", ge=1, le=120)] = 30,
                                mode: Mode = "transit", rooms: Rooms = None,
                                limit: Annotated[int, Field(ge=1, le=100)] = 50) -> dict[str, Any]:
        """Every neighborhood from which this point (e.g. the office) is reached within max_minutes, fastest first (zasieg w N minut).
        Lists also neighborhoods with too few ads for a price (median_rent null), unlike rank_neighborhoods."""
        return dj.reachable_neighborhoods(lat, lon, max_minutes, mode, rooms, limit)

    @mcp.tool(annotations=RO)
    @_safe
    def rent_trend(area: Annotated[str, Field(description=f"'{NAME['nom']}' (default) for the whole city, a district or a neighborhood name", min_length=2, max_length=60)] = NAME["nom"],
                   rooms: Rooms = None) -> dict[str, Any]:
        """Median asking rent per day of data for the city, a district or a neighborhood, and the change between the first and the last
        day (trend czynszu). Daily snapshots of the map's own data; days with fewer than 20 ads show null."""
        return dj.rent_trend(area, rooms)

    @mcp.tool(annotations=RO)
    @_safe
    def dataset_info() -> dict[str, Any]:
        """Data dates, sources, listing counts per portal, method notes and caveats (zrodla, daty, ograniczenia danych)."""
        return dj.dataset_info()

    return mcp


def build_http_app(data_dir=None):
    """ASGI app: /mcp behind a per-IP rate limit (env DOJAZDOMAT_RATE_PER_MIN, default 60)."""
    app = build_server(data_dir).streamable_http_app()
    return RateLimitMiddleware(DiscoverShim(app), RateLimit(int(os.environ.get("DOJAZDOMAT_RATE_PER_MIN", "60"))))
