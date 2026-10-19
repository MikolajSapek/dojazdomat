CREATE MATERIALIZED VIEW mart.mv_active_listing AS
SELECT l.id, l.portal_id, l.geom, l.rooms, l.area_m2, l.flags, l.coord_precision,
       l.first_seen, l.last_seen, p.price, p.rent, p.czynsz, nb.id AS neighborhood_id
FROM core.listing l
JOIN core.listing_price p ON p.listing_id = l.id AND p.valid_to IS NULL
LEFT JOIN core.listing_dup d ON d.listing_id = l.id
LEFT JOIN LATERAL (
    SELECT n.id FROM core.neighborhood n WHERE ST_Within(l.geom, n.geom) LIMIT 1
) nb ON true
WHERE l.active AND d.listing_id IS NULL;   -- non-canonical duplicates dropped
CREATE UNIQUE INDEX mv_active_listing_pk ON mart.mv_active_listing (id);
CREATE INDEX mv_active_listing_geom_gix ON mart.mv_active_listing USING gist (geom);

-- precision-2 (district centroid) rows excluded from spatial stats, per CONTRACT.md
CREATE MATERIALIZED VIEW mart.mv_rent_by_neighborhood AS
SELECT neighborhood_id, rooms,
       count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS median_price,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY price::numeric / area_m2)
           FILTER (WHERE area_m2 IS NOT NULL) AS median_price_m2
FROM mart.mv_active_listing
WHERE neighborhood_id IS NOT NULL AND coord_precision < 2
GROUP BY neighborhood_id, rooms;
CREATE UNIQUE INDEX mv_rent_by_neighborhood_pk ON mart.mv_rent_by_neighborhood (neighborhood_id, rooms);
