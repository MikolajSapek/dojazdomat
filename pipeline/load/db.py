"""staging -> core upsert, SCD2 prices, sweep, cross-portal dedup, mart refresh.
Nothing here commits: the caller owns the transaction."""
import json
import unicodedata
from pathlib import Path

PORTALS = ["otodom", "olx", "gratka", "morizon", "nieruchomosci-online", "adresowo", "domiporta",
           "sprzedajemy-pl-rental-section-private-owners", "szybko-pl-og-oszenia-nieruchomos-ci",
           "allegrolokalnie-pl-nieruchomosci-wynajem"]
COLS = "ext, strength, lon, lat, prec, price, rent, czynsz, rooms, area, flags"


# CONTRACT.md: no personal data. Keys that carry agent/owner identity are dropped at any depth.
PII_KEYS = {"organisationAssignedMember", "advertOwner", "agency", "contact", "phone", "phones", "email", "user", "owner",
            "firstName", "lastName", "name", "photoURL", "images", "description", "shortDescription"}


def scrub(v):
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items() if k not in PII_KEYS}
    if isinstance(v, list):
        return [scrub(x) for x in v]
    return v


def norm(name):
    s = unicodedata.normalize("NFKD", name.lower().replace("ł", "l"))
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c))


def seed(conn, districts_geojson):
    for p in PORTALS:
        conn.execute("INSERT INTO core.portal(name) VALUES (%s) ON CONFLICT DO NOTHING", (p,))
    for i, (name, geom) in enumerate(sorted(json.loads(Path(districts_geojson).read_text()).items()), 1):
        conn.execute(
            """INSERT INTO core.district(id, name, geom)
               VALUES (%s, %s, ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 2180))
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, geom = EXCLUDED.geom""",
            (i, name, json.dumps(geom)))


def _centroids(conn):
    return {norm(n): (lat, lon) for n, lat, lon in conn.execute(
        """SELECT name, ST_Y(c), ST_X(c) FROM
           (SELECT name, ST_Transform(ST_PointOnSurface(geom), 4326) AS c FROM core.district
             WHERE geom IS NOT NULL) d""")}


def _resolve(records, cent):
    """district-only records get the district centroid; unknown district -> dropped."""
    out, lost = [], 0
    for r in records:
        if r["lat"] is None:
            c = cent.get(norm(r.get("district") or ""))
            if not c:
                lost += 1
                continue
            r = {**r, "lat": c[0], "lon": c[1], "prec": 2}
        out.append(r)
    return out, lost


