CREATE TABLE staging.scrape_run (
    id          bigserial PRIMARY KEY,
    portal_id   smallint NOT NULL REFERENCES core.portal(id),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'partial' CHECK (status IN ('complete', 'partial', 'failed')),
    n_seen      int NOT NULL DEFAULT 0,
    n_new       int NOT NULL DEFAULT 0,
    n_updated   int NOT NULL DEFAULT 0,
    n_errors    int NOT NULL DEFAULT 0,
    notes       text
);
CREATE INDEX scrape_run_portal_ix ON staging.scrape_run (portal_id, started_at DESC, id DESC);

CREATE TABLE staging.raw_listing (
    id          bigserial PRIMARY KEY,
    run_id      bigint NOT NULL REFERENCES staging.scrape_run(id) ON DELETE CASCADE,
    external_id text,
    raw         jsonb NOT NULL
);
CREATE INDEX raw_listing_run_ix ON staging.raw_listing (run_id);
