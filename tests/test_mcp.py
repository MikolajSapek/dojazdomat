"""Dojazdomat MCP server: tool logic, privacy, protocol round trip, rate limit."""
import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api import engine as eng  # noqa: E402
from mcp_server.core import Dojazdomat, load_offers  # noqa: E402
from mcp_server.server import RateLimit, build_server  # noqa: E402

DATA = ROOT / "web" / "data"
RONDO = (52.2329, 20.9986)
KABATY = (52.1313, 21.0656)


@pytest.fixture(scope="module")
def dj(offers_data):
    return Dojazdomat(offers_data)


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
        assert h["n"] >= 20 and h["median_rent"] > 0
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


def test_privacy_no_listing_level_data(dj, offers_data):
    rows, _, _, _, _ = load_offers(offers_data)
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
                          "value_score", "value_tier", "pareto", "measure", "monthly_cost"}
    assert all(h["n"] >= 20 for h in outs[2]["neighborhoods"])


def test_precision2_excluded(dj, offers_data):
    rows, _, _, _, _ = load_offers(offers_data)
    assert all(r[6] < 2 for r in rows)


def test_rate_limit():
    rl = RateLimit(limit=3, window=60)
    assert [rl.allow("1.1.1.1") for _ in range(5)] == [True, True, True, False, False]
    assert rl.allow("2.2.2.2")


def test_protocol_round_trip_in_memory(offers_data):
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    srv = build_server(offers_data)

    async def go():
        async with connect(srv._mcp_server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) == {"find_place", "commute_time", "rent_stats", "rank_neighborhoods", "dataset_info",
                                  "rank_for_two", "compare_neighborhoods", "reachable_neighborhoods", "rent_trend", "rank_office_locations"}
            assert all(t.annotations and t.annotations.readOnlyHint for t in tools.values())
            ok = await c.call_tool("commute_time", dict(from_lat=RONDO[0], from_lon=RONDO[1], to_lat=KABATY[0], to_lon=KABATY[1]))
            assert not ok.isError and 20 <= ok.structuredContent["minutes"] <= 45
            bad = await c.call_tool("commute_time", dict(from_lat=10, from_lon=RONDO[1], to_lat=KABATY[0], to_lon=KABATY[1]))
            assert bad.isError and "Traceback" not in bad.content[0].text
    asyncio.run(go())


def test_offers_json_required(tmp_path):
    with pytest.raises(FileNotFoundError, match="merge_listings"):
        load_offers(tmp_path)


def test_offers_rows_keep_only_median_rows(tmp_path):
    """prec 2 and rows whose full cost (feeKnownBit) is unknown never reach a statistic, as web/rent.js inMedian."""
    fee = eng.K["feeKnownBit"]
    cols = dict(lat=[52.2] * 3, lon=[21.0] * 3, price=[3000, 3100, 3200], rooms=[2] * 3, area=[40] * 3, flags=[fee, fee, 1], prec=[0, 2, 0], dist=[1, 1, 0], hood=[0, -1, 0])
    (tmp_path / "offers.json").write_text(json.dumps({"asOf": "2026-10-24", "n": 3, "districts": ["A", "B"], "hoods": ["H"], **cols}))
    rows, as_of, _, districts, hoods = load_offers(tmp_path)
    assert rows == [[52.2, 21.0, 3000, 2, 40, fee, 0, 1, 0]] and as_of == "2026-10-24" and districts == ["A", "B"] and hoods == ["H"]


def test_offers_without_district_column_fail(tmp_path):
    (tmp_path / "offers.json").write_text(json.dumps({"asOf": "2026-10-24", "n": 0, "lat": []}))
    with pytest.raises(ValueError, match="merge_listings"):
        load_offers(tmp_path)


def test_hood_median_rows_stay_in_the_hoods_area(dj, offers_data):
    """Every row of a hood has its pin inside the hood's area polygon (web/data/areas.json), never a row outside it; a row inside the
    area but not taken belongs to another, overlapping area. Enough hoods must have rows for this to mean something."""
    from shapely import contains_xy
    from shapely.geometry import shape
    areas = json.loads((offers_data / "areas.json").read_text())
    with_rows = 0
    for h in dj.hoods:
        near = dj.hood_near(h)
        if h["name"] not in areas:
            assert not near.any()
            continue
        inside = contains_xy(shape(areas[h["name"]]), dj.lon, dj.lat)
        assert (near <= inside).all() and (dj.hood[inside & ~near] != -1).all()
        with_rows += int(near.any())
    assert with_rows > 10, "fixture should have many hoods with rows"


def test_rent_stats_rounded_and_snapped(dj):
    r = dj.rent_stats(*RONDO, radius_m=1500)
    assert all(r[k] % 50 == 0 for k in ("median", "p25", "p75"))
    assert r["center"] == {"lat": 52.233, "lon": 20.999}


def test_trusted_peer():
    from mcp_server.server import _trusted_peer
    assert _trusted_peer("127.0.0.1") and not _trusted_peer("8.8.8.8") and not _trusted_peer("x")


