# Dojazdomat

Mapa Warszawy, która pokazuje, gdzie opłaca się wynająć mieszkanie, jeśli pracujesz w konkretnym miejscu: czas dojazdu komunikacją miejską od drzwi do drzwi zestawiony z czynszami z ogłoszeń.

Działa na https://dojazdomat.sapcorp.eu. Serwer MCP dla Claude i innych asystentów AI: `https://dojazdomat.sapcorp.eu/mcp` (opis w `mcp_server/README.md`).

## Co jest w repozytorium

- `web/`: statyczny frontend (Leaflet, bez frameworka). Liczy izochrony w przeglądarce z gotowej macierzy czasów przejazdu.
- `pipeline/transit/`: macierz czasów przystanek → przystanek z rozkładów GTFS (ZTM, SKM, metro, KM, WKD), średnia dla odjazdów 7:00–9:00 w dzień roboczy.
- `pipeline/walk/`: kalibracja dojścia pieszo na grafie ulic OpenStreetMap, punkty osiedli.
- `pipeline/load/`, `db/`: PostgreSQL + PostGIS (warstwy staging / core / mart, historia cen SCD2, reguła duplikatów w SQL).
- `api/`: FastAPI (siatka dojazdu, mediany czynszu, ranking osiedli).
- `mcp_server/`: serwer MCP zwracający tylko statystyki zbiorcze.
- `CONTRACT.md`: format plików danych, `ARCHITECTURE.md`: architektura.

## Czego tu nie ma

Ogłoszeń. Repozytorium nie zawiera pobranych ogłoszeń ani scraperów portali: bazy ogłoszeń należą do portali i ich regulaminy ograniczają dalsze udostępnianie. Do uruchomienia lokalnie służą syntetyczne dane w `web/fixtures/` (`web/index.html?fixtures=1`). Własne pliki `web/data/listings-<źródło>.json` w formacie z `CONTRACT.md` frontend wczyta automatycznie.

## Uruchomienie

```
python3 -m http.server 8000 --directory web      # mapa: http://localhost:8000/?fixtures=1
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q tests                        # testy DB wymagają lokalnego PostgreSQL z PostGIS
```

## Dane i licencje źródeł

- Rozkłady jazdy: GTFS ZTM Warszawa, SKM, Koleje Mazowieckie, WKD (agregowane przez mkuran.pl).
- Ulice i granice: © OpenStreetMap contributors, ODbL.
- Podkład mapy: Esri, HERE, Garmin, © OpenStreetMap.
