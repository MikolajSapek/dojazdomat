"""Shared fixtures. offers.json is derived (not committed), so offline tests build it the way refresh.sh does."""
import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "data"
NO_FEE = ("gratka", "nieruchomosci-online", "szybko-pl-og-oszenia-nieruchomos-ci")


def districts_file(d):
    """Real polygons (data/raw/walk/districts.geojson, or $DISTRICTS_GEOJSON) when present; else, as in CI where raw data is not
    versioned, one polygon per district of neighborhoods.json: the hull of its label points, 1.5 km wider. Either way every
    district of the hoods exists and the parity tests compare the map and the MCP on the same dist column."""
    real = Path(os.environ.get("DISTRICTS_GEOJSON") or ROOT / "data" / "raw" / "walk" / "districts.geojson")
    if real.is_file():
        return real
    from shapely.geometry import MultiPoint, mapping
    pts = {}
    for h in json.loads((DATA / "neighborhoods.json").read_text()):
        if h["district"] != "Okolice":
            pts.setdefault(h["district"], []).append((h["lon"], h["lat"]))
    out = Path(d) / "districts.geojson"
    out.write_text(json.dumps({n: mapping(MultiPoint(p).convex_hull.buffer(0.02)) for n, p in pts.items()}))
    return out


@pytest.fixture(scope="session")
def offers_data(tmp_path_factory):
    """Copy of web/data (symlinks) with listings re-dated to today, so the 7-day stale rule keeps them, plus quality.json and
    offers.json rebuilt the way refresh.sh does. Until the crawlers write the feeKnownBit (branch fix/admin-fee), the fixture
    sets it on every portal except NO_FEE (the portals that mostly leave the admin fee out): a mix of rows in and out of medians."""
    if not any(DATA.glob("listings-*.json")) or not (ROOT / "pipeline" / "merge_listings.py").is_file():
        pytest.skip("listing data and the merge pipeline are not part of this repository")
    d = tmp_path_factory.mktemp("data")
    today = datetime.date.today().isoformat()
    fee = json.loads((ROOT / "web" / "model-constants.json").read_text())["feeKnownBit"]
    for f in DATA.iterdir():
        if f.name.startswith("listings-"):
            j = json.loads(f.read_text())
            j["asOf"] = today
            if not any(f.name == f"listings-{p}.json" for p in NO_FEE):
                j["listings"] = [[*r[:5], int(r[5]) | fee, *r[6:]] for r in j["listings"]]
            (d / f.name).write_text(json.dumps(j))
        elif f.is_file() and f.name not in ("offers.json", "quality.json"):
            (d / f.name).symlink_to(f)
    sys.path.insert(0, str(ROOT))
    from pipeline import quality
    (d / "quality.json").write_text(json.dumps(quality.compute(quality.load_portals(str(d)))))
    spec = importlib.util.spec_from_file_location("merge_listings", ROOT / "pipeline" / "merge_listings.py")
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    doc = ml.merge(str(d), str(districts_file(d)))
    assert doc["n"] > 1000
    (d / "offers.json").write_text(json.dumps(doc, separators=(",", ":")))
    return d


def pytest_configure(config):
    config.addinivalue_line("markers", "db: needs a live Postgres (deselect with -m 'not db')")


DB_FIXTURES = {"db_url", "db", "conn"}  # the Postgres fixtures of test_api.py and test_db_constraints.py


def pytest_collection_modifyitems(items):
    """Mark every test that (transitively) uses a Postgres fixture, so CI selects `-m "not db"` instead of a file list."""
    for item in items:
        if DB_FIXTURES & set(item.fixturenames):
            item.add_marker(pytest.mark.db)
