"""Download OSM walk + bike networks for the contract bbox (osmnx/Overpass) and residential landuse. Cached in data/raw/walk/."""
import osmnx as ox, os, requests, json
R=str(__import__('pathlib').Path(__file__).resolve().parents[2])+'/data/raw/walk/'
ox.settings.requests_headers={'User-Agent':'dojazdomat/0.1 (+https://github.com/MikolajSapek/dojazdomat)'}
ox.settings.use_cache=True; ox.settings.cache_folder=R+'osmnx_cache'
bbox=(20.84,52.08,21.29,52.38) # W,S,E,N slightly padded
for nt in ('walk','bike'):
    f=R+f'{nt}.graphml'
    if os.path.exists(f): continue
    G=ox.graph_from_bbox(bbox,network_type=nt,simplify=True,retain_all=False)
    ox.save_graphml(G,f); print(nt,len(G.nodes),len(G.edges))
f=R+'residential.json'
if not os.path.exists(f):
    q='[out:json][timeout:180];(way["landuse"="residential"](52.08,20.84,52.38,21.29););out geom;'
    r=requests.post('https://overpass-api.de/api/interpreter',data={'data':q},headers=ox.settings.requests_headers,timeout=300); r.raise_for_status()
    open(f,'w').write(r.text)
print('ok')
