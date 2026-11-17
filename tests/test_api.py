import os, sys, time
from pathlib import Path

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT))
import migrate  # noqa: E402
from api.main import create_app  # noqa: E402

TEST_DB = "commute_map_api_test_" + os.environ.get("PYTEST_XDIST_WORKER", "gw0")  # one DB per xdist worker: the fixture drops it WITH (FORCE)
USER = os.environ.get("USER", "postgres")
dsn = lambda db: f"host=localhost port=5432 user={USER} dbname={db}"
RONDO = (52.2329, 20.9986)


def pt(lon, lat):
    return f"ST_Transform(ST_SetSRID(ST_MakePoint({lon},{lat}),4326),2180)"


@pytest.fixture(scope="session")
def db_url():
    with psycopg.connect(dsn("postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")
    migrate.migrate(dsn(TEST_DB))
    with psycopg.connect(dsn(TEST_DB), autocommit=True) as c:
        c.execute("INSERT INTO core.portal(name) VALUES ('olx'),('otodom')")
        c.execute("INSERT INTO core.district(id,name) VALUES (1,'Wola')")
        # two neighbourhoods: A around Rondo ONZ, B far east
        for i, (nm, x0, y0) in enumerate([("Alfa", 20.99, 52.228), ("Beta", 21.20, 52.15)], 1):
            c.execute(f"""INSERT INTO core.neighborhood(id,district_id,name,geom,label_geom) VALUES
              ({i},1,'{nm}', ST_Transform(ST_MakeEnvelope({x0},{y0},{x0+0.02},{y0+0.02},4326),2180),
               {pt(x0+0.01, y0+0.01)})""")
        # 6 listings in Alfa (cheap-ish, 2 rooms), 5 in Beta (cheaper)
        k = 0
        for nid, (x0, y0), prices in [(1, (20.99, 52.228), [4000, 4200, 4400, 4600, 4800, 5000]),
                                      (2, (21.20, 52.15), [2000, 2100, 2200, 2300, 2400])]:
            for p in prices:
                k += 1
                c.execute(f"""INSERT INTO core.listing(portal_id,external_id,id_strength,geom,coord_precision,rooms,area_m2,flags)
                  VALUES (%s,%s,'native',{pt(x0+0.005+k*1e-4, y0+0.005)},0,2,40,%s) RETURNING id""",
                          (1 + k % 2, f"e{k}", 1 if k % 2 else 0))
                lid = c.execute("SELECT max(id) FROM core.listing").fetchone()[0]
                c.execute("INSERT INTO core.listing_price(listing_id,price) VALUES (%s,%s)", (lid, p))
        c.execute(f"""INSERT INTO core.stop_group(id,name,geom) VALUES
              (0,'Testowa Alfa',{pt(21.0,52.23)}),(1,'Zupelnie Inna',{pt(21.1,52.2)})""")
        c.execute("REFRESH MATERIALIZED VIEW mart.mv_active_listing")
        c.execute("REFRESH MATERIALIZED VIEW mart.mv_rent_by_neighborhood")
    yield dsn(TEST_DB)
    with psycopg.connect(dsn("postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture(scope="session")
def client(db_url):
    with TestClient(create_app(database_url=db_url, rate_limit=10_000), raise_server_exceptions=False) as c:
        yield c


def grid_of(r):
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    rows, cols = (int(x) for x in np.frombuffer(r.content[:4], "<u2"))
    g = np.frombuffer(r.content, np.uint8, offset=4)
    assert g.size == rows * cols
    return g.reshape(rows, cols)


def cell(g, lat, lon):
    from api.engine import LAT0, LON0, DLAT, DLON
    return int(g[int((lat - LAT0) // DLAT), int((lon - LON0) // DLON)])


def test_health(client):
    d = client.get("/api/health").json()
    assert d["ok"] and d["error"] is None and d["data"]["db"] is True and d["data"]["stops"] > 1000


def test_commute_origin_and_far_cell(client):
    g = grid_of(client.get("/api/commute", params=dict(lat=RONDO[0], lon=RONDO[1])))
    assert 0 <= cell(g, *RONDO) <= 15
    # SE corner of bbox is far from any stop: unreached or >= 60
    assert cell(g, 52.095, 21.275) >= 60
    assert (g < 255).sum() > 5000  # network reaches many cells


def test_commute_matches_reference(client):
    """Brute-force port of app.js computeCommute for a handful of cells."""
    from api import engine as e
    g = grid_of(client.get("/api/commute", params=dict(lat=RONDO[0], lon=RONDO[1])))
    en = e.Engine(*e.read_files(ROOT / "web" / "data"), e.read_walk(ROOT / "web" / "data"))
    w = en.walk
    wt = e.net_min(np.hypot((en.slat - RONDO[0]) * e.M_LAT, (en.slon - RONDO[1]) * e.M_LON))
    best = np.full(en.n, np.inf)
    for a in np.flatnonzero(wt <= w["stopWalkCap"]):
        row = en.matrix[:, a].astype(float)  # column: home -> office; row[row == 255] = np.inf
        best = np.minimum(best, wt[a] + row)
    rng = np.random.default_rng(1)
    for _ in range(40):
        r, c = rng.integers(0, e.ROWS), rng.integers(0, e.COLS)
        la, lo = e.LAT0 + (r + .5) * e.DLAT, e.LON0 + (c + .5) * e.DLON
        d = e.net_min(np.hypot((en.slat - la) * e.M_LAT, (en.slon - lo) * e.M_LON))
        t = np.where((d <= w["stopWalkCap"]) & (best < e.STOP_CUT), best + d, np.inf).min()
        direct = float(e.net_min(np.hypot((la - RONDO[0]) * e.M_LAT, (lo - RONDO[1]) * e.M_LON)))
        t = min(t, direct if direct <= w["originWalkCap"] else np.inf)
        exp = 255 if not np.isfinite(t) or t > e.STOP_CUT else int(np.floor(t + 0.5))
        assert abs(int(g[r, c]) - exp) <= 1 if exp != 255 else g[r, c] == 255


@pytest.mark.parametrize("params", [dict(lat=10, lon=21), dict(lat=52.2, lon=30), dict(lat="x", lon=21), dict(lat=52.2)])
def test_commute_validation(client, params):
    r = client.get("/api/commute", params=params)
    j = r.json()
    assert r.status_code == 422 and j["ok"] is False and j["data"] is None and "Traceback" not in r.text


def test_stops_db_trigram(client):
    j = client.get("/api/stops", params=dict(q="alfa")).json()
    assert [s["name"] for s in j["data"]] == ["Testowa Alfa"]
    assert len(client.get("/api/stops", params=dict(q="te")).json()["data"]) <= 10
    assert client.get("/api/stops", params=dict(q="a")).status_code == 422


def test_cors(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:8765"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:8765"
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_not_found_envelope(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["ok"] is False


def test_rate_limit(db_url):
    with TestClient(create_app(database_url=db_url, rate_limit=3)) as c:
        codes = [c.get("/api/health").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_db_down_still_serves_commute():
    with TestClient(create_app(database_url="postgresql://localhost:1/none"), raise_server_exceptions=False) as c:
        assert c.get("/api/health").json()["data"]["db"] is False
        assert grid_of(c.get("/api/commute", params=dict(lat=RONDO[0], lon=RONDO[1]))) is not None


def test_commute_warm_latency(client):
    params = dict(lat=52.2297, lon=21.0122)
    client.get("/api/commute", params=params)  # cold
    ts = []
    for i in range(10):
        p = dict(lat=52.2297 + i * 0.0007, lon=21.0122 + i * 0.0009)  # distinct origins: no cache hits
        t = time.perf_counter(); client.get("/api/commute", params=p); ts.append((time.perf_counter() - t) * 1000)
    print("commute ms", sorted(ts))
    assert float(np.median(ts)) < 150


def test_client_ip_trusts_headers_only_from_private_peer():
    from types import SimpleNamespace as NS
    from api.main import client_ip
    mk = lambda peer, **h: NS(client=NS(host=peer), headers=h)
    assert client_ip(mk("10.0.0.5", **{"x-forwarded-for": "1.2.3.4, 10.0.0.5"})) == "1.2.3.4"
    assert client_ip(mk("10.0.0.5", **{"x-forwarded-for": "6.6.6.6, 1.2.3.4, 10.0.0.5"})) == "1.2.3.4"  # forged left entry ignored
    assert client_ip(mk("172.18.0.2", **{"cf-connecting-ip": "5.6.7.8"})) == "5.6.7.8"
    assert client_ip(mk("8.8.8.8", **{"cf-connecting-ip": "5.6.7.8"})) == "8.8.8.8"
