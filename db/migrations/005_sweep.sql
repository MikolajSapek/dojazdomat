-- Marks listings of the run's portal not seen since the run started as inactive.
-- Only allowed when the run is 'complete' and is the portal's latest run.
CREATE FUNCTION core.sweep_inactive(p_run_id bigint) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
    r staging.scrape_run;
    latest bigint;
    n int;
BEGIN
    SELECT * INTO r FROM staging.scrape_run WHERE id = p_run_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'scrape_run % not found', p_run_id;
    END IF;
    IF r.status <> 'complete' THEN
        RAISE EXCEPTION 'scrape_run % has status %, sweep needs complete', p_run_id, r.status;
    END IF;
    SELECT id INTO latest FROM staging.scrape_run
     WHERE portal_id = r.portal_id ORDER BY started_at DESC, id DESC LIMIT 1;
    IF latest <> p_run_id THEN
        RAISE EXCEPTION 'scrape_run % is not the latest run (%) for portal %', p_run_id, latest, r.portal_id;
    END IF;
    UPDATE core.listing SET active = false
     WHERE portal_id = r.portal_id AND active AND last_seen < r.started_at;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;
