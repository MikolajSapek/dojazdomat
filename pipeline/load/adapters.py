"""Per-portal adapters: raw dump dir -> Source(records, complete). Read only; no network.

Files may still be growing: jsonl readers keep only newline-terminated lines.
Listings with no coordinates (otodom/gratka without a detail row, morizon always)
are kept at coord_precision 2 on the district centroid; they are excluded from spatial
stats and dedup, and are upgraded in place when a later run brings real coordinates.
`complete` is True only when the dump itself proves the source crawl finished.
"""
import json
import re
from pathlib import Path

from .rules import FLAGS, collect, hid, make

RAW = Path(__file__).resolve().parents[2] / "data" / "raw"


def jsonl(path):
    """dict per complete, parseable line; last occurrence of an id wins (callers key by id)."""
    data = Path(path).read_bytes()
    end = data.rfind(b"\n") + 1
    for line in data[:end].splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            pass


def num(s):
    m = re.search(r"\d[\d  ]*", s or "")
    return int(re.sub(r"\D", "", m.group())) if m else None


# ---------- otodom: list.json (dict id->item) joined with details.jsonl ----------
WORDS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SEVEN": 7,
         "EIGHT": 8, "NINE": 9, "TEN": 10}


def _vals(r, key):
    t = r["target"].get(key) or []
    v = {x.split("::")[-1] for x in (t if isinstance(t, list) else [t])}
    for i in r["info"]:
        if i["label"] == key.lower():
            v |= {x.split("::")[-1] for x in i["values"]}
    return v


def _oto_flags(d, li):
    tags = {x["value"] for x in li.get("tags", [])}
    f = 0
    ex = eq = set()
    if d:
        ex, eq = _vals(d, "Extras_types"), _vals(d, "Equipment_types")
        if "furniture" in eq: f |= 1
        if "dishwasher" in eq: f |= 4
        if "washing_machine" in eq: f |= 8
        if "lift" in ex or any(x["label"] == "lift" and x["values"] for x in d["info"]): f |= 32
        if ex & {"pets_allowed", "pets"}: f |= 128
    if ex & {"balcony", "terrace", "garden"} or tags & {"BALCONY", "TERRACE", "GARDEN"}: f |= 2
    if "air_conditioning" in ex | eq or "AIR_CONDITIONING" in tags: f |= 16
    if ex & {"garage", "parking"} or "PARKING_SPOT" in tags: f |= 64
    if li.get("isPrivateOwner"): f |= 256
    return f


def _oto_precision(d, shared):
    rad = d.get("radius") or 0
    if rad >= 300 or not d.get("street"): return 2
    if rad > 0: return 1
    if shared[(d["lat"], d["lon"])] >= 15: return 2  # many ads on one pin = centroid
    return 0 if d.get("number") else 1


def otodom(root=RAW):
    d0 = Path(root) / "otodom"
    lst = json.loads((d0 / "list.json").read_text())
    det = {r["id"]: r for r in jsonl(d0 / "details.jsonl") if r.get("ok")}
    tried = {r["id"] for r in jsonl(d0 / "details.jsonl")}
    from collections import Counter
    shared = Counter((r["lat"], r["lon"]) for r in det.values() if r.get("lat"))

    def rows():
        for k, li in lst.items():
            if li.get("estate") != "FLAT" or li.get("transaction") != "RENT":
                yield None, "not flat/rent"; continue
            d = det.get(int(k))
            if d and d.get("status") not in (None, "active"):
                yield None, "inactive"; continue
            if d and d.get("lat") is not None:
                t = d["target"]
                base = t.get("Price") or (li.get("totalPrice") or {}).get("value")
                rent = float(t.get("Rent") or 0)
                kw = dict(lat=d["lat"], lon=d["lon"], prec=_oto_precision(d, shared),
                          rooms=(t.get("Rooms_num") or [None])[0], area=t.get("Area"))
            else:
                base = (li.get("totalPrice") or {}).get("value")
                rent = float((li.get("rentPrice") or {}).get("value") or 0)
                dist = next((x["name"] for x in (li["location"].get("reverseGeocoding") or {}).get("locations", [])
                             if x["locationLevel"] == "district"), None)
                kw = dict(district=dist, rooms=WORDS.get(li.get("roomsNumber"), -1),
                          area=li.get("areaInSquareMeters"))
            try:
                base = float(base)
            except (TypeError, ValueError):
                yield None, "noprice"; continue
            yield make(k, "native", base + rent, rent=int(base), czynsz=int(rent) or None,
                       flags=_oto_flags(d, li), raw={"list": li, "detail": d}, **kw)

    complete = all(int(k) in tried for k in lst)
    return collect(rows(), complete, f"{len(det)} detail rows of {len(lst)} listed")


