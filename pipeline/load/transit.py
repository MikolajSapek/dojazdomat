"""web/data/stops.json + matrix.bin -> core.stop_group, core.travel_matrix."""
import json
import struct
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web" / "data"


def load_transit(conn, stops_path=WEB / "stops.json", matrix_path=WEB / "matrix.bin"):
    stops = json.loads(Path(stops_path).read_text())
    blob = Path(matrix_path).read_bytes()
    n = struct.unpack_from("<I", blob)[0]
    if n != len(stops) or len(blob) != 4 + n * n:
        raise ValueError(f"matrix n={n} size={len(blob)} does not match {len(stops)} stops")
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO core.stop_group(id, name, geom, routes, modes)
               VALUES (%s, %s, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 2180), %s, %s)
               ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, geom=EXCLUDED.geom,
                 routes=EXCLUDED.routes, modes=EXCLUDED.modes""",
            [(i, s["name"], s["lon"], s["lat"], s["routes"], s["modes"]) for i, s in enumerate(stops)])
        cur.executemany(
            """INSERT INTO core.travel_matrix(origin, row) VALUES (%s, %s)
               ON CONFLICT (origin) DO UPDATE SET row=EXCLUDED.row""",
            [(i, blob[4 + i * n: 4 + (i + 1) * n]) for i in range(n)])
    conn.execute("DELETE FROM core.stop_group WHERE id >= %s", (n,))
    return n
