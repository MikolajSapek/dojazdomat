CREATE TABLE core.stop_group (
    id     int PRIMARY KEY,                       -- = matrix index
    name   text NOT NULL,
    geom   geometry(Point, 2180) NOT NULL,
    routes text[] NOT NULL DEFAULT '{}',
    modes  text[] NOT NULL DEFAULT '{}'
);
CREATE INDEX stop_group_geom_gix ON core.stop_group USING gist (geom);
CREATE INDEX stop_group_name_trgm ON core.stop_group USING gin (name gin_trgm_ops);

-- row = n uint8 minutes from origin to every stop group (255 = unreachable)
CREATE TABLE core.travel_matrix (
    origin int PRIMARY KEY REFERENCES core.stop_group(id) ON DELETE CASCADE,
    row    bytea NOT NULL
);
