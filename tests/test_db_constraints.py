import os, sys
from pathlib import Path

import psycopg
import pytest
from psycopg import errors

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import migrate  # noqa: E402

TEST_DB = "commute_map_test"
USER = os.environ.get("USER", "postgres")


def dsn(db):
    return f"host=localhost port=5432 user={USER} dbname={db}"


@pytest.fixture(scope="session")
def db():
    with psycopg.connect(dsn("postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")
    migrate.migrate(dsn(TEST_DB))
    yield dsn(TEST_DB)
    with psycopg.connect(dsn("postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture
def conn(db):
    """Each test runs in a transaction that is rolled back."""
    with psycopg.connect(db) as c:
        c.execute("SELECT 1")
        yield c
        c.rollback()


def portal(c, name="olx"):
    return c.execute(
        "INSERT INTO core.portal(name) VALUES (%s) RETURNING id", (name,)
    ).fetchone()[0]


def listing(c, pid, ext="a1", **kw):
    v = dict(rooms=2, coord_precision=0, id_strength="native", flags=0)
    v.update(kw)
    return c.execute(
        """INSERT INTO core.listing(portal_id, external_id, id_strength, geom, rooms,
               area_m2, flags, coord_precision)
           VALUES (%s,%s,%s, ST_Transform(ST_SetSRID(ST_MakePoint(21.0,52.2),4326),2180),
                   %s,40,%s,%s) RETURNING id""",
        (pid, ext, v["id_strength"], v["rooms"], v["flags"], v["coord_precision"]),
    ).fetchone()[0]


def price(c, lid, p=3000, valid_to=None, valid_from="2026-01-01"):
    c.execute(
        "INSERT INTO core.listing_price(listing_id, price, valid_from, valid_to)"
        " VALUES (%s,%s,%s,%s)",
        (lid, p, valid_from, valid_to),
    )


def run(c, pid, status):
    return c.execute(
        "INSERT INTO staging.scrape_run(portal_id, status) VALUES (%s,%s) RETURNING id",
        (pid, status),
    ).fetchone()[0]


def test_duplicate_portal_external_id_rejected(conn):
    pid = portal(conn)
    listing(conn, pid, "x")
    with pytest.raises(errors.UniqueViolation):
        listing(conn, pid, "x")


def test_same_external_id_other_portal_ok(conn):
    listing(conn, portal(conn, "olx"), "x")
    listing(conn, portal(conn, "otodom"), "x")


def test_second_current_price_rejected(conn):
    lid = listing(conn, portal(conn))
    price(conn, lid, 3000)
    with pytest.raises((errors.UniqueViolation, errors.ExclusionViolation)):
        price(conn, lid, 3100, valid_from="2026-02-01")


def test_closed_price_plus_current_ok(conn):
    lid = listing(conn, portal(conn))
    price(conn, lid, 3000, valid_to="2026-02-01")
    price(conn, lid, 3100, valid_from="2026-02-01")


def test_overlapping_price_periods_rejected(conn):
    lid = listing(conn, portal(conn))
    price(conn, lid, 3000, valid_to="2026-03-01")
    with pytest.raises(errors.ExclusionViolation):
        price(conn, lid, 3100, valid_from="2026-02-01", valid_to="2026-04-01")


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_sweep_refuses_incomplete_run(conn, status):
    pid = portal(conn)
    rid = run(conn, pid, status)
    with pytest.raises(errors.RaiseException):
        conn.execute("SELECT core.sweep_inactive(%s)", (rid,))


def test_sweep_refuses_if_not_latest_run(conn):
    pid = portal(conn)
    old = run(conn, pid, "complete")
    run(conn, pid, "partial")
    with pytest.raises(errors.RaiseException):
        conn.execute("SELECT core.sweep_inactive(%s)", (old,))


def test_sweep_deactivates_unseen(conn):
    pid = portal(conn)
    l_old = listing(conn, pid, "old")
    l_new = listing(conn, pid, "new")
    conn.execute(
        "UPDATE core.listing SET last_seen = now() - interval '3 days' WHERE id=%s", (l_old,)
    )
    rid = run(conn, pid, "complete")
    conn.execute("UPDATE staging.scrape_run SET started_at = now() - interval '1 day' WHERE id=%s", (rid,))
    n = conn.execute("SELECT core.sweep_inactive(%s)", (rid,)).fetchone()[0]
    assert n == 1
    act = dict(conn.execute("SELECT id, active FROM core.listing").fetchall())
    assert act[l_old] is False and act[l_new] is True


@pytest.mark.parametrize("bad", [799, 40001])
def test_price_check(conn, bad):
    lid = listing(conn, portal(conn))
    with pytest.raises(errors.CheckViolation):
        price(conn, lid, bad)


@pytest.mark.parametrize("bad", [0, 5, -2])
def test_rooms_check(conn, bad):
    with pytest.raises(errors.CheckViolation):
        listing(conn, portal(conn), rooms=bad)


@pytest.mark.parametrize("bad", [-1, 3])
def test_coord_precision_check(conn, bad):
    with pytest.raises(errors.CheckViolation):
        listing(conn, portal(conn), coord_precision=bad)


def test_id_strength_check(conn):
    with pytest.raises(errors.CheckViolation):
        listing(conn, portal(conn), id_strength="bogus")


def test_scrape_status_check(conn):
    with pytest.raises(errors.CheckViolation):
        run(conn, portal(conn), "weird")


def test_geom_srid_enforced(conn):
    pid = portal(conn)
    with pytest.raises(psycopg.Error):
        conn.execute(
            "INSERT INTO core.listing(portal_id, external_id, id_strength, geom, rooms, coord_precision)"
            " VALUES (%s,'g','native', ST_SetSRID(ST_MakePoint(21,52),4326), 2, 0)",
            (pid,),
        )


def test_mart_views_refresh_concurrently(db):
    with psycopg.connect(db, autocommit=True) as c:
        for mv in ("mart.mv_active_listing", "mart.mv_rent_by_neighborhood"):
            c.execute(f"REFRESH MATERIALIZED VIEW {mv}")  # populate first
            c.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}")


def test_mart_dedup_and_median(conn):
    p1, p2 = portal(conn, "olx"), portal(conn, "otodom")
    conn.execute("INSERT INTO core.district(id,name) VALUES (1,'Mokotow')")
    conn.execute(
        """INSERT INTO core.neighborhood(id,district_id,name,geom)
           VALUES (1,1,'Sluzewiec', ST_Transform(ST_GeomFromText(
             'POLYGON((20.9 52.1,21.1 52.1,21.1 52.3,20.9 52.3,20.9 52.1))',4326),2180))"""
    )
    a, b, c3 = listing(conn, p1, "a"), listing(conn, p2, "b"), listing(conn, p1, "c")
    for lid, pr in ((a, 3000), (b, 3000), (c3, 4000)):
        price(conn, lid, pr)
    conn.execute("INSERT INTO core.listing_dup(listing_id, canonical_id) VALUES (%s,%s)", (b, a))
    conn.execute("REFRESH MATERIALIZED VIEW mart.mv_active_listing")
    conn.execute("REFRESH MATERIALIZED VIEW mart.mv_rent_by_neighborhood")
    assert conn.execute("SELECT count(*) FROM mart.mv_active_listing").fetchone()[0] == 2
    row = conn.execute(
        "SELECT n, median_price FROM mart.mv_rent_by_neighborhood WHERE neighborhood_id=1 AND rooms=2"
    ).fetchone()
    assert row == (2, 3500)
