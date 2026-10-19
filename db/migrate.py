"""Apply db/migrations/*.sql in order, once each. Usage: python db/migrate.py [dsn]"""
import os, sys
from pathlib import Path

import psycopg

DIR = Path(__file__).parent / "migrations"
DEFAULT = f"host=localhost port=5432 user={os.environ.get('USER', 'postgres')} dbname=commute_map"


def migrate(dsn=DEFAULT):
    applied = []
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations("
                  "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        # advisory lock: concurrent runners serialize
        c.execute("SELECT pg_advisory_lock(727274)")
        try:
            done = {r[0] for r in c.execute("SELECT name FROM public.schema_migrations")}
            for f in sorted(DIR.glob("*.sql")):
                if f.name in done:
                    continue
                with c.transaction():
                    c.execute(f.read_text())
                    c.execute("INSERT INTO public.schema_migrations(name) VALUES (%s)", (f.name,))
                applied.append(f.name)
        finally:
            c.execute("SELECT pg_advisory_unlock(727274)")
    return applied


if __name__ == "__main__":
    print("applied:", migrate(*sys.argv[1:]) or "nothing")