def load_run(conn, portal, records, complete=False, note=""):
    """One scrape run for one portal. complete=True also sweeps listings not seen."""
    conn.execute("INSERT INTO core.portal(name) VALUES (%s) ON CONFLICT DO NOTHING", (portal,))
    pid = conn.execute("SELECT id FROM core.portal WHERE name=%s", (portal,)).fetchone()[0]
    records, lost = _resolve(list({r["ext"]: r for r in records}.values()), _centroids(conn))
    run_id, t0 = conn.execute(
        """INSERT INTO staging.scrape_run(portal_id, started_at, status, n_seen, n_errors, notes)
           VALUES (%s, clock_timestamp(), 'partial', %s, %s, %s) RETURNING id, started_at""",
        (pid, len(records), lost, note)).fetchone()

    with conn.cursor() as cur:
        with cur.copy("COPY staging.raw_listing(run_id, external_id, raw) FROM STDIN") as cp:
            for r in records:
                cp.write_row((run_id, r["ext"], json.dumps(scrub(r["raw"]), ensure_ascii=False)))
        cur.execute("DROP TABLE IF EXISTS tmp_in")
        cur.execute("""CREATE TEMP TABLE tmp_in(ext text PRIMARY KEY, strength text, lon float8, lat float8,
                       prec smallint, price int, rent int, czynsz int, rooms smallint, area smallint,
                       flags int, geom geometry(Point, 2180)) ON COMMIT DROP""")
        with cur.copy(f"COPY tmp_in({COLS}) FROM STDIN") as cp:
            for r in records:
                cp.write_row([r[k] for k in ("ext", "strength", "lon", "lat", "prec", "price", "rent",
                                              "czynsz", "rooms", "area", "flags")])
        cur.execute("UPDATE tmp_in SET geom = ST_Transform(ST_SetSRID(ST_MakePoint(lon, lat), 4326), 2180)")

        # 1. attribute changes on existing listings (last_seen handled below)
        cur.execute("""UPDATE core.listing l SET geom=t.geom, coord_precision=t.prec, rooms=t.rooms,
                         area_m2=t.area, flags=t.flags
                       FROM tmp_in t WHERE l.portal_id=%s AND l.external_id=t.ext
                         AND (l.geom::bytea, l.coord_precision, l.rooms, l.area_m2, l.flags)
                             IS DISTINCT FROM (t.geom::bytea, t.prec, t.rooms, t.area, t.flags)""", (pid,))
        n_attr = cur.rowcount
        # 2. seen again: refresh last_seen, reactivate
        cur.execute("""UPDATE core.listing l SET last_seen=%s, active=true
                       FROM tmp_in t WHERE l.portal_id=%s AND l.external_id=t.ext""", (t0, pid))
        # 3. new listings
        cur.execute("""INSERT INTO core.listing(portal_id, external_id, id_strength, geom, coord_precision,
                         rooms, area_m2, flags, first_seen, last_seen)
                       SELECT %s, ext, strength, geom, prec, rooms, area, flags, %s, %s FROM tmp_in
                       ON CONFLICT (portal_id, external_id) DO NOTHING""", (pid, t0, t0))
        n_new = cur.rowcount
        # 4. SCD2: close changed current price, then open a row for everyone without a current one
        cur.execute("""UPDATE core.listing_price p SET valid_to=%s
                       FROM tmp_in t JOIN core.listing l ON l.portal_id=%s AND l.external_id=t.ext
                       WHERE p.listing_id=l.id AND p.valid_to IS NULL AND p.price <> t.price""", (t0, pid))
        n_price = cur.rowcount
        cur.execute("""INSERT INTO core.listing_price(listing_id, price, rent, czynsz, valid_from)
                       SELECT l.id, t.price, t.rent, t.czynsz, %s
                       FROM tmp_in t JOIN core.listing l ON l.portal_id=%s AND l.external_id=t.ext
                       WHERE NOT EXISTS (SELECT 1 FROM core.listing_price p
                                          WHERE p.listing_id=l.id AND p.valid_to IS NULL)""", (t0, pid))

    n_upd = n_attr + n_price
    conn.execute("""UPDATE staging.scrape_run SET status=%s, finished_at=clock_timestamp(),
                    n_new=%s, n_updated=%s WHERE id=%s""",
                 ("complete" if complete else "partial", n_new, n_upd, run_id))
    swept = conn.execute("SELECT core.sweep_inactive(%s)", (run_id,)).fetchone()[0] if complete else 0
    return {"run_id": run_id, "seen": len(records), "new": n_new, "updated": n_upd,
            "swept": swept, "dropped_unknown_district": lost}


PAIRS_SQL = "SELECT a_id, b_id FROM core.v_dup_links"  # rule lives in db/migrations/008-010


def dedup(conn):
    """Rebuild core.listing_dup from matching pairs; canonical = lowest id of the component.
    The table is only rewritten when the result differs. Returns number of dup links."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in conn.execute(PAIRS_SQL).fetchall():
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    links = {(x, find(x)) for x in parent if find(x) != x}
    if links != set(conn.execute("SELECT listing_id, canonical_id FROM core.listing_dup").fetchall()):
        conn.execute("DELETE FROM core.listing_dup")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO core.listing_dup(listing_id, canonical_id) VALUES (%s, %s)", sorted(links))
    return len(links)


def refresh(conn):
    for mv in ("mart.mv_active_listing", "mart.mv_rent_by_neighborhood"):
        conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}")
