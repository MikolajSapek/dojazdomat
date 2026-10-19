"""Calibrate circuity (network/straight) origin->nearest 1-3 GTFS stops; write web/data/walk.json. Seeded, rerunnable."""
import json, csv, random, datetime, sys
import numpy as np, networkx as nx, osmnx as ox
from shapely.geometry import Polygon, Point, shape
from shapely.prepared import prep
from pyproj import Transformer
from sklearn.neighbors import BallTree
R=str(__import__('pathlib').Path(__file__).resolve().parents[2])+'/'; RAW=R+'data/raw/walk/'
N=int(sys.argv[1]) if len(sys.argv)>1 else 2500
rng=random.Random(42)
tr=Transformer.from_crs(4326,32634,always_xy=True)
# stops inside contract bbox
stops=[(float(r['stop_lat']),float(r['stop_lon'])) for r in csv.DictReader(open(RAW+'stops.txt',encoding='utf-8'))
       if r['town_name']=='Warszawa' and 52.09<=float(r['stop_lat'])<=52.37 and 20.85<=float(r['stop_lon'])<=21.28]
sx,sy=tr.transform([s[1] for s in stops],[s[0] for s in stops]); S=np.c_[sx,sy]; tree=BallTree(S)
# residential polygons + districts
res=[Polygon([(p['lon'],p['lat']) for p in w['geometry']]) for w in json.load(open(RAW+'residential.json'))['elements'] if len(w.get('geometry',[]))>3]
res=[p for p in res if p.is_valid and p.area>0]
dists={n:prep(shape(g)) for n,g in json.load(open(RAW+'districts.geojson')).items()}
def sample():
    ws=[p.area for p in res]
    while True:
        p=rng.choices(res,ws)[0]; x0,y0,x1,y1=p.bounds
        for _ in range(20):
            pt=Point(rng.uniform(x0,x1),rng.uniform(y0,y1))
            if p.contains(pt): return pt
origins=[]
while len(origins)<N:
    pt=sample(); d=next((n for n,dp in dists.items() if dp.contains(pt)),None)
    if d: origins.append((pt.y,pt.x,d))
ox_,oy_=tr.transform([o[1] for o in origins],[o[0] for o in origins]); O=np.c_[ox_,oy_]
dist,idx=tree.query(O,k=3)
out={}
for mode in ('walk','bike'):
    G=ox.project_graph(ox.load_graphml(RAW+f'{mode}.graphml'),to_crs='EPSG:32634')
    if mode=='walk': G=G.to_undirected()  # walking is bidirectional; ignore one-way tags
    ns=list(G.nodes); NX=np.array([[G.nodes[n]['x'],G.nodes[n]['y']] for n in ns]); nt=BallTree(NX)
    od,oi=nt.query(O); sd,si=nt.query(S[idx.reshape(-1)]); sd=sd.reshape(-1,3); si=si.reshape(-1,3)
    rows=[]
    for i,(lat,lon,d) in enumerate(origins):
        src=ns[oi[i,0]]
        lens=nx.single_source_dijkstra_path_length(G,src,cutoff=6000,weight='length')
        for j in range(3):
            st=dist[i,j]
            if st<150: continue  # snap noise dominates
            nl=lens.get(ns[si[i,j]])
            if nl is None: continue
            rows.append((d,(nl+od[i,0]+sd[i,j])/st,st))
    out[mode]=rows; print(mode,len(rows),'pairs')
def stats(rows):
    r=np.array([x[1] for x in rows]); return {'n':len(r),'median':round(float(np.median(r)),3),'p75':round(float(np.percentile(r,75)),3),'p90':round(float(np.percentile(r,90)),3)}
res_out={m:{'all':stats(rows),'byDistrict':{d:stats([x for x in rows if x[0]==d]) for d in sorted({x[0] for x in rows}) if sum(1 for x in rows if x[0]==d)>=30}} for m,rows in out.items()}
json.dump(res_out,open(RAW+'calibration.json','w'),ensure_ascii=False,indent=1)
w,b=res_out['walk']['all'],res_out['bike']['all']
WSPD,BSPD=80,250
# ponytail: circuity = median (typical); p75 reported in note for conservative use
walk={'mPerMin':round(WSPD/w['median'],1),'circuity':w['median'],'stopWalkCap':15,'originWalkCap':25,'bikeMPerMin':round(BSPD/b['median'],1),
 'note':(f"OSM street network (Overpass, osmnx, snapshot {datetime.date.today()}). {w['n']} walk pairs from {N} random origins in landuse=residential to their 3 nearest GTFS stops (mkuran.pl ZTM stops.txt, all modes), "
 f"snap distances included. Walk network/straight circuity median {w['median']}, p75 {w['p75']}, p90 {w['p90']}. Network speed 80 m/min (4.8 km/h) => mPerMin = 80/circuity = {round(WSPD/w['median'],1)} straight-line m/min "
 f"(p75-based: {round(WSPD/w['p75'],1)}). Bike {b['n']} pairs, circuity median {b['median']}, p75 {b['p75']}; network 250 m/min => bikeMPerMin {round(BSPD/b['median'],1)}. "
 f"Caps: stopWalkCap 15 min (metro/rail, contract), originWalkCap 25. Per-district circuity: data/raw/walk/calibration.json. Caveat: bus-stop targets are denser than metro/rail so circuity to stations may be somewhat higher.")}
json.dump(walk,open(R+'web/data/walk.json','w'),ensure_ascii=False,indent=1)
print(json.dumps(res_out['walk']['all']),json.dumps(res_out['bike']['all']))
for d,s in res_out['walk']['byDistrict'].items(): print(d,s['median'],s['p75'],s['n'])
