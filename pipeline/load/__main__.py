"""python -m pipeline.load [seed|listings [portal ...]|dedup|transit|refresh|all]"""
import os
import sys
import time
from pathlib import Path

import psycopg

from . import adapters, db
from .transit import load_transit

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("DATABASE_URL", f"host=localhost port=5432 user={os.environ.get('USER', 'postgres')} dbname=commute_map")


def listings(conn, portals):
    out = []
    for p in portals or adapters.ADAPTERS:
        src = adapters.ADAPTERS[p]()
        stats = db.load_run(conn, p, src.records, src.complete, f"{src.note}; dropped {dict(src.dropped)}")
        conn.commit()
        out.append((p, stats, src.complete))
        print(p, "complete" if src.complete else "partial", stats, dict(src.dropped), flush=True)
    return out


def main(argv):
    cmd, args = (argv[0] if argv else "all"), argv[1:]
    sys.path.insert(0, str(ROOT / "db"))
    import migrate
    print("migrations:", migrate.migrate(DSN) or "none")
    with psycopg.connect(DSN) as conn:
        if cmd in ("seed", "all"):
            db.seed(conn, ROOT / "data/raw/walk/districts.geojson"); conn.commit()
        if cmd in ("listings", "all"):
            listings(conn, args)
        if cmd in ("transit", "all"):
            print("stop groups:", load_transit(conn)); conn.commit()
        if cmd in ("dedup", "all"):
            t = time.time(); print("dup links:", db.dedup(conn), f"{time.time() - t:.2f}s"); conn.commit()
        if cmd in ("refresh", "all"):
            db.refresh(conn); conn.commit(); print("refreshed mart")


if __name__ == "__main__":
    main(sys.argv[1:])
