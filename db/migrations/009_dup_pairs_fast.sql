-- Same rule as 008, split so each branch can use an index:
--   pins/streets (precision 0-1): GiST nested loop within 300 m;
--   any district centroid (precision 2): hash join on equal rooms+area, then distance.
CREATE OR REPLACE VIEW core.v_dup_pairs AS
WITH cur AS MATERIALIZED (
    SELECT l.id, l.external_id, l.geom, l.rooms, l.area_m2, l.coord_precision, p.price, coalesce(p.rent, p.price) AS base
    FROM core.listing l
    JOIN core.listing_price p ON p.listing_id = l.id AND p.valid_to IS NULL
    WHERE l.active AND l.rooms <> -1 AND l.area_m2 IS NOT NULL
)
SELECT a.id AS a_id, b.id AS b_id, a.external_id AS a_ext, b.external_id AS b_ext,
       ST_Distance(a.geom, b.geom) AS dist_m
FROM core.listing a
JOIN core.listing_price pa ON pa.listing_id = a.id AND pa.valid_to IS NULL
JOIN core.listing b ON b.id > a.id AND b.rooms = a.rooms AND ST_DWithin(b.geom, a.geom, 300)
                   AND b.active AND b.coord_precision < 2
JOIN core.listing_price pb ON pb.listing_id = b.id AND pb.valid_to IS NULL
WHERE a.active AND a.coord_precision < 2
  AND core.dup_match(a.rooms, a.area_m2, pa.price, coalesce(pa.rent, pa.price), a.coord_precision,
                     b.rooms, b.area_m2, pb.price, coalesce(pb.rent, pb.price), b.coord_precision,
                     ST_Distance(a.geom, b.geom))
UNION ALL
SELECT a.id, b.id, a.external_id, b.external_id, ST_Distance(a.geom, b.geom)
FROM cur a
JOIN cur b ON b.id > a.id AND b.rooms = a.rooms AND b.area_m2 = a.area_m2
WHERE greatest(a.coord_precision, b.coord_precision) = 2
  AND ST_DWithin(a.geom, b.geom, 1500)
  AND core.dup_match(a.rooms, a.area_m2, a.price, a.base, a.coord_precision,
                     b.rooms, b.area_m2, b.price, b.base, b.coord_precision,
                     ST_Distance(a.geom, b.geom));
