"""Synthetic fixtures for web/fixtures/ in the exact CONTRACT.md shapes.

Re-runnable, deterministic (fixed seed). Run:
  .venv/bin/python pipeline/frontend/make_fixtures.py
"""
import json
import math
import random
import struct
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "web" / "fixtures"
FLAG_BITS = {"furnished": 1, "balcony": 2, "dishwasher": 4, "washer": 8, "ac": 16,
             "elevator": 32, "parking": 64, "pets": 128, "private": 256}
M_LAT = 110540.0
M_LON = 111320.0 * math.cos(math.radians(52.23))
CENTER = (52.2329, 20.9986)  # Rondo ONZ

# Two metro-ish lines and one tram line through the centre, as (name, lat, lon).
LINES = {
    "M1": ("metro", "#e2001a", [("Kabaty", 52.1313, 21.0656), ("Natolin", 52.1403, 21.0574),
            ("Wilanowska", 52.1808, 21.0230), ("Pole Mokotowskie", 52.2085, 21.0080),
            ("Politechnika", 52.2190, 21.0150), ("Centrum", 52.2310, 21.0105),
            ("Rondo ONZ", 52.2330, 20.9980), ("Dworzec Gdański", 52.2580, 20.9950),
            ("Marymont", 52.2720, 20.9720), ("Młociny", 52.2905, 20.9300)]),
    "M2": ("metro", "#0072bc", [("Bemowo", 52.2400, 20.9100), ("Księcia Janusza", 52.2400, 20.9600),
            ("Rondo Daszyńskiego", 52.2300, 20.9840), ("Świętokrzyska", 52.2350, 21.0080),
            ("Stadion Narodowy", 52.2470, 21.0440), ("Dworzec Wileński", 52.2540, 21.0350),
            ("Targówek", 52.2800, 21.0500), ("Bródno", 52.2930, 21.0280)]),
    "9": ("tram", "#b60000", [("Gocław", 52.2380, 21.0950), ("Rondo Wiatraczna", 52.2470, 21.0870),
            ("Rondo Waszyngtona", 52.2390, 21.0560), ("Most Poniatowskiego", 52.2330, 21.0350),
            ("Pl. Zbawiciela \"&<test>\"", 52.2195, 21.0145), ("Dw. Zachodni", 52.2200, 20.9650),
            ("Ursus", 52.1950, 20.8850)]),
}
NEIGHBORHOODS = [
    ("Śródmieście Północne", "Śródmieście", 52.2330, 21.0050), ("Powiśle", "Śródmieście", 52.2380, 21.0300),
    ("Muranów", "Śródmieście", 52.2450, 20.9950), ("Mokotów Górny", "Mokotów", 52.1950, 21.0200),
    ("Stary Mokotów", "Mokotów", 52.2050, 21.0100), ("Ursynów Północny", "Ursynów", 52.1550, 21.0450),
    ("Kabaty", "Ursynów", 52.1300, 21.0700), ("Wola \"<&>\"", "Wola", 52.2350, 20.9700),
    ("Bemowo", "Bemowo", 52.2450, 20.9100), ("Żoliborz", "Żoliborz", 52.2650, 20.9850),
    ("Bielany", "Bielany", 52.2850, 20.9450), ("Praga Północ", "Praga-Północ", 52.2550, 21.0350),
    ("Saska Kępa", "Praga-Południe", 52.2300, 21.0600), ("Gocław", "Praga-Południe", 52.2350, 21.0950),
    ("Targówek", "Targówek", 52.2850, 21.0450), ("Ursus", "Ursus", 52.1950, 20.8850),
    ("Wilanów", "Wilanów", 52.1650, 21.0850), ("Białołęka", "Białołęka", 52.3250, 21.0100),
]


def dist_m(a, b):
    return math.hypot((a[0] - b[0]) * M_LAT, (a[1] - b[1]) * M_LON)


