"""Bounded database-authorized removal of retained publication data."""

CLEANUP_SQL = """
CREATE OR REPLACE FUNCTION iwiki.cleanup_publication_staging(
    tenant text, domain bigint, retention_seconds integer, cleanup_limit integer
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    item record;
    removed integer := 0;
    expired_before timestamptz;
    cutoff timestamptz := iwiki.publication_now();
    batch_limit integer;
BEGIN
    IF iwiki.publication_may_clean(tenant, domain) IS DISTINCT FROM true THEN
        RAISE insufficient_privilege;
    END IF;
    SELECT cutoff - make_interval(secs => greatest(retention_seconds, p.staging_retention_seconds)),
           least(cleanup_limit, p.staging_cleanup_limit)
      INTO STRICT expired_before, batch_limit
      FROM iwiki.publication_policy p WHERE p.singleton;
    IF cleanup_limit IS NULL OR cleanup_limit < 1 OR retention_seconds IS NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    FOR item IN
        SELECT a.session_id FROM iwiki.code_graph_publication_attempts a
        WHERE a.iwiki_id = tenant AND a.domain_id = domain AND a.recovery_deadline <= cutoff
        ORDER BY a.recovery_deadline LIMIT batch_limit FOR UPDATE SKIP LOCKED
    LOOP
        PERFORM iwiki.set_publication_cleanup(tenant, domain, item.session_id, NULL);
        DELETE FROM iwiki.code_graph_publication_attempts a
          WHERE a.iwiki_id = tenant AND a.domain_id = domain AND a.session_id = item.session_id;
    END LOOP;
    FOR item IN
        SELECT s.session_id, s.snapshot_id FROM iwiki.code_graph_publication_sessions s
        WHERE s.iwiki_id = tenant AND s.domain_id = domain
          AND s.state = 'staging' AND s.lease_expires_at <= expired_before
        ORDER BY s.lease_expires_at LIMIT batch_limit FOR UPDATE SKIP LOCKED
    LOOP
        PERFORM iwiki.set_publication_cleanup(tenant, domain, item.session_id, item.snapshot_id);
        DELETE FROM iwiki.code_graph_publication_sessions s
          WHERE s.iwiki_id = tenant AND s.domain_id = domain AND s.session_id = item.session_id;
        DELETE FROM iwiki.code_graph_snapshots s
          WHERE s.iwiki_id = tenant AND s.domain_id = domain AND s.snapshot_id = item.snapshot_id
            AND s.state = 'staging';
        removed := removed + 1;
    END LOOP;
    -- Reclaim historical terminal payloads without deleting the terminal receipt.
    FOR item IN
        SELECT s.session_id, s.snapshot_id, b.kind, b.ordinal
        FROM iwiki.code_graph_publication_sessions s
        JOIN iwiki.code_graph_batches b USING (iwiki_id, domain_id, session_id)
        WHERE s.iwiki_id = tenant AND s.domain_id = domain
          AND s.state <> 'staging' AND s.updated_at <= expired_before
        LIMIT batch_limit FOR UPDATE OF s SKIP LOCKED
    LOOP
        PERFORM iwiki.set_publication_cleanup(tenant, domain, item.session_id, item.snapshot_id);
        DELETE FROM iwiki.code_graph_batches b
          WHERE b.iwiki_id = tenant AND b.domain_id = domain AND b.session_id = item.session_id
            AND b.kind = item.kind AND b.ordinal = item.ordinal;
    END LOOP;
    PERFORM iwiki.set_publication_cleanup(tenant, domain, NULL, NULL);
    RETURN removed;
END;
$$;

CREATE OR REPLACE FUNCTION iwiki.cleanup_publication_snapshot(
    tenant text, domain bigint, snapshot text, table_name text, retention_seconds integer,
    row_limit integer
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    removed integer;
    publication_session text;
    cutoff timestamptz;
    precedes text;
    children_exist boolean;
BEGIN
    IF iwiki.publication_may_clean(tenant, domain) IS DISTINCT FROM true THEN
        RAISE insufficient_privilege;
    END IF;
    IF table_name IS NULL OR table_name NOT IN (
        'code_graph_wiki_links', 'code_graph_relations', 'code_graph_symbols',
        'code_graph_files', 'code_graph_snapshots'
    ) OR row_limit IS NULL OR row_limit NOT BETWEEN 1 AND 10000 OR retention_seconds IS NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    SELECT iwiki.publication_now()
           - make_interval(secs => greatest(retention_seconds, p.superseded_retention_seconds))
      INTO STRICT cutoff FROM iwiki.publication_policy p WHERE p.singleton;
    PERFORM 1 FROM iwiki.code_graph_snapshots s
      JOIN iwiki.code_graph_domain_state d USING (iwiki_id, domain_id)
      WHERE s.iwiki_id = tenant AND s.domain_id = domain AND s.snapshot_id = snapshot
        AND s.state = 'ready' AND s.ready_at <= cutoff
        AND s.snapshot_id IS DISTINCT FROM d.active_snapshot_id
      FOR SHARE OF d;
    IF NOT FOUND THEN RETURN 0; END IF;
    -- Enforce children-first removal even for a direct SQL invocation.
    FOR precedes IN SELECT unnest(CASE table_name
        WHEN 'code_graph_relations' THEN ARRAY['code_graph_wiki_links']
        WHEN 'code_graph_symbols' THEN ARRAY['code_graph_relations']
        WHEN 'code_graph_files' THEN ARRAY['code_graph_symbols','code_graph_relations']
        ELSE ARRAY[]::text[] END)
    LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM iwiki.%I '
            'WHERE iwiki_id = $1 AND domain_id = $2 AND snapshot_id = $3)', precedes)
            INTO children_exist USING tenant, domain, snapshot;
        IF children_exist THEN RETURN 0; END IF;
    END LOOP;
    SELECT s.session_id INTO publication_session FROM iwiki.code_graph_publication_sessions s
      WHERE s.iwiki_id = tenant AND s.domain_id = domain AND s.snapshot_id = snapshot;
    PERFORM iwiki.set_publication_cleanup(tenant, domain, publication_session, snapshot);
    IF table_name = 'code_graph_snapshots' THEN
        DELETE FROM iwiki.code_graph_snapshots s
          WHERE s.iwiki_id = tenant AND s.domain_id = domain AND s.snapshot_id = snapshot
            AND NOT EXISTS (
                SELECT 1 FROM iwiki.code_graph_files f
                  WHERE f.iwiki_id = tenant AND f.domain_id = domain AND f.snapshot_id = snapshot
            );
    ELSE
        EXECUTE format(
            'DELETE FROM iwiki.%1$I WHERE ctid IN (SELECT ctid FROM iwiki.%1$I '
            'WHERE iwiki_id = $1 AND domain_id = $2 AND snapshot_id = $3 LIMIT $4)', table_name
        ) USING tenant, domain, snapshot, row_limit;
    END IF;
    GET DIAGNOSTICS removed = ROW_COUNT;
    PERFORM iwiki.set_publication_cleanup(tenant, domain, NULL, NULL);
    RETURN removed;
END;
$$;
REVOKE ALL ON FUNCTION iwiki.cleanup_publication_staging(text,bigint,integer,integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION iwiki.cleanup_publication_snapshot(text,bigint,text,text,integer,integer)
    FROM PUBLIC;
"""
