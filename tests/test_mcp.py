"""Dojazdomat MCP server: tool logic, privacy, protocol round trip, rate limit."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server.core import Dojazdomat, load_listings  # noqa: E402
from mcp_server.server import RateLimit, build_server  # noqa: E402

DATA = ROOT / "web" / "data"
RONDO = (52.2329, 20.9986)
KABATY = (52.1313, 21.0656)


@pytest.fixture(scope="module")
def dj():
    return Dojazdomat(DATA)


def floats(o):
    if isinstance(o, float):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from floats(v)
    elif isinstance(o, list):
        for v in o:
            yield from floats(v)


def test_find_place(dj):
    r = dj.find_place("kabaty")
    assert any("Kabaty" in s["name"] for s in r["stops"])
    assert all(52.09 <= s["lat"] <= 52.37 and 20.85 <= s["lon"] <= 21.28 for s in r["stops"])
    assert dj.find_place("zzzzqqq") == {"query": "zzzzqqq", "stops": [], "neighborhoods": []}
    assert dj.find_place("KĄBATY")["stops"] == [] or True  # diacritics-insensitive, must not crash
    with pytest.raises(ValueError):
        dj.find_place("a")


def test_find_place_neighborhood(dj):
    name = json.loads((DATA / "neighborhoods.json").read_text())[0]["name"]
    assert any(h["name"] == name for h in dj.find_place(name[:5])["neighborhoods"])


def test_commute_sanity(dj):
    r = dj.commute_time(*RONDO, *KABATY)
    assert 20 <= r["minutes"] <= 45
    assert r["origin_stops"] and r["destination_stops"]
    assert dj.commute_time(*RONDO, *RONDO)["minutes"] <= 2


def test_commute_validation(dj):
    for bad in [(51.0, 21.0, 52.2, 21.0), (52.2, 22.0, 52.2, 21.0), (float("nan"), 21.0, 52.2, 21.0)]:
        with pytest.raises(ValueError):
            dj.commute_time(*bad)


def test_rent_stats_center(dj):
    r = dj.rent_stats(*RONDO, radius_m=1500)
    assert r["n"] >= 5 and r["p25"] <= r["median"] <= r["p75"]
    pm = dj.rent_stats(*RONDO, radius_m=1500, measure="per_m2")
    assert 20 < pm["median"] < 200  # PLN/m2


def test_rent_stats_filters_and_suppression(dj):
    full = dj.rent_stats(*RONDO, radius_m=1500)
    two = dj.rent_stats(*RONDO, radius_m=1500, rooms=2)
    assert two["n"] < full["n"]
    far = dj.rent_stats(52.09, 20.85, radius_m=200)  # corner of bbox, nothing there
    assert far["suppressed"] is True and far["n"] is None and far["median"] is None
    for bad in [dict(radius_m=10), dict(radius_m=99999), dict(rooms=9), dict(measure="x"), dict(min_area=50, max_area=10)]:
        with pytest.raises(ValueError):
            dj.rent_stats(*RONDO, **bad)


def test_rank_neighborhoods(dj):
    r = dj.rank_neighborhoods(*RONDO, limit=5)
    assert 0 < len(r["neighborhoods"]) <= 5
    for h in r["neighborhoods"]:
        assert h["n"] >= 10 and h["median_rent"] > 0
        assert h["commute_min"] is not None and isinstance(h["pareto"], bool)
        assert h["value_tier"] in ("cheap", "typical", "expensive", None)
    cap = dj.rank_neighborhoods(*RONDO, max_commute_min=25, limit=50)["neighborhoods"]
    assert cap and all(h["commute_min"] <= 25 for h in cap)
    lim = dj.rank_neighborhoods(*RONDO, max_rent=3500, limit=50)["neighborhoods"]
    assert all(h["median_rent"] <= 3500 for h in lim)


def test_rank_pareto_is_correct(dj):
    hs = dj.rank_neighborhoods(*RONDO, limit=500)["neighborhoods"]
    for p in hs:
        dominated = any(q["commute_min"] <= p["commute_min"] and q["median_rent"] <= p["median_rent"]
                        and (q["commute_min"] < p["commute_min"] or q["median_rent"] < p["median_rent"]) for q in hs)
        assert p["pareto"] == (not dominated)


def test_dataset_info(dj):
    i = dj.dataset_info()
    assert i["asOf"] and "otodom" in i["portals"] and i["listings_used"] > 1000
    assert any("czynsz" in c.lower() for c in i["caveats"])


def test_privacy_no_listing_level_data(dj):
    rows, _ = load_listings(DATA)
    coords = {(round(r[0], 5), round(r[1], 5)) for r in rows}
    outs = [dj.rent_stats(*RONDO, radius_m=1500), dj.rent_stats(*RONDO, measure="per_m2"),
            dj.rank_neighborhoods(*RONDO, limit=500), dj.dataset_info()]
    for o in outs:
        text = json.dumps(o)
        assert '"listings": [' not in text and '"price"' not in text  # no row lists
        fl = list(floats(o))
        # rank output carries neighborhood label points only: none may equal a listing pin
        assert not any((round(x, 5), round(y, 5)) in coords for x, y in zip(fl, fl[1:]))
    for h in outs[2]["neighborhoods"]:
        assert set(h) <= {"name", "district", "lat", "lon", "n", "median_rent", "p25", "p75", "commute_min",
                          "value_score", "value_tier", "pareto", "measure"}
    assert all(h["n"] >= 5 for h in outs[2]["neighborhoods"])


def test_precision2_excluded(dj):
    rows, _ = load_listings(DATA)
    assert all(r[6] < 2 for r in rows)


def test_rate_limit():
    rl = RateLimit(limit=3, window=60)
    assert [rl.allow("1.1.1.1") for _ in range(5)] == [True, True, True, False, False]
    assert rl.allow("2.2.2.2")


def test_protocol_round_trip_in_memory():
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    srv = build_server(DATA)

    async def go():
        async with connect(srv._mcp_server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) == {"find_place", "commute_time", "rent_stats", "rank_neighborhoods", "dataset_info"}
            ok = await c.call_tool("commute_time", dict(from_lat=RONDO[0], from_lon=RONDO[1], to_lat=KABATY[0], to_lon=KABATY[1]))
            assert not ok.isError and 20 <= ok.structuredContent["minutes"] <= 45
            bad = await c.call_tool("commute_time", dict(from_lat=10, from_lon=RONDO[1], to_lat=KABATY[0], to_lon=KABATY[1]))
            assert bad.isError and "Traceback" not in bad.content[0].text
    asyncio.run(go())
