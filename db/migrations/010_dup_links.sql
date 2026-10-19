-- Unambiguous duplicate links: keep a candidate pair only if, on the other listing's portal,
-- each side has exactly one candidate. Common configurations (2 rooms, 40 m2, 3000 PLN) then
-- never chain thousands of different flats into one group.
CREATE VIEW core.v_dup_links AS
WITH c AS MATERIALIZED (
    SELECT v.a_id, v.b_id, la.portal_id AS pa, lb.portal_id AS pb
    FROM core.v_dup_pairs v
    JOIN core.listing la ON la.id = v.a_id
    JOIN core.listing lb ON lb.id = v.b_id
), sides AS (
    SELECT a_id AS id, pb AS other_portal FROM c
    UNION ALL
    SELECT b_id, pa FROM c
), n AS (
    SELECT id, other_portal, count(*) AS k FROM sides GROUP BY 1, 2
)
SELECT c.a_id, c.b_id
FROM c
JOIN n na ON na.id = c.a_id AND na.other_portal = c.pb AND na.k = 1
JOIN n nb ON nb.id = c.b_id AND nb.other_portal = c.pa AND nb.k = 1;
