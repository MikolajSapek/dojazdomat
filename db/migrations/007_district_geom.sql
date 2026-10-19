-- district polygon: source of centroids for listings that carry only a district name (coord_precision 2)
ALTER TABLE core.district ADD COLUMN geom geometry(Geometry, 2180);