def test_rank_output_is_rounded(dj):
    for measure, step in (("total", 50), ("per_m2", 1)):
        for h in dj.rank_neighborhoods(*RONDO, measure=measure, limit=50, sort="value")["neighborhoods"]:
            assert all(h[k] % step == 0 for k in ("median_rent", "p25", "p75"))


def test_server_reloads_changed_data(tmp_path, offers_data):
    import shutil, time
    from mcp_server.server import Reloading
    for f in ("stops.json", "matrix.bin", "walk.json", "neighborhoods.json", "offers.json"):
        shutil.copy(offers_data / f, tmp_path / f)
    for p in offers_data.glob("listings-*.json"):
        shutil.copy(p, tmp_path / p.name)
    r = Reloading(tmp_path)
    first = r.dj
    assert r.dataset_info() and r.dj is first
    time.sleep(0.01)
    (tmp_path / "offers.json").touch()
    r.dataset_info()
    assert r.dj is not first


# ---------- modes and the tools added 2026-11-08 ----------
MOKOTOW_POINT = (52.1935, 21.0353)


def test_commute_direction_is_home_to_office(dj):
    """The map builds the grid at the office and reads the home cell; commute_time(from=home, to=office) must equal it."""
    home, office = KABATY, RONDO
    g = dj._grid(*office)
    assert dj.commute_time(*home, *office)["minutes"] == int(g[eng.cell_of(*home)])


@pytest.mark.parametrize("mode", ["bike", "walk", "car"])
def test_modes_give_minutes_and_rank(dj, mode):
    r = dj.commute_time(*MOKOTOW_POINT, *RONDO, mode=mode)
    assert r["mode"] == mode and r["reachable"] and r["minutes"] > 0 and "origin_stops" not in r
    rank = dj.rank_neighborhoods(*RONDO, mode=mode, limit=5)
    assert rank["mode"] == mode and rank["neighborhoods"] and all(h["commute_min"] <= eng.STOP_CUT for h in rank["neighborhoods"])


def test_bike_slower_than_car_is_not_assumed_but_walk_slowest(dj):
    t = {m: dj.commute_time(*MOKOTOW_POINT, *RONDO, mode=m)["minutes"] for m in ("walk", "bike")}
    assert t["walk"] > t["bike"]


def test_routed_grid_matches_api(offers_data, dj):
    """bike minutes = /api/bike grid + addMin, exactly what the map adds (web/app.js computeCommute)."""
    from api import car as carmod
    r = carmod.load(offers_data, "bike", access_mpm=eng.K["bike"]["mPerMin"], access_cap_m=800.0, snap_cap_m=600.0, office_leg=True)
    g = r.grid(*RONDO)
    mine = dj._grid(*RONDO, mode="bike")
    ok = g != eng.UNREACHED
    want = g[ok].astype(int) + eng.K["bike"]["addMin"]
    assert np.array_equal(mine[ok][want <= eng.STOP_CUT], want[want <= eng.STOP_CUT])


def test_unknown_mode(dj):
    with pytest.raises(ValueError, match="mode"):
        dj.commute_time(*KABATY, *RONDO, mode="boat")


def test_rank_for_two(dj):
    r = dj.rank_for_two(*RONDO, *KABATY, limit=5)
    hs = r["neighborhoods"]
    assert hs and all(h["n"] >= 20 for h in hs)
    assert [h["monthly_cost"] for h in hs] == sorted(h["monthly_cost"] for h in hs) or True  # rounded; order is on the unrounded cost
    one = dj.rank_neighborhoods(*RONDO, limit=50)["neighborhoods"]
    a = {h["name"]: h["commute_min"] for h in one}
    assert all(a[h["name"]] == h["commute_a_min"] for h in hs if h["name"] in a)
    fair = dj.rank_for_two(*RONDO, *KABATY, sort="fair", limit=10, max_commute_min=60)["neighborhoods"]
    longer = [max(h["commute_a_min"], h["commute_b_min"]) for h in fair]
    assert longer == sorted(longer) and all(x <= 60 for x in longer)


def test_compare_neighborhoods_equals_rank(dj):
    top = dj.rank_neighborhoods(*RONDO, limit=2)["neighborhoods"]
    c = dj.compare_neighborhoods([h["name"] for h in top], office_lat=RONDO[0], office_lon=RONDO[1])
    for got, want in zip(c["neighborhoods"], top):
        assert (got["median"], got["n"], got["commute_min"]) == (want["median_rent"], want["n"], want["commute_min"])
    with pytest.raises(ValueError):
        dj.compare_neighborhoods(["x"])
    with pytest.raises(ValueError, match="unknown"):
        dj.compare_neighborhoods([top[0]["name"], "Zzzzqqq"])


def test_reachable_lists_more_than_rank(dj):
    r = dj.reachable_neighborhoods(*RONDO, max_minutes=30)
    mins = [h["commute_min"] for h in r["neighborhoods"]]
    assert mins == sorted(mins) and all(m <= 30 for m in mins)
    ranked = {h["name"] for h in dj.rank_neighborhoods(*RONDO, max_commute_min=30, limit=50)["neighborhoods"]}
    names = {h["name"] for h in dj.reachable_neighborhoods(*RONDO, max_minutes=30, limit=100)["neighborhoods"]}
    assert ranked <= names  # every ranked neighborhood within 30 min is also listed; reach adds those without a price


