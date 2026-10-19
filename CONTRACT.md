# Data contract: Dojazd × Czynsz, Warszawa

All agents write only to the paths they own. Frontend reads `web/data/*`.
Spec of the original behaviour: `SPEC.md` (clean room: build from it, never from the NYC source).

## Files (`web/data/`)

| File | Owner | Shape |
|---|---|---|
| `stops.json` | transit | `[{id, name, lat, lon, routes:[str], modes:["metro"\|"tram"\|"bus"\|"rail"]}]`, index = matrix index |
| `matrix.bin` | transit | uint32 LE `n`, then `n*n` uint8 minutes stop→stop (row = from). `255` = unreachable or > 120 min |
| `lines.json` | transit | `[{route, mode, color:"#rrggbb", paths:[[[lat,lon],...]]}]` metro, rail, tram only (no buses) |
| `walk.json` | walk | `{mPerMin, circuity, stopWalkCap, originWalkCap, bikeMPerMin, note}` calibrated on OSM |
| `neighborhoods.json` | walk | `[{name, district, lat, lon}]` label points (osiedla / MSI areas) |
| `listings-<portal>.json` | scraper | see below |
| `listings.json` | merge | all portals merged + deduped, same shape, `source:"merged (...)"` |

## Listings file

```json
{"asOf":"YYYY-MM-DD","source":"otodom","count":N,
 "flagBits":{"furnished":1,"balcony":2,"dishwasher":4,"washer":8,"ac":16,"elevator":32,"parking":64,"pets":128,"private":256},
 "listings":[[lat, lon, pricePLN, rooms, areaM2, flags, coordPrecision]]}
```

- `pricePLN` = monthly total: rent + czynsz administracyjny when the ad states it separately; integer.
- `rooms`: 1 (kawalerka) .. 3 exact, 4 = 4+, -1 unknown. `areaM2`: integer or -1.
- `coordPrecision`: 0 exact pin, 1 street/approximate, 2 district centroid only (frontend drops 2 from spatial stats).
- Only flats/rooms for rent in Warszawa city limits. Drop price < 800 or > 40000.
- Never store personal data (names, phones, emails, descriptions, photos). Raw dumps go to `data/raw/` (gitignored), only the rows above go to `web/data/`.

## Commute model

- Grid over Warszawa bbox lat 52.09–52.37, lon 20.85–21.28, ~110 m cells.
- Door-to-door = walk to stop + matrix + walk from stop, minimum over stops; plus direct walk.
- Matrix: mean earliest-arrival time over departures every 10 min, 07:00–09:00, weekday, from GTFS (ZTM + SKM + metro + KM + WKD), stops clustered to stop groups (zespoły), n ≤ 2500.
- Commute bands (min): ≤15, 15–25, 25–35, 35–45, 45–60, 60+.
