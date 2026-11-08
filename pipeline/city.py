"""City settings: load cities/<slug>.json (env CITY, default warszawa). Everything specific to one city comes from here.

One process = one city: modules read city.load() at import, so a loop over cities runs one process per city
(CITY=<slug> python ...), never a loop inside Python.
Paths: raw() and web() are the city's raw-dump and built-data dirs. Warsaw keeps data/raw and web/data (cities/warszawa.json
"paths"); every other city lives under data/cities/<slug>/, outside web/data, so nothing of it is deployed or baked into an
image before its own publication (docs/MULTI-CITY phase 3 and 4).
"""
import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = "warszawa"


def slug(s: str | None = None) -> str:
    return s or os.environ.get("CITY") or DEFAULT


@lru_cache(maxsize=None)
def _load(s: str) -> dict:
    return json.loads((ROOT / "cities" / f"{s}.json").read_text(encoding="utf-8"))


def load(s: str | None = None) -> dict:
    """The city's settings; s None = env CITY or warszawa."""
    return _load(slug(s))


def _path(s: str | None, key: str) -> Path:
    c = load(s)
    return ROOT / (c.get("paths", {}).get(key) or f"data/cities/{c['slug']}/{key}")


def raw(s: str | None = None) -> Path:
    """Raw portal dumps, GTFS, OSM extracts (data/raw for Warsaw)."""
    return _path(s, "raw")


def web(s: str | None = None) -> Path:
    """Built data the site and engines read (web/data for Warsaw)."""
    return _path(s, "web")


def street_extent(s: str | None = None) -> Path:
    """Long-street extents of the city's street index (pipeline/geo/street_extent.py writes it; missing = no long-street filter)."""
    c = load(s)
    return ROOT / (c.get("paths", {}).get("streetExtent") or f"data/cities/{c['slug']}/street-extent.json")


def constants(s: str | None = None) -> dict:
    """web/model-constants.json with the city's model block on top (grid, car, walk, bike, cost, map): what the pipeline
    builds a city with. Dict values merge one level deep (cost keeps tripsPerMonth). Warsaw: equal to the file (tests/test_city_config.py)."""
    k = json.loads((ROOT / "web" / "model-constants.json").read_text(encoding="utf-8"))
    for key, v in load(s).get("model", {}).items():
        k[key] = {**k[key], **v} if isinstance(v, dict) and isinstance(k.get(key), dict) else v
    return k


def enabled() -> list[str]:
    """Slugs the nightly crawls, Warsaw first (its log lines and deploy stay as they were)."""
    on = [p.stem for p in sorted((ROOT / "cities").glob("*.json")) if _load(p.stem).get("nightly")]
    return sorted(on, key=lambda s: s != DEFAULT)


if __name__ == "__main__":  # shell: python3 pipeline/city.py enabled | raw <slug> | web <slug>
    import sys
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    print("\n".join(enabled()) if cmd == "enabled" else (raw(arg) if cmd == "raw" else web(arg)))
