-- Duplicate rule for listings, kept in the database so every consumer (importer, API, ad-hoc SQL) uses the same formula.
-- base = rent without czynsz administracyjny; portals that do not state czynsz have base = total.
-- Two listings are the same flat when:
--   rooms equal and known, area within max(2 m2, 3 %),
--   price matches within 2 % on total, on base, or total vs base (one portal adds czynsz, the other does not),
--   distance <= 150 m for exact pins, <= 300 m if either is street-level,
--   district centroids (precision 2) only with identical rooms/area and a price match within 0.5 %, <= 1500 m.
CREATE FUNCTION core.price_close(a int, b int, tol numeric) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT abs(a - b) <= tol * greatest(a, b) $$;

CREATE FUNCTION core.dup_match(
    rooms_a int, area_a int, total_a int, base_a int, prec_a int,
    rooms_b int, area_b int, total_b int, base_b int, prec_b int,
    dist_m double precision) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$
SELECT rooms_a = rooms_b AND rooms_a <> -1
   AND area_a IS NOT NULL AND area_b IS NOT NULL
   AND CASE WHEN greatest(prec_a, prec_b) = 2 THEN
            area_a = area_b AND dist_m <= 1500
            AND (core.price_close(total_a, total_b, 0.005) OR core.price_close(base_a, base_b, 0.005)
                 OR core.price_close(total_a, base_b, 0.005) OR core.price_close(base_a, total_b, 0.005))
       ELSE
            abs(area_a - area_b) <= greatest(2, 0.03 * greatest(area_a, area_b))
            AND dist_m <= CASE WHEN greatest(prec_a, prec_b) = 1 THEN 300 ELSE 150 END
            AND (core.price_close(total_a, total_b, 0.02) OR core.price_close(base_a, base_b, 0.02)
                 OR core.price_close(total_a, base_b, 0.02) OR core.price_close(base_a, total_b, 0.02))
       END
$$;

-- Candidate pairs among active listings, same portal included (reposts under a new id). a_id < b_id.
CREATE VIEW core.v_dup_pairs AS
SELECT a.id AS a_id, b.id AS b_id, a.external_id AS a_ext, b.external_id AS b_ext,
       ST_Distance(a.geom, b.geom) AS dist_m
FROM core.listing a
JOIN core.listing b ON a.id < b.id AND a.rooms = b.rooms AND ST_DWithin(a.geom, b.geom, 1500)
JOIN core.listing_price pa ON pa.listing_id = a.id AND pa.valid_to IS NULL
JOIN core.listing_price pb ON pb.listing_id = b.id AND pb.valid_to IS NULL
WHERE a.active AND b.active
  AND core.dup_match(a.rooms, a.area_m2, pa.price, coalesce(pa.rent, pa.price), a.coord_precision,
                     b.rooms, b.area_m2, pb.price, coalesce(pb.rent, pb.price), b.coord_precision,
                     ST_Distance(a.geom, b.geom));
