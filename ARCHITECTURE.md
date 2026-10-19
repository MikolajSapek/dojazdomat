# Architecture (approved 2026-10-19)

Stack: PostgreSQL 17 + PostGIS (local Postgres.app now, `docker-compose.yml` for sapserver later), FastAPI (Python), static Leaflet frontend behind Caddy.

Design follows the DBMS course (vault `Datascience-2026/Database Management Systems`: L04 Logical Design, L10 OLAP, L11 Data Warehousing; concepts Normalization, Slowly Changing Dimension, Database Index, View Materialization).

## Database: three layers

1. `staging`: raw scraper JSON (`raw_listing` jsonb) + `scrape_run` (portal, started/finished, counts, errors).
2. `core` (3NF):
   - `portal`, `district`, `neighborhood` (polygons, label point)
   - `listing`: surrogate key, unique `(portal_id, external_id)`, `geom` point EPSG:2180, rooms, area, flags, coord_precision, first_seen, last_seen, active
   - `listing_price`: SCD type 2 (`price`, `czynsz`, `valid_from`, `valid_to`)
   - `listing_dup`: cross-portal duplicate links (canonical listing id)
   - transit: `stop_group`, `route`, `travel_matrix` (one `bytea` row per origin stop group), `grid_cell`, `cell_stop_walk` (OSM walk minutes)
3. `mart` (materialized views, refreshed after each scrape): active deduped listings, medians per neighborhood × rooms × flags, price/m2 per grid cell.

Indexes: GiST on geometry, BRIN on dates, partial index on active listings, `pg_trgm` on stop names.

## Backend

FastAPI loads the matrix and walk tables into RAM (numpy) at startup.
Endpoints: `/api/commute?lat&lon` (grid of minutes), `/api/rent` (filtered medians), `/api/neighborhoods` (ranking + Pareto), `/api/stops?q=`.

## Pipeline

scrapers -> `staging` -> upsert `core` (new price = new SCD2 row; listings not seen in a run -> inactive) -> `REFRESH MATERIALIZED VIEW mart.*`.

## Repo

Numbered SQL migrations, `docker-compose.yml` (postgis + api + caddy), pytest for API and DB constraints.
Order: first swarm finishes the static version; second swarm builds DB + API and switches the frontend to the API.