# ---------- olx: manual browser export, never complete ----------
def olx(root=RAW):
    seen = {}
    for r in jsonl(Path(root) / "olx" / "olx_decoded.jsonl"):
        seen[r["id"]] = r
    fl_map = {1: 1, 2: 32, 4: 64, 8: 128}
    keys, rows = set(), []
    for r in seen.values():
        price, rent = float(r["price"]), float(r.get("rent") or 0)
        ok = 50 <= rent < price
        k = (r["lat"], r["lon"], round(price + (rent if ok else 0)), r["rooms"], round(r["area"] or 0))
        if k in keys:  # same ad re-posted
            rows.append((None, "dup")); continue
        keys.add(k)
        flags = sum(v for b, v in fl_map.items() if r["flags"] & b)
        rows.append(make(r["id"], "hashed", price + (rent if ok else 0), r["lat"], r["lon"], r["prec"],
                         r["rooms"], r["area"], flags, rent=int(price), czynsz=int(rent) if ok else None, raw=r))
    return collect(rows, False, "manual export")


# ---------- gratka / morizon: list.jsonl (+ gratka detail.jsonl) ----------
def _rooms_pl(s):
    if not s: return -1
    m = re.match(r"\d+", s)
    return int(m.group()) if m else (1 if "kawalerka" in s.lower() else -1)


def _district(li):
    loc = li["location"]["location"]
    return loc[2] if len(loc) > 2 else None


def _gratka_flags(d):
    labels = {i["label"]: i.get("value") for v in (d.get("lists") or {}).values() for i in v}
    f = 0
    if "Meble" in labels: f |= 1
    if any(labels.get(k) == "Tak" for k in ("Balkon", "Taras", "Loggia")) or "Pow. balkonu" in labels: f |= 2
    if any(k.startswith("Winda") for k in labels): f |= 32
    if any(k.startswith("Miejsce postojowe") for k in labels): f |= 64
    return f, num(labels.get("Dodatkowe koszty")) if "zł" in str(labels.get("Dodatkowe koszty")) else None


def gratka(root=RAW):
    d0 = Path(root) / "gratka"
    lst = {r["id"]: r for r in jsonl(d0 / "list.jsonl")}
    det = {r["id"]: r for r in jsonl(d0 / "detail.jsonl")}

    def rows():
        for i, li in lst.items():
            d = det.get(i)
            if d and (d.get("gone") or d.get("status") != "ACTIVE"):
                yield None, "gone/inactive"; continue
            base = float(li["price"]["amount"])
            kw = dict(rooms=_rooms_pl(li.get("numberOfRooms")), area=li.get("area"))
            if d and d.get("lat") is not None:
                f, extra = _gratka_flags(d)
                kw.update(lat=d["lat"], lon=d["lon"], prec=1 if d.get("street") else 2, flags=f)
            else:
                f, extra = 0, None
                kw.update(district=_district(li))
            yield make(i, "native", base + (extra or 0), rent=int(base), czynsz=extra,
                       raw={"list": li, "detail": d}, **kw)

    return collect(rows(), all(i in det for i in lst), f"{len(det)} detail rows of {len(lst)} listed")


def morizon(root=RAW):
    d0 = Path(root) / "morizon"
    lst = {r["id"]: r for r in jsonl(d0 / "list.jsonl")}
    log = (d0 / "crawl.log").read_text().strip().splitlines()
    complete = bool(log) and log[-1] == f"unique {len(lst)}"  # crawler prints this only at the end

    def rows():
        for i, li in lst.items():
            yield make(i, "native", float(li["price"]["amount"]), district=_district(li),
                       rooms=_rooms_pl(li.get("numberOfRooms")), area=li.get("area"), raw=li)
    return collect(rows(), complete, "list only, no coordinates")


# ---------- nieruchomosci-online: listings.json (already normalized, no end marker) ----------
def nieruchomosci_online(root=RAW):
    rows = json.loads((Path(root) / "nieruchomosci-online" / "listings.json").read_text())
    seen = {r["id"]: r for r in rows}
    return collect((make(r["id"], "hashed", r["price"], r["lat"], r["lon"], r["prec"], r["rooms"],
                         r["area"], r["flags"], raw=r) for r in seen.values()), False)


# ---------- adresowo ----------
def _adr_flags(b, private):
    f = 0
    if "umeblowan" in b and "umeblowanie brak" not in b: f |= 1
    if "balkon" in b or "taras" in b: f |= 2
    if "zmywark" in b: f |= 4
    if "pralk" in b: f |= 8
    if "klimatyz" in b: f |= 16
    if "winda" in b: f |= 32
    if "garaż" in b or "miejsce postojowe" in b or "parking" in b: f |= 64
    if private: f |= FLAGS["private"]
    return f


def adresowo(root=RAW):
    det = {r["id"]: r for r in jsonl(Path(root) / "adresowo" / "details.jsonl")}

    def rows():
        for r in det.values():
            if r.get("lat") is None or r.get("price") is None or not r.get("warsaw"):
                yield None, "nocoord/price/not warsaw"; continue
            gp = r.get("geoPrecision") or 0
            yield make(r["id"], "hashed", r["price"] + (r.get("fee") or 0), r["lat"], r["lon"],
                       0 if gp >= 4 else 1 if gp >= 2 else 2, r.get("rooms"), r.get("area"),
                       _adr_flags(r.get("blk") or "", r.get("private")), rent=r["price"],
                       czynsz=r.get("fee"), raw={k: v for k, v in r.items() if k != "blk"})
    return collect(rows(), False)