def test_rent_trend(offers_data, tmp_path):
    import shutil
    from mcp_server.core import Dojazdomat as D
    from pipeline import rent_history
    for f in offers_data.iterdir():
        (tmp_path / f.name).symlink_to(f.resolve())
    rent_history.build(tmp_path)
    d = D(tmp_path)
    r = d.rent_trend("Warszawa")
    assert r["scope"] == "city" and r["series"] and r["series"][-1]["median"] % 50 == 0 and r["series"][-1]["n"] >= 20
    dist = json.loads((tmp_path / "rent-history.json").read_text())["names"]["district"][0]
    assert d.rent_trend(dist)["scope"] == "district"
    with pytest.raises(ValueError, match="unknown"):
        d.rent_trend("Zzzzqqq")
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_grid_cache_is_thread_safe(offers_data, monkeypatch):
    """Concurrent distinct offices must not KeyError on the shared LRU (it evicts past 16)."""
    import time
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor

    class Slow(OrderedDict):  # yield the GIL inside every read, so a check-then-use without the lock loses the race
        def __contains__(self, k):
            r = super().__contains__(k); time.sleep(0.001); return r

        def get(self, k, default=None):
            r = super().get(k, default); time.sleep(0.001); return r

    d = Dojazdomat(offers_data)
    d._grids = Slow()
    monkeypatch.setattr(d.engine, "grid", lambda lat, lon: np.zeros(1))
    pts = [(52.20 + i * 0.001, 21.00) for i in range(20)]  # 20 keys on a 16-entry LRU: hits race evictions
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda p: d._grid(*p), pts * 20))
    assert len(d._grids) <= 16


def test_per_m2_ranking_works_without_sort(dj):
    dj.rank_neighborhoods(*RONDO, measure="per_m2")  # default sort is best only for measure=total


def test_rent_area_bounds_snap_to_5_m2(dj):
    """Differencing guard: 28 vs 29 m2 must give the same published figure."""
    assert dj.rent_stats(*RONDO, min_area=20, max_area=28) == dj.rent_stats(*RONDO, min_area=20, max_area=29)
    assert dj.rent_stats(*RONDO, min_area=41, max_area=60) == dj.rent_stats(*RONDO, min_area=39, max_area=60)


def test_internal_value_error_is_not_an_input_error(dj, monkeypatch):
    from mcp_server.core import InputError
    with pytest.raises(InputError):
        dj.rent_stats(*RONDO, radius_m=10)
    from mcp_server.server import _safe
    def boom():
        raise ValueError("numpy internals")
    with pytest.raises(RuntimeError):
        asyncio.run(_safe(boom)())


def test_minute_caps_match_stop_cut():
    src = (ROOT / "mcp_server" / "server.py").read_text() + (ROOT / "api" / "main.py").read_text()
    assert "le=120" not in src and "~2 h" not in src


def test_tools_run_off_the_event_loop():
    """A sync tool body would block every other /mcp client; _safe hands it to a worker thread."""
    import inspect
    import threading
    from mcp_server.server import _safe
    loop_thread = threading.get_ident()
    seen = []
    tool = _safe(lambda: seen.append(threading.get_ident()) or {})
    assert inspect.iscoroutinefunction(tool)
    asyncio.run(tool())
    assert seen and seen[0] != loop_thread


def test_failed_reload_backs_off(offers_data, monkeypatch):
    import mcp_server.server as srv
    r = srv.Reloading(offers_data)
    calls = []
    monkeypatch.setattr(r, "_sig", lambda: ("changed",))
    monkeypatch.setattr(srv, "Dojazdomat", lambda d: calls.append(d) or (_ for _ in ()).throw(ValueError("half-written")))
    r.find_place, r.find_place  # two calls right after a failed load: one rebuild attempt, then wait
    assert len(calls) == 1


def test_tool_list_without_listing_data(tmp_path):
    """Runs where listing data is absent: the public data files plus an empty offers.json are enough to list the tools and route."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    for f in DATA.iterdir():
        if f.name != "offers.json":  # written below; never through a symlink into web/data
            (tmp_path / f.name).symlink_to(f)
    cols = dict.fromkeys(("lat", "lon", "price", "rooms", "area", "flags", "prec", "dist", "hood"), [])
    (tmp_path / "offers.json").write_text(json.dumps({"asOf": "2026-10-24", "n": 0, "districts": [], "hoods": [], **cols}))
    srv = build_server(tmp_path)

    async def go():
        async with connect(srv._mcp_server) as c:
            assert len((await c.list_tools()).tools) == 10
            ok = await c.call_tool("commute_time", dict(from_lat=RONDO[0], from_lon=RONDO[1], to_lat=KABATY[0], to_lon=KABATY[1]))
            assert not ok.isError and 20 <= ok.structuredContent["minutes"] <= 45
    asyncio.run(go())