def build_stops():
    stops, line_of = [], []
    for route, (mode, _c, pts) in LINES.items():
        for i, (name, lat, lon) in enumerate(pts):
            stops.append({"id": f"{route}-{i}", "name": name, "lat": lat, "lon": lon,
                          "routes": [route], "modes": [mode]})
            line_of.append((route, i))
    return stops, line_of


def build_matrix(stops, line_of):
    n = len(stops)
    inf = 10 ** 9
    t = [[inf] * n for _ in range(n)]
    for i in range(n):
        t[i][i] = 0
    for i in range(n):  # same-line rides: 2 min/stop + 3 min wait; transfers by 6-min walk
        for j in range(n):
            if i == j:
                continue
            if line_of[i][0] == line_of[j][0]:
                t[i][j] = 3 + 2 * abs(line_of[i][1] - line_of[j][1])
            elif dist_m((stops[i]["lat"], stops[i]["lon"]), (stops[j]["lat"], stops[j]["lon"])) < 500:
                t[i][j] = 6
    for k in range(n):  # Floyd-Warshall
        tk = t[k]
        for i in range(n):
            tik = t[i][k]
            if tik >= inf:
                continue
            ti = t[i]
            for j in range(n):
                if tik + tk[j] < ti[j]:
                    ti[j] = tik + tk[j]
    body = bytes(min(255, v) if v <= 120 else 255 for row in t for v in row)
    return struct.pack("<I", n) + body


def build_listings(rng, count, source, extra_flags_mask):
    rows = []
    for _ in range(count):
        lat = CENTER[0] + rng.gauss(0, 0.045)
        lon = CENTER[1] + rng.gauss(0, 0.07)
        if not (52.09 <= lat <= 52.37 and 20.85 <= lon <= 21.28):
            continue
        km = dist_m((lat, lon), CENTER) / 1000
        rooms = rng.choice([1, 1, 2, 2, 2, 3, 3, 4, -1])
        area = {1: 28, 2: 45, 3: 62, 4: 85, -1: 50}[rooms] + rng.randint(-6, 12)
        ppm = max(45, 115 - 6 * km + rng.gauss(0, 12))
        price = int(round(ppm * area / 10) * 10)
        flags = 0
        for bit in (1, 2, 4, 8, 16, 32, 64):
            if rng.random() < 0.35:
                flags |= bit
        flags |= extra_flags_mask if rng.random() < 0.2 else 0  # pets only in one portal; private (256) never set
        prec = rng.choices([0, 1, 2], [6, 3, 1])[0]
        if rng.random() < 0.08:
            area = -1
        if 800 <= price <= 40000:
            rows.append([round(lat, 6), round(lon, 6), price, rooms, area, flags, prec])
    return {"asOf": "2026-10-19", "source": source, "count": len(rows), "flagBits": FLAG_BITS, "listings": rows}


def main():
    rng = random.Random(42)
    OUT.mkdir(parents=True, exist_ok=True)
    stops, line_of = build_stops()
    lines = [{"route": r, "mode": m, "color": c, "paths": [[[lat, lon] for _n, lat, lon in pts]]}
             for r, (m, c, pts) in LINES.items()]
    files = {
        "stops.json": stops,
        "lines.json": lines,
        "walk.json": {"mPerMin": 50.0, "circuity": 1.6, "stopWalkCap": 15, "originWalkCap": 25,
                      "bikeMPerMin": 250, "note": "synthetic fixture"},
        "neighborhoods.json": [{"name": n, "district": d, "lat": la, "lon": lo} for n, d, la, lo in NEIGHBORHOODS],
        "listings-otodom.json": build_listings(rng, 900, "otodom", 128),
        "listings-olx.json": build_listings(rng, 350, "olx", 0),
    }
    for name, obj in files.items():
        (OUT / name).write_text(json.dumps(obj, ensure_ascii=False))
    (OUT / "matrix.bin").write_bytes(build_matrix(stops, line_of))
    for name in sorted(files):
        obj = files[name]
        print(name, obj["count"] if isinstance(obj, dict) and "count" in obj else len(obj))
    print("matrix.bin n =", len(stops))


if __name__ == "__main__":
    main()