# ---------- domiporta ----------
def _dom_flags(feat, agency):
    f = 0
    if "umeblowane" in feat: f |= 1
    if "balkon" in feat or "taras" in feat: f |= 2
    if "zmywark" in feat: f |= 4
    if "pralk" in feat: f |= 8
    if "klimatyz" in feat: f |= 16
    if "winda" in feat: f |= 32
    if "gara" in feat or "miejsce parkingowe" in feat: f |= 64
    if not agency: f |= 256
    return f


def domiporta(root=RAW):
    det = {r["id"]: r for r in jsonl(Path(root) / "domiporta" / "details.jsonl")}

    def rows():
        for r in det.values():
            if r.get("lat") is None or r.get("price") is None:
                yield None, "nocoord/price"; continue
            base, adm = float(r["price"]), r.get("admin") or 0
            yield make(r["id"], "hashed", base + adm, r["lat"], r["lon"], 1 if r.get("hasStreet") else 2,
                       r.get("rooms"), r.get("area"), _dom_flags(r.get("feat") or "", r.get("agency")),
                       rent=int(base), czynsz=adm or None, raw={k: v for k, v in r.items() if k != "feat"})
    return collect(rows(), False)

# ---------- sprzedajemy.pl: offers.jsonl, one row per listing (lat/lon + attrs) ----------
SPRZ = "sprzedajemy-pl-rental-section-private-owners"
_SPRZ_KW = {1: r"umeblowan", 2: r"balkon|taras|loggia", 4: r"zmywark", 8: r"pralk", 16: r"klimatyz",
            32: r"winda", 64: r"parking|gara[żz]|miejsce postoj", 128: r"zwierz"}


def _fnum(s):
    m = re.search(r"\d+(?:\.\d+)?", (s or "").replace(",", "."))
    return float(m.group()) if m else None


def sprzedajemy(root=RAW):
    from collections import Counter
    det = {r["id"]: r for r in jsonl(Path(root) / SPRZ / "offers.jsonl")}
    pins = Counter((r.get("lat"), r.get("lon")) for r in det.values())

    def rows():
        for r in det.values():
            if r.get("lat") is None:
                yield None, "nocoord"; continue
            price = r.get("price_ld") or _fnum(r.get("price_txt"))
            if not price:
                yield None, "noprice"; continue
            adm = r.get("admin_hint") or 0
            adm = adm if 100 <= adm <= 3000 else 0
            a = r.get("attrs") or {}
            blob = " ".join(r.get("params") or [])
            f = sum(b for b, p in _SPRZ_KW.items() if re.search(p, blob, re.I))
            if "prywatnej" in a.get("Oferta od", ""): f |= FLAGS["private"]
            yield make(r["id"], "native", price + adm, r["lat"], r["lon"], 2 if pins[(r["lat"], r["lon"])] >= 4 else 1,
                       _fnum(a.get("Liczba pokoi")), _fnum(a.get("Powierzchnia")), f,
                       rent=int(price), czynsz=int(adm) or None, raw={k: v for k, v in r.items() if k != "url"})
    return collect(rows(), False)


# ---------- szybko.pl: cards.jsonl carry pins, price, area, rooms ----------
SZYBKO = "szybko-pl-og-oszenia-nieruchomos-ci"


def szybko(root=RAW):
    det = {r["nid"]: r for r in jsonl(Path(root) / SZYBKO / "cards.jsonl")}
    return collect((make(r["nid"], "native", r["price"], r["lat"], r["lon"], 1,
                         -1 if r["type"] == "pokoj" else r["rooms"], r["area"], raw=r)
                    for r in det.values()), False)


# ---------- allegro lokalnie: list cards, district only (centroid, prec 2); blocked by anti-bot, never complete ----------
ALLEGRO = "allegrolokalnie-pl-nieruchomosci-wynajem"


def allegrolokalnie(root=RAW):
    cards = {c["id"]: c for r in jsonl(Path(root) / ALLEGRO / "pages.jsonl") for c in r["cards"]}

    def rows():
        for c in cards.values():
            parts = [x.strip() for x in c["loc"].split(",")]
            if len(parts) < 2:
                yield None, "no district"; continue
            yield make(c["id"], "native", c["price"], district=parts[1], rooms=c.get("rooms"), area=c.get("area"),
                       flags=FLAGS["private"] if c.get("private") else 0, raw=c)
    return collect(rows(), False)


ADAPTERS = {"otodom": otodom, "olx": olx, "gratka": gratka, "morizon": morizon,
            "nieruchomosci-online": nieruchomosci_online, "adresowo": adresowo, "domiporta": domiporta,
            SPRZ: sprzedajemy, SZYBKO: szybko, ALLEGRO: allegrolokalnie}
