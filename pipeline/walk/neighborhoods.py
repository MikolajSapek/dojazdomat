"""Build web/data/neighborhoods.json from Overpass admin_level 9 (dzielnice) and 10 (osiedla MSI). Raw: data/raw/walk/rel.json"""
import json, sys
from shapely.geometry import LineString
from shapely.ops import unary_union, polygonize
R = str(__import__('pathlib').Path(__file__).resolve().parents[2]) + '/'
els = json.load(open(R+'data/raw/walk/rel.json'))['elements']
def poly(e):
    lines = [LineString([(p['lon'],p['lat']) for p in m['geometry']]) for m in e['members']
             if m['type']=='way' and m.get('role') in ('outer','') and m.get('geometry') and len(m['geometry'])>1]
    ps = list(polygonize(unary_union(lines)))
    return unary_union(ps) if ps else None
dist, osi = {}, []
for e in els:
    p = poly(e)
    if p is None: print('no poly', e['tags'].get('name')); continue
    (dist if e['tags']['admin_level']=='9' else {}).update({e['tags']['name']: p}) if e['tags']['admin_level']=='9' else osi.append((e['tags']['name'], p))
print(len(dist), 'districts', len(osi), 'osiedla')
out, seen = [], set()
for name, p in osi:
    c = p.representative_point()
    d = next((n for n, dp in dist.items() if dp.contains(c)), None)
    if d is None: print('no district', name); continue
    if (name, d) in seen: continue
    seen.add((name, d))
    out.append({'name': name, 'district': d, 'lat': round(c.y,5), 'lon': round(c.x,5)})
out.sort(key=lambda x: (x['district'], x['name']))
json.dump(out, open(R+'web/data/neighborhoods.json','w'), ensure_ascii=False, indent=0)
json.dump({n: p.__geo_interface__ for n,p in dist.items()}, open(R+'data/raw/walk/districts.geojson','w'))
print(len(out), 'written', len({o['district'] for o in out}), 'districts')
