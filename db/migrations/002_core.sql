CREATE TABLE core.portal (
    id   smallserial PRIMARY KEY,
    name text NOT NULL UNIQUE
);

CREATE TABLE core.district (
    id   smallint PRIMARY KEY,
    name text NOT NULL UNIQUE
);

CREATE TABLE core.neighborhood (
    id          int PRIMARY KEY,
    district_id smallint NOT NULL REFERENCES core.district(id),
    name        text NOT NULL,
    geom        geometry(Geometry, 2180) NOT NULL,
    label_geom  geometry(Point, 2180),
    UNIQUE (district_id, name)
);
CREATE INDEX neighborhood_geom_gix ON core.neighborhood USING gist (geom);

CREATE TABLE core.listing (
    id           bigserial PRIMARY KEY,
    portal_id    smallint NOT NULL REFERENCES core.portal(id),
    external_id  text NOT NULL,
    -- native: portal's own numeric id; hashed: portal's opaque/hashed id;
    -- synthetic: md5(lat,lon,price,rooms,area) for portals without an id
    id_strength  text NOT NULL CHECK (id_strength IN ('native', 'hashed', 'synthetic')),
    geom         geometry(Point, 2180) NOT NULL,
    coord_precision smallint NOT NULL CHECK (coord_precision IN (0, 1, 2)),  -- 0 exact, 1 street, 2 district centroid
    rooms        smallint NOT NULL CHECK (rooms IN (-1, 1, 2, 3, 4)),        -- -1 unknown, 4 = 4+
    area_m2      smallint CHECK (area_m2 IS NULL OR area_m2 > 0),            -- NULL = unknown
    flags        int NOT NULL DEFAULT 0 CHECK (flags BETWEEN 0 AND 511),     -- CONTRACT.md flagBits
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    active       boolean NOT NULL DEFAULT true,
    UNIQUE (portal_id, external_id)
);
CREATE INDEX listing_geom_gix ON core.listing USING gist (geom);
CREATE INDEX listing_first_seen_brin ON core.listing USING brin (first_seen);
CREATE INDEX listing_last_seen_brin ON core.listing USING brin (last_seen);
CREATE INDEX listing_active_ix ON core.listing (portal_id, last_seen) WHERE active;
CREATE INDEX listing_active_geom_gix ON core.listing USING gist (geom) WHERE active;

-- SCD type 2: one row per price period; valid_to NULL = current.
CREATE TABLE core.listing_price (
    id         bigserial PRIMARY KEY,
    listing_id bigint NOT NULL REFERENCES core.listing(id) ON DELETE CASCADE,
    price      int NOT NULL CHECK (price BETWEEN 800 AND 40000),  -- total monthly PLN (rent + czynsz)
    rent       int CHECK (rent IS NULL OR rent > 0),
    czynsz     int CHECK (czynsz IS NULL OR czynsz >= 0),
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to   timestamptz,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    EXCLUDE USING gist (listing_id WITH =, tstzrange(valid_from, valid_to) WITH &&)
);
CREATE UNIQUE INDEX listing_price_one_current ON core.listing_price (listing_id) WHERE valid_to IS NULL;

-- cross-portal duplicates: listing_id is a duplicate of canonical_id
CREATE TABLE core.listing_dup (
    listing_id   bigint PRIMARY KEY REFERENCES core.listing(id) ON DELETE CASCADE,
    canonical_id bigint NOT NULL REFERENCES core.listing(id) ON DELETE CASCADE,
    CHECK (listing_id <> canonical_id)
);
CREATE INDEX listing_dup_canonical_ix ON core.listing_dup (canonical_id);
