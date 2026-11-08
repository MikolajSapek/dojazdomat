"""web/data/offers.json (and archived copies) -> web/data/rent-history.json: one median snapshot per day for the MCP rent_trend tool.

A snapshot is mcp_server.core.snapshot: the map's median rows, pin cap and >= medianMinN rule, for the city, each district and each
neighborhood (all rooms and 1/2/3/4+). Days are keyed by offers.json asOf, so rerunning a day replaces it.
Usage:
  python -I pipeline/rent_history.py                      add today's web/data/offers.json (refresh.sh runs this after the merge)
  python -I pipeline/rent_history.py --archive DIR        also add every listings-YYYY-MM-DD.tar.gz in DIR that holds an offers.json
"""
import argparse
import json
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server.core import HISTORY_FILE, snapshot  # noqa: E402
from pipeline import city  # noqa: E402


def load(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"days": []}


def archived(archive_dir):
    """Yield temp dirs holding one archived day's offers.json (days archived before offers.json was added are skipped)."""
    for tar in sorted(Path(archive_dir).glob("listings-*.tar.gz")):
        with tarfile.open(tar) as t:
            member = next((m for m in t.getmembers() if m.name.endswith("offers.json") and m.isfile()), None)
            if member is None:
                continue
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "offers.json").write_bytes(t.extractfile(member).read())
                yield d


def build(data, archive=None):
    data = Path(data)
    hoods = json.loads((data / "neighborhoods.json").read_text())
    hist = load(data / HISTORY_FILE)
    days = {d["date"]: d for d in hist["days"]}
    for d in (archived(archive) if archive else ()):
        try:
            snap = snapshot(d, hoods)
        except ValueError as e:  # an offers.json from before the district/neighborhood columns: no comparable medians
            print(f"rent-history: skip archived day ({e})", file=sys.stderr)
            continue
        days[snap["date"]] = snap
    snap = snapshot(data, hoods)
    days[snap["date"]] = snap
    ordered = [days[k] for k in sorted(days)]
    names = {"city": [city.load()["name"]["nom"]], "district": sorted({n for d in ordered for n in d["district"]}),
             "hood": sorted({n for d in ordered for n in d["hood"]})}
    out = {"_": "pipeline/rent_history.py; read by mcp_server rent_trend", "names": names, "days": ordered}
    tmp = data / (HISTORY_FILE + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(data / HISTORY_FILE)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(city.web()))
    ap.add_argument("--archive")
    a = ap.parse_args()
    h = build(a.data, a.archive)
    print(f"rent-history: {len(h['days'])} days, {h['days'][0]['date']} .. {h['days'][-1]['date']}")
