import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from pipeline.load import adapters, db as ldb  # noqa: E402
from test_db_constraints import conn, db  # noqa: E402,F401  (fixtures)

FIX = Path(__file__).parent / "fixtures"


def rec(ext, lat=52.2, lon=21.0, price=3000, rooms=2, area=40, prec=0, rent=None, czynsz=None, **kw):
    return dict(ext=ext, strength="native", lat=lat, lon=lon, prec=prec, price=price,
                rent=rent, czynsz=czynsz, rooms=rooms, area=area, flags=0, raw={"id": ext}, **kw)


def run(conn, portal, recs, complete=False):
    return ldb.load_run(conn, portal, recs, complete=complete)


def one(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


def test_scd2_price_change(conn):
    run(conn, "olx", [rec("a", price=3000)])
    run(conn, "olx", [rec("a", price=3500)])
    rows = conn.execute("SELECT price, valid_to IS NULL FROM core.listing_price ORDER BY valid_from").fetchall()
    assert rows == [(3000, False), (3500, True)]
    assert one(conn, "SELECT count(*) FROM core.listing") == 1
    # closed period ends exactly when the next one opens
    assert one(conn, "SELECT count(*) FROM core.listing_price p JOIN core.listing_price q "
                     "ON p.listing_id=q.listing_id AND p.valid_to=q.valid_from") == 1


def snapshot(conn):
    return (one(conn, "SELECT count(*) FROM core.listing"),
            conn.execute("SELECT price, valid_from, valid_to FROM core.listing_price ORDER BY id").fetchall(),
            one(conn, "SELECT count(*) FROM core.listing_dup"),
            one(conn, "SELECT count(*) FROM core.listing WHERE active"),
            conn.execute("SELECT id, first_seen FROM core.listing ORDER BY id").fetchall())


def test_idempotent_rerun(conn):
    recs = [rec("a"), rec("b", lat=52.18)]
    run(conn, "olx", recs, complete=True)
    first = snapshot(conn)
    stats = run(conn, "olx", recs, complete=True)
    assert snapshot(conn) == first
    assert stats["new"] == 0 and stats["updated"] == 0 and stats["swept"] == 0


def test_dedup_pair(conn):
    run(conn, "olx", [rec("o1", lat=52.2000, price=3000), rec("o2", lat=52.1, price=2000)])
    # same flat on another portal: 60 m away, price +1%, area +1 m2
    run(conn, "gratka", [rec("g1", lat=52.20054, price=3030, area=41),
                         rec("g2", lat=52.1, price=2030, prec=2)])  # precision 2 needs price within 0.5 %
    n = ldb.dedup(conn)
    assert n == 1
    d = conn.execute("SELECT l.external_id, c.external_id FROM core.listing_dup d "
                     "JOIN core.listing l ON l.id=d.listing_id JOIN core.listing c ON c.id=d.canonical_id").fetchall()
    assert d == [("g1", "o1")]
    assert ldb.dedup(conn) == 1  # stable on rerun


def test_dedup_rejects_far_or_different(conn):
    run(conn, "olx", [rec("o1"), rec("o2", lat=52.1, rooms=3)])
    run(conn, "gratka", [rec("g1", lat=52.2030),            # 330 m
                         rec("g2", lat=52.1, rooms=2),       # rooms differ
                         rec("g3", price=3100)])             # 3.3 % price
    assert ldb.dedup(conn) == 0


def test_partial_run_never_deactivates(conn):
    run(conn, "olx", [rec("a"), rec("b", lat=52.18)], complete=True)
    stats = run(conn, "olx", [rec("a")], complete=False)
    assert stats["swept"] == 0
    assert one(conn, "SELECT count(*) FROM core.listing WHERE active") == 2
    # control: a complete run that lacks b does sweep it
    stats = run(conn, "olx", [rec("a")], complete=True)
    assert stats["swept"] == 1
    assert one(conn, "SELECT active FROM core.listing WHERE external_id='b'") is False
    # and b comes back when it reappears
    run(conn, "olx", [rec("a"), rec("b", lat=52.18)])
    assert one(conn, "SELECT active FROM core.listing WHERE external_id='b'") is True


def test_district_centroid_precision_2(conn):
    ldb.seed(conn, FIX / "districts.geojson")
    r = dict(rec("m1"), lat=None, lon=None, district="Mokotów", prec=2)
    run(conn, "morizon", [r])
    assert one(conn, "SELECT coord_precision FROM core.listing") == 2
    assert float(one(conn, "SELECT ST_Y(ST_Transform(geom,4326)) FROM core.listing")) == pytest.approx(52.175, abs=0.01)


def test_olx_adapter_contract_rules():
    src = adapters.olx(FIX)
    ids = {r["ext"]: r for r in src.records}
    assert src.complete is False           # manual export
    # bbb outside bbox, ccc price<800, eee is an unterminated (still growing) line
    assert set(ids) == {"aaa", "ddd"}
    assert ids["aaa"]["price"] == 3500 and ids["aaa"]["flags"] == 1 | 32
    assert ids["ddd"]["rooms"] == 4


def test_dedup_czynsz_included_on_one_portal(conn):
    # olx states rent 3200 + czynsz 800; morizon shows only the rent
    run(conn, "olx", [rec("o1", price=4000, rent=3200, czynsz=800)])
    run(conn, "morizon", [rec("m1", lat=52.2003, price=3200)])
    assert ldb.dedup(conn) == 1


def test_dedup_same_portal_repost(conn):
    run(conn, "olx", [rec("o1"), rec("o9", lat=52.2002)])
    assert ldb.dedup(conn) == 1


def test_dedup_centroid_needs_exact_match(conn):
    # precision 2 = district centroid, ~900 m from the real pin
    run(conn, "olx", [rec("o1", lat=52.208, prec=2), rec("o2", lat=52.108, prec=2, price=3030)])
    run(conn, "otodom", [rec("t1"), rec("t2", lat=52.1)])
    pairs = conn.execute("SELECT a_ext, b_ext FROM core.v_dup_pairs ORDER BY 1").fetchall()
    assert pairs == [("o1", "t1")]


def test_dup_match_formula(conn):
    f = lambda *a: one(conn, "SELECT core.dup_match(%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s)", *a)
    #        rooms area total base prec | rooms area total base prec | dist
    assert f(2, 40, 3000, 3000, 0,  2, 41, 3050, 3050, 0, 100)
    assert not f(2, 40, 3000, 3000, 0,  3, 40, 3000, 3000, 0, 10)      # rooms differ
    assert not f(2, 40, 3000, 3000, 0,  2, 40, 3000, 3000, 0, 200)     # too far for exact pins
    assert f(2, 40, 3000, 3000, 1,  2, 40, 3000, 3000, 0, 250)         # street precision: 300 m
    assert not f(-1, 40, 3000, 3000, 0,  -1, 40, 3000, 3000, 0, 10)    # unknown rooms
    assert not f(2, None, 3000, 3000, 0,  2, None, 3000, 3000, 0, 10)  # unknown area


def test_dedup_ambiguous_match_is_not_linked(conn):
    # two otodom flats (2.6 % apart, so not each other) both fit one olx ad: cannot tell which one it is
    run(conn, "olx", [rec("o1")])
    run(conn, "otodom", [rec("t1", lat=52.2003, price=2960), rec("t2", lat=52.1997, price=3040)])
    assert ldb.dedup(conn) == 0


def test_staging_drops_personal_data(conn):
    r = rec("p1")
    r["raw"] = {"id": "p1", "list": {"organisationAssignedMember": {"lastName": "X"}, "price": 1}, "agency": "A"}
    run(conn, "olx", [r])
    raw = one(conn, "SELECT raw FROM staging.raw_listing")
    assert raw == {"id": "p1", "list": {"price": 1}}


def test_sprzedajemy_adapter():
    src = adapters.sprzedajemy(FIX)
    ids = {r["ext"]: r for r in src.records}
    assert not src.complete
    assert set(ids) == {"1", "2"}  # 3 outside bbox, 4 price<800, 5 unterminated
    assert ids["1"]["price"] == 4100 and ids["1"]["czynsz"] == 300 and ids["1"]["rooms"] == 3
    assert ids["1"]["flags"] == 2 | 32 | 256 and ids["1"]["area"] == 57 and ids["1"]["prec"] == 1
    assert "url" not in ids["1"]["raw"]


def test_szybko_adapter():
    src = adapters.szybko(FIX)
    ids = {r["ext"]: r for r in src.records}
    assert not src.complete and set(ids) == {"100", "101"}  # 102 price<800
    assert ids["100"]["rooms"] == 1 and ids["100"]["prec"] == 1
    assert ids["101"]["rooms"] == -1  # room ad: card rooms refers to whole flat


def test_allegrolokalnie_adapter():
    src = adapters.allegrolokalnie(FIX)
    assert not src.complete and [r["ext"] for r in src.records] == ["u1"]  # "Warszawa" has no district
    assert src.records[0]["flags"] == 256 and src.records[0]["district"] == "Ursus"
