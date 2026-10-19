"""CONTRACT.md rules shared by every adapter."""
import hashlib
from collections import Counter
from dataclasses import dataclass, field

FLAGS = {"furnished": 1, "balcony": 2, "dishwasher": 4, "washer": 8, "ac": 16,
         "elevator": 32, "parking": 64, "pets": 128, "private": 256}
BBOX = (52.09, 52.37, 20.85, 21.28)  # lat0, lat1, lon0, lon1


@dataclass
class Source:
    records: list
    complete: bool
    note: str = ""
    dropped: Counter = field(default_factory=Counter)


def hid(portal, raw):
    return hashlib.sha1(f"{portal}:{raw}".encode()).hexdigest()[:12]


def to_rooms(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return -1
    return -1 if n < 1 else min(n, 4)


def to_area(a):
    try:
        a = round(float(a))
    except (TypeError, ValueError):
        return None
    return a if 8 <= a <= 400 else None


def make(ext, strength, price, lat=None, lon=None, prec=0, rooms=-1, area=None, flags=0,
         rent=None, czynsz=None, district=None, raw=None):
    """Normalized record, or (None, reason) when the contract rejects it.
    price is the monthly total (rent + czynsz)."""
    try:
        price = int(round(float(price)))
    except (TypeError, ValueError):
        return None, "noprice"
    if not 800 <= price <= 40000:
        return None, "price range"
    if lat is None:
        if not district:
            return None, "nocoord"
        prec = 2
    elif not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]):
        return None, "bbox"
    return dict(ext=str(ext), strength=strength, lat=lat, lon=lon, prec=prec, price=price,
                rent=rent, czynsz=czynsz, rooms=to_rooms(rooms), area=to_area(area),
                flags=flags, district=district, raw=raw or {}), None


def collect(pairs, complete, note=""):
    """pairs: iterable of make() results."""
    src = Source([], complete, note)
    for r, why in pairs:
        if r:
            src.records.append(r)
        else:
            src.dropped[why] += 1
    return src
