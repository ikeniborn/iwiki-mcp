"""Schema 11 private authorization and quota accounting for graph publication."""

PRIVATE_SCHEMA_SQL = """
CREATE TABLE iwiki.publication_principals (
    pair_id uuid NOT NULL DEFAULT gen_random_uuid(),
    issuer regrole PRIMARY KEY,
    publisher regrole NOT NULL UNIQUE CHECK (publisher <> issuer)
);
CREATE TABLE iwiki.publication_policy (
    singleton boolean PRIMARY KEY CHECK (singleton),
    limits bigint[] NOT NULL CHECK (array_ndims(limits) = 1 AND array_length(limits, 1) = 4
        AND array_lower(limits, 1) = 1 AND array_position(limits, NULL) IS NULL
        AND 0 < ALL(limits)),
    staging_retention_seconds integer NOT NULL DEFAULT 86400,
    superseded_retention_seconds integer NOT NULL DEFAULT 86400,
    session_ttl_seconds integer NOT NULL DEFAULT 900,
    staging_cleanup_limit integer NOT NULL DEFAULT 100
);
INSERT INTO iwiki.publication_policy (singleton,limits)
VALUES (true,ARRAY[64,8,2147483648,268435456]::bigint[]);
CREATE TABLE iwiki.publication_usage (
    iwiki_id text NOT NULL, domain_id bigint NOT NULL, session_id text NOT NULL,
    owner_kind text NOT NULL CHECK (owner_kind IN ('hosted','direct','legacy')),
    quota_owner text NOT NULL, session_owner text NOT NULL,
    is_staging boolean NOT NULL, expires_at timestamptz NOT NULL,
    charged_bytes bigint NOT NULL CHECK (charged_bytes >= 0),
    PRIMARY KEY (iwiki_id, domain_id, session_id)
);
CREATE INDEX publication_usage_live_idx ON iwiki.publication_usage
    (expires_at, owner_kind, quota_owner) WHERE is_staging;
CREATE INDEX publication_usage_owner_idx ON iwiki.publication_usage
    (owner_kind, quota_owner) INCLUDE (charged_bytes) WHERE charged_bytes > 0;
LOCK TABLE iwiki.code_graph_publication_sessions, iwiki.code_graph_batches
    IN SHARE ROW EXCLUSIVE MODE;
INSERT INTO iwiki.publication_usage
SELECT s.iwiki_id, s.domain_id, s.session_id,
    CASE WHEN t.token_id IS NULL THEN 'legacy' ELSE 'hosted' END, s.owner_id, s.owner_id,
    s.state = 'staging', least(s.lease_expires_at, s.recovery_deadline),
    coalesce(b.bytes, 0)
FROM iwiki.code_graph_publication_sessions s
LEFT JOIN iwiki.tokens t ON (t.iwiki_id,t.token_id) = (s.iwiki_id,s.owner_id)
LEFT JOIN (
    SELECT iwiki_id,domain_id,session_id,sum(byte_count)::bigint AS bytes
    FROM iwiki.code_graph_batches GROUP BY iwiki_id,domain_id,session_id
) b ON (s.iwiki_id,s.domain_id,s.session_id)=(b.iwiki_id,b.domain_id,b.session_id);
ALTER TABLE iwiki.code_graph_publication_attempts
    ADD COLUMN quota_owner_kind text,
    ADD COLUMN quota_owner text;
UPDATE iwiki.code_graph_publication_attempts a SET
    quota_owner_kind=CASE WHEN EXISTS(SELECT FROM iwiki.tokens t WHERE
        (t.iwiki_id,t.token_id)=(a.iwiki_id,a.owner_id)) THEN 'hosted' ELSE 'legacy' END,
    quota_owner=a.owner_id;
ALTER TABLE iwiki.code_graph_publication_attempts
    ALTER COLUMN quota_owner_kind SET NOT NULL, ALTER COLUMN quota_owner SET NOT NULL;
CREATE TABLE iwiki.publication_tickets (
    digest bytea PRIMARY KEY CHECK (octet_length(digest) = 32),
    publisher regrole NOT NULL, iwiki_id text NOT NULL, domain_id bigint NOT NULL,
    owner_id text NOT NULL, operation text NOT NULL, arguments jsonb NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX publication_tickets_expiry ON iwiki.publication_tickets (expires_at);
CREATE TABLE iwiki.publication_authorizations (
    transaction_id bigint PRIMARY KEY, iwiki_id text NOT NULL, domain_id bigint NOT NULL,
    owner_id text, owner_kind text, quota_owner text, operation text NOT NULL,
    arguments jsonb, session_id text, snapshot_id text
);
CREATE FUNCTION iwiki.publication_pair_status()
    RETURNS TABLE(pair_id uuid, member text, limits bigint[], lifecycle integer[])
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT p.pair_id,CASE WHEN p.issuer=session_user::regrole THEN 'issuer' ELSE 'publisher' END,
        CASE WHEN p.issuer=session_user::regrole THEN q.limits END,
        CASE WHEN p.issuer=session_user::regrole THEN
            ARRAY[q.staging_retention_seconds,q.superseded_retention_seconds,
                q.session_ttl_seconds,q.staging_cleanup_limit] END
    FROM iwiki.publication_principals p CROSS JOIN iwiki.publication_policy q
    WHERE session_user::regrole IN (p.issuer,p.publisher)
$$;
CREATE FUNCTION iwiki.publication_forget_authorization() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    DELETE FROM iwiki.publication_authorizations WHERE transaction_id = NEW.transaction_id;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER forget_publication_authorization
    AFTER INSERT ON iwiki.publication_authorizations
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    EXECUTE FUNCTION iwiki.publication_forget_authorization();
CREATE FUNCTION iwiki.publication_now() RETURNS timestamptz
    LANGUAGE sql VOLATILE SET search_path = pg_catalog AS $$ SELECT clock_timestamp() $$;

CREATE OR REPLACE FUNCTION iwiki.database_principal_can_access(
    requested_iwiki text, requested_domain bigint, requested_write boolean
) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT FROM iwiki.database_principal_domain_grants g
        WHERE g.iwiki_id=requested_iwiki AND g.domain_id=requested_domain
          AND g.can_read AND (NOT requested_write OR g.can_write)
          AND (g.principal=session_user OR (g.runtime='hosted' AND EXISTS (
              SELECT FROM iwiki.publication_principals p JOIN pg_roles r ON r.oid=p.issuer
              WHERE p.publisher=session_user::regrole AND r.rolname=g.principal
          )))
    )
$$;
CREATE FUNCTION iwiki.issue_publication_ticket(
    ticket_digest bytea, tenant text, token text, domain_slug text, op text,
    claims jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE pub regrole; target bigint;
BEGIN
    SELECT publisher INTO STRICT pub FROM iwiki.publication_principals
        WHERE issuer=session_user::regrole;
    IF op NOT IN ('begin','batch','finalize','abort','inspect') OR op IS NULL
        OR jsonb_typeof(claims) IS DISTINCT FROM 'object' THEN RAISE insufficient_privilege; END IF;
    SELECT d.domain_id INTO target FROM iwiki.domains d
        JOIN iwiki.token_domain_grants g USING(iwiki_id,domain_id)
        JOIN iwiki.tokens t USING(iwiki_id,token_id) JOIN iwiki.iwikis w USING(iwiki_id)
        WHERE d.iwiki_id=tenant AND d.slug=domain_slug AND g.token_id=token AND g.can_write
        AND t.revoked_at IS NULL AND w.active
        AND iwiki.database_principal_can_access(tenant,d.domain_id,true);
    IF NOT FOUND THEN RAISE insufficient_privilege; END IF;
    DELETE FROM iwiki.publication_tickets WHERE digest IN (
        SELECT digest FROM iwiki.publication_tickets WHERE expires_at <= clock_timestamp()
        ORDER BY expires_at LIMIT 32 FOR UPDATE SKIP LOCKED
    );
    INSERT INTO iwiki.publication_tickets VALUES
        (ticket_digest,pub,tenant,target,token,op,claims,clock_timestamp()+interval '30 seconds');
END $$;

CREATE FUNCTION iwiki.authorize_code_publication(
    tenant text, target bigint, requested_owner text, op text, claims jsonb, ticket bytea,
    generated_session text DEFAULT NULL, generated_snapshot text DEFAULT NULL
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE kind text; quota_id text; sid text; snap text; subject text;
BEGIN
    IF op NOT IN ('begin','batch','finalize','abort','inspect') OR op IS NULL
        OR jsonb_typeof(claims) IS DISTINCT FROM 'object' THEN
        RAISE insufficient_privilege;
    END IF;
    IF ticket IS NOT NULL THEN
        DELETE FROM iwiki.publication_tickets t
        WHERE t.digest=sha256(ticket) AND t.publisher=session_user::regrole
            AND t.iwiki_id=tenant AND t.domain_id=target AND t.owner_id=requested_owner
            AND t.operation=op AND t.arguments=claims AND t.expires_at>clock_timestamp()
            AND EXISTS (SELECT FROM iwiki.publication_principals p WHERE p.publisher=t.publisher)
            AND iwiki.database_principal_can_access(tenant,target,true)
            AND EXISTS (SELECT FROM iwiki.token_domain_grants g
                JOIN iwiki.tokens a USING (iwiki_id,token_id) JOIN iwiki.iwikis w USING(iwiki_id)
                WHERE g.iwiki_id=tenant AND g.domain_id=target AND g.token_id=t.owner_id
                AND g.can_write AND a.revoked_at IS NULL AND w.active)
        RETURNING t.owner_id INTO subject;
        IF NOT FOUND THEN RAISE insufficient_privilege; END IF;
        kind := 'hosted'; quota_id := subject;
    ELSIF EXISTS (SELECT FROM iwiki.database_principal_domain_grants
        WHERE principal=session_user AND runtime='direct' AND iwiki_id=tenant
        AND domain_id=target AND can_write)
        AND NOT EXISTS (SELECT FROM iwiki.publication_principals WHERE
            issuer=session_user::regrole OR publisher=session_user::regrole)
    THEN
        kind := 'direct'; quota_id := session_user::regrole::oid::text; subject := requested_owner;
    ELSIF session_user=current_user THEN
        -- The schema owner runs migration/repair and disposable integration fixtures only.
        kind := 'legacy'; quota_id := requested_owner; subject := requested_owner;
    ELSE RAISE insufficient_privilege;
    END IF;
    IF op='begin' THEN
        sid := generated_session; snap := generated_snapshot;
        IF sid IS NULL OR snap IS NULL OR (ticket IS NOT NULL AND (
            sid IS DISTINCT FROM claims->>'generated_session_id'
            OR snap IS DISTINCT FROM claims->>'generated_snapshot_id'
        )) THEN RAISE insufficient_privilege; END IF;
    ELSE
        sid := claims->>'session_id';
        SELECT s.snapshot_id INTO snap FROM iwiki.code_graph_publication_sessions s
        JOIN iwiki.publication_usage u USING(iwiki_id,domain_id,session_id)
        WHERE (s.iwiki_id,s.domain_id,s.session_id,s.owner_id)=(tenant,target,sid,subject)
        AND (u.owner_kind='legacy' OR (u.owner_kind,u.quota_owner)=(kind,quota_id));
    END IF;
    INSERT INTO iwiki.publication_authorizations VALUES
        (txid_current(),tenant,target,subject,kind,quota_id,op,claims,sid,snap)
    ON CONFLICT(transaction_id) DO UPDATE SET
        iwiki_id=excluded.iwiki_id,domain_id=excluded.domain_id,owner_id=excluded.owner_id,
        owner_kind=excluded.owner_kind,quota_owner=excluded.quota_owner,
            operation=excluded.operation,
        arguments=excluded.arguments,session_id=excluded.session_id,
            snapshot_id=excluded.snapshot_id;
END $$;

CREATE FUNCTION iwiki.publication_may_clean(tenant text,target bigint) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT session_user=current_user OR iwiki.database_principal_can_access(tenant,target,true)
$$;
CREATE FUNCTION iwiki.set_publication_cleanup(tenant text,target bigint,sid text,snap text)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
    DELETE FROM iwiki.publication_authorizations WHERE transaction_id=txid_current();
    IF sid IS NULL AND snap IS NULL THEN RETURN; END IF;
    INSERT INTO iwiki.publication_authorizations
        (transaction_id,iwiki_id,domain_id,operation,session_id,snapshot_id)
        VALUES (txid_current(),tenant,target,'cleanup',sid,snap);
END $$;
"""

ACCOUNTING_SQL = """
ALTER TABLE iwiki.code_graph_publication_sessions ADD CONSTRAINT quota_usage_required
    FOREIGN KEY (iwiki_id,domain_id,session_id)
    REFERENCES iwiki.publication_usage(iwiki_id,domain_id,session_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION iwiki.enforce_publication_quota(
    is_slot boolean, delta bigint, owner_type text, owner_key text, expires timestamptz
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE budgets bigint[]; used numeric; owned numeric; admitted_at timestamptz;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'publication admission requires read committed';
    END IF;
    PERFORM pg_advisory_xact_lock(7595435311942266218::bigint);
    admitted_at:=iwiki.publication_now();
    IF expires <= admitted_at THEN
        RAISE EXCEPTION USING ERRCODE='IQ003', MESSAGE='publication lease expired';
    END IF;
    SELECT limits INTO STRICT budgets FROM iwiki.publication_policy;
    IF is_slot THEN
        SELECT count(*),count(*) FILTER (WHERE owner_kind=owner_type AND quota_owner=owner_key)
            INTO used,owned FROM iwiki.publication_usage
            WHERE is_staging AND expires_at>admitted_at;
        IF used+1>budgets[1] OR owned+1>budgets[2] THEN
            RAISE EXCEPTION USING ERRCODE='IQ001', MESSAGE='publication session capacity exceeded';
        END IF;
    ELSIF delta>0 THEN
        SELECT coalesce(sum(charged_bytes),0),
            coalesce(sum(charged_bytes) FILTER (WHERE owner_kind=owner_type AND
                quota_owner=owner_key),0)
            INTO used,owned FROM iwiki.publication_usage WHERE charged_bytes > 0;
        IF used+delta>budgets[3] OR owned+delta>budgets[4] THEN
            RAISE EXCEPTION USING ERRCODE='IQ002', MESSAGE='publication staging capacity exceeded';
        END IF;
    END IF;
END $$;

CREATE FUNCTION iwiki.guard_publication_session() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a iwiki.publication_authorizations; u iwiki.publication_usage;
    row_data iwiki.code_graph_publication_sessions; privileged boolean;
BEGIN
    IF TG_OP='INSERT' THEN row_data:=NEW; ELSE row_data:=OLD; END IF;
    SELECT * INTO a FROM iwiki.publication_authorizations WHERE transaction_id=txid_current();
    privileged := session_user=current_user AND a.operation IS NULL;
    IF NOT privileged AND (
        (row_data.iwiki_id,row_data.domain_id,row_data.session_id)
            IS DISTINCT FROM (a.iwiki_id,a.domain_id,a.session_id)
        OR (a.operation <> 'cleanup' AND a.owner_id IS DISTINCT FROM row_data.owner_id)
    ) THEN RAISE insufficient_privilege; END IF;
    IF TG_OP='INSERT' THEN
        IF NOT privileged AND (a.operation IS DISTINCT FROM 'begin'
            OR NEW.state<>'staging' OR NEW.snapshot_id IS DISTINCT FROM a.snapshot_id
            OR NEW.recovery_deadline IS DISTINCT FROM to_timestamp((
                a.arguments->>'recovery_deadline')::bigint)
            OR NEW.terminal_result IS NOT NULL
        ) THEN RAISE insufficient_privilege; END IF;
        PERFORM iwiki.enforce_publication_quota(true,1,coalesce(a.owner_kind,'legacy'),coalesce(
            a.quota_owner,NEW.owner_id),
            CASE WHEN session_user<>current_user THEN least(NEW.lease_expires_at,
                NEW.recovery_deadline) END);
        IF NOT privileged AND session_user<>current_user AND (
            NEW.lease_expires_at<=iwiki.publication_now()
            OR (a.owner_kind='hosted' AND NEW.lease_expires_at>iwiki.publication_now()
                +make_interval(secs=>(SELECT session_ttl_seconds FROM iwiki.publication_policy))))
        THEN RAISE EXCEPTION USING ERRCODE='IQ003', MESSAGE='publication lease expired'; END IF;
        INSERT INTO iwiki.publication_usage VALUES (NEW.iwiki_id,NEW.domain_id,NEW.session_id,
            coalesce(a.owner_kind,'legacy'),coalesce(a.quota_owner,NEW.owner_id),NEW.owner_id,
            NEW.state='staging',least(NEW.lease_expires_at,NEW.recovery_deadline),0);
        RETURN NEW;
    END IF;
    SELECT * INTO STRICT u FROM iwiki.publication_usage WHERE
        (iwiki_id,domain_id,session_id)=(OLD.iwiki_id,OLD.domain_id,OLD.session_id) FOR UPDATE;
    IF (u.is_staging,u.expires_at,u.session_owner) IS DISTINCT FROM
        (OLD.state='staging',least(OLD.lease_expires_at,OLD.recovery_deadline),OLD.owner_id)
    THEN RAISE EXCEPTION 'publication accounting inconsistency'; END IF;
    IF NOT privileged AND a.operation <> 'cleanup' AND u.owner_kind <> 'legacy'
        AND (u.owner_kind,u.quota_owner) IS DISTINCT FROM (a.owner_kind,a.quota_owner)
    THEN RAISE insufficient_privilege; END IF;
    IF TG_OP='DELETE' THEN
        IF NOT privileged AND a.operation IS DISTINCT FROM 'cleanup' THEN RAISE
            insufficient_privilege; END IF;
        DELETE FROM iwiki.publication_usage WHERE
            (iwiki_id,domain_id,session_id)=(OLD.iwiki_id,OLD.domain_id,OLD.session_id);
        RETURN OLD;
    END IF;
    IF NOT privileged THEN
        IF (to_jsonb(NEW)-ARRAY['state','terminal_result','lease_expires_at','updated_at'])
            IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','terminal_result','lease_expires_at',
                'updated_at'])
            OR OLD.state <> 'staging' THEN RAISE insufficient_privilege; END IF;
        IF a.operation='batch' THEN
            IF NEW.state<>'staging' OR NEW.terminal_result IS NOT NULL OR NOT EXISTS (
                SELECT FROM iwiki.code_graph_batches b WHERE
                    (b.iwiki_id,b.domain_id,b.session_id)=(OLD.iwiki_id,OLD.domain_id,
                        OLD.session_id)
                    AND b.kind=a.arguments->>'kind' AND b.ordinal=(a.arguments->>'ordinal')::int
                    AND b.payload_hash=a.arguments->>'payload_hash'
            ) THEN RAISE insufficient_privilege; END IF;
            PERFORM pg_advisory_xact_lock(7595435311942266218::bigint);
            IF session_user<>current_user AND (u.expires_at<=iwiki.publication_now()
                OR NEW.lease_expires_at<u.expires_at OR (a.owner_kind='hosted'
                AND NEW.lease_expires_at>iwiki.publication_now()
                +make_interval(secs=>(SELECT session_ttl_seconds FROM iwiki.publication_policy))))
            THEN RAISE EXCEPTION USING ERRCODE='IQ003', MESSAGE='publication lease expired'; END IF;
        ELSIF a.operation='finalize' THEN
            IF NEW.state<>'ready' OR NEW.lease_expires_at<>OLD.lease_expires_at OR NOT EXISTS (
                SELECT FROM iwiki.code_graph_domain_state WHERE iwiki_id=OLD.iwiki_id
                AND domain_id=OLD.domain_id AND active_snapshot_id=OLD.snapshot_id
            ) THEN RAISE insufficient_privilege; END IF;
        ELSIF a.operation='abort' THEN
            IF NEW.state<>'aborted' OR NEW.lease_expires_at<>OLD.lease_expires_at
            THEN RAISE insufficient_privilege; END IF;
        ELSE RAISE insufficient_privilege;
        END IF;
    END IF;
    UPDATE iwiki.publication_usage SET is_staging=NEW.state='staging',
        expires_at=least(NEW.lease_expires_at,NEW.recovery_deadline)
        WHERE (iwiki_id,domain_id,session_id)=(OLD.iwiki_id,OLD.domain_id,OLD.session_id);
    RETURN NEW;
END $$;
CREATE TRIGGER quota_guard BEFORE INSERT OR UPDATE OR DELETE ON
    iwiki.code_graph_publication_sessions
    FOR EACH ROW EXECUTE FUNCTION iwiki.guard_publication_session();

CREATE FUNCTION iwiki.guard_publication_batch() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a iwiki.publication_authorizations; u iwiki.publication_usage; privileged boolean;
    b iwiki.code_graph_batches;
BEGIN
    SELECT * INTO a FROM iwiki.publication_authorizations WHERE transaction_id=txid_current();
    privileged := session_user=current_user AND a.operation IS NULL;
    IF TG_OP='UPDATE' THEN RAISE insufficient_privilege; END IF;
    IF TG_OP='INSERT' THEN b:=NEW; ELSE b:=OLD; END IF;
    IF NOT privileged AND (b.iwiki_id,b.domain_id,b.session_id)
        IS DISTINCT FROM (a.iwiki_id,a.domain_id,a.session_id)
    THEN RAISE insufficient_privilege; END IF;
    SELECT * INTO u FROM iwiki.publication_usage WHERE
        (iwiki_id,domain_id,session_id)=(b.iwiki_id,b.domain_id,b.session_id) FOR UPDATE;
    IF NOT FOUND THEN
        IF TG_OP='DELETE' AND NOT EXISTS (SELECT FROM iwiki.code_graph_publication_sessions WHERE
            (iwiki_id,domain_id,session_id)=(b.iwiki_id,b.domain_id,b.session_id))
        THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'publication accounting unavailable';
    END IF;
    IF NOT privileged AND a.operation<>'cleanup' AND (a.owner_id IS DISTINCT FROM u.session_owner
        OR (u.owner_kind <> 'legacy' AND (a.owner_kind,a.quota_owner)
            IS DISTINCT FROM (u.owner_kind,u.quota_owner)))
    THEN RAISE insufficient_privilege; END IF;
    IF TG_OP='INSERT' THEN
        IF NOT privileged AND (a.operation IS DISTINCT FROM 'batch' OR NOT u.is_staging
            OR (b.kind,b.ordinal,b.payload_hash,b.row_count,b.byte_count) IS DISTINCT FROM
              (a.arguments->>'kind',(a.arguments->>'ordinal')::int,a.arguments->>'payload_hash',
               (a.arguments->>'row_count')::int,(a.arguments->>'byte_count')::int)
            OR b.byte_count<>octet_length(b.payload)
            OR b.payload_hash<>'sha256:' || encode(sha256(b.payload),'hex'))
        THEN RAISE insufficient_privilege; END IF;
        PERFORM iwiki.enforce_publication_quota(false,b.byte_count,u.owner_kind,u.quota_owner,
            CASE WHEN session_user<>current_user THEN u.expires_at END);
        IF NOT privileged AND session_user<>current_user AND u.expires_at<=iwiki.publication_now()
        THEN RAISE EXCEPTION USING ERRCODE='IQ003', MESSAGE='publication lease expired'; END IF;
    ELSIF NOT privileged AND (a.operation NOT IN ('abort','finalize','cleanup') OR u.is_staging)
    THEN RAISE insufficient_privilege;
    END IF;
    UPDATE iwiki.publication_usage SET charged_bytes=charged_bytes+
        CASE WHEN TG_OP='INSERT' THEN b.byte_count ELSE -b.byte_count END
        WHERE (iwiki_id,domain_id,session_id)=(b.iwiki_id,b.domain_id,b.session_id);
    RETURN NULL;
END $$;
CREATE TRIGGER quota_account AFTER INSERT OR UPDATE OR DELETE ON iwiki.code_graph_batches
    FOR EACH ROW EXECUTE FUNCTION iwiki.guard_publication_batch();
"""

GUARDS_SQL = """
CREATE FUNCTION iwiki.publication_session_visible(tenant text,target bigint,sid text,subject text,
    key text)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT session_user=current_user
        OR EXISTS (SELECT FROM iwiki.publication_authorizations a
            WHERE a.transaction_id=txid_current() AND a.iwiki_id=tenant AND a.domain_id=target
            AND (subject IS NULL OR a.owner_id=subject)
            AND (a.session_id=sid OR (a.operation='begin' AND (
                a.arguments->>'attempt_key'=key OR EXISTS (
                    SELECT FROM iwiki.code_graph_publication_attempts t WHERE
                    t.iwiki_id=tenant AND t.domain_id=target AND t.session_id=sid
                    AND t.owner_id=a.owner_id AND t.attempt_key=a.arguments->>'attempt_key'
                )
            ))) AND EXISTS (SELECT 1 FROM iwiki.code_graph_publication_sessions s WHERE
                (s.iwiki_id,s.domain_id,s.session_id,s.owner_id)=(tenant,target,sid,a.owner_id)
                AND EXISTS (SELECT FROM iwiki.publication_usage u WHERE
                    (u.iwiki_id,u.domain_id,u.session_id)=(tenant,target,sid)
                    AND (u.owner_kind='legacy' OR (u.owner_kind,u.quota_owner)=(a.owner_kind,
                        a.quota_owner)))
                UNION ALL SELECT 1 FROM iwiki.code_graph_publication_attempts t WHERE
                (t.iwiki_id,t.domain_id,t.session_id,t.owner_id)=(tenant,target,sid,a.owner_id)
                AND (t.quota_owner_kind='legacy' OR
                    (t.quota_owner_kind,t.quota_owner)=(a.owner_kind,a.quota_owner))))
$$;
CREATE FUNCTION iwiki.publication_snapshot_visible(tenant text,target bigint,snap text)
    RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT session_user=current_user OR EXISTS(SELECT FROM iwiki.publication_authorizations a
        WHERE (a.transaction_id,a.iwiki_id,a.domain_id,a.snapshot_id)=(txid_current(),tenant,
            target,snap))
$$;
CREATE POLICY quota_snapshot_visibility ON iwiki.code_graph_snapshots
    AS RESTRICTIVE FOR SELECT USING (state<>'staging' OR
        iwiki.publication_snapshot_visible(iwiki_id,domain_id,snapshot_id));
CREATE POLICY quota_session_visibility ON iwiki.code_graph_publication_sessions
    AS RESTRICTIVE FOR SELECT USING (iwiki.publication_session_visible(iwiki_id,domain_id,
        session_id,owner_id,NULL));
CREATE POLICY quota_attempt_visibility ON iwiki.code_graph_publication_attempts
    AS RESTRICTIVE FOR SELECT USING (iwiki.publication_session_visible(iwiki_id,domain_id,
        session_id,owner_id,attempt_key));
CREATE POLICY quota_batch_visibility ON iwiki.code_graph_batches
    AS RESTRICTIVE FOR SELECT USING (iwiki.publication_session_visible(iwiki_id,domain_id,
        session_id,NULL,NULL));

CREATE FUNCTION iwiki.guard_publication_child_insert() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a iwiki.publication_authorizations;
BEGIN
    SELECT * INTO a FROM iwiki.publication_authorizations WHERE transaction_id=txid_current();
    IF a.operation IS NULL AND (session_user=current_user OR (
        TG_TABLE_NAME='code_graph_wiki_links' AND NOT EXISTS (
            SELECT FROM iwiki.publication_principals WHERE publisher=session_user::regrole
        ) AND NOT EXISTS (
            SELECT FROM inserted i WHERE NOT EXISTS (
                SELECT FROM iwiki.code_graph_domain_state d WHERE (d.iwiki_id,d.domain_id,
                    d.active_snapshot_id)
                    =(i.iwiki_id,i.domain_id,i.snapshot_id))
        )
    )) THEN RETURN NULL; END IF;
    IF a.operation IS DISTINCT FROM 'finalize' OR EXISTS (
        SELECT FROM inserted i WHERE (i.iwiki_id,i.domain_id,i.snapshot_id)
            IS DISTINCT FROM (a.iwiki_id,a.domain_id,a.snapshot_id)
    ) OR NOT EXISTS (SELECT FROM iwiki.code_graph_publication_sessions s
        JOIN iwiki.publication_usage u USING (iwiki_id,domain_id,session_id) WHERE
        (s.iwiki_id,s.domain_id,s.session_id,s.snapshot_id,s.owner_id)=
        (a.iwiki_id,a.domain_id,a.session_id,a.snapshot_id,a.owner_id) AND s.state='staging'
        AND (u.owner_kind='legacy' OR (u.owner_kind,u.quota_owner)=(a.owner_kind,a.quota_owner)))
    THEN RAISE insufficient_privilege; END IF;
    RETURN NULL;
END $$;
CREATE FUNCTION iwiki.guard_publication_root() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a iwiki.publication_authorizations; r jsonb; prev jsonb; allowed boolean:=false;
BEGIN
    IF TG_OP<>'INSERT' THEN prev:=to_jsonb(OLD); END IF;
    IF TG_OP='DELETE' THEN r:=prev; ELSE r:=to_jsonb(NEW); END IF;
    SELECT * INTO a FROM iwiki.publication_authorizations WHERE transaction_id=txid_current();
    IF TG_TABLE_NAME='code_graph_publication_attempts' AND TG_OP='INSERT' THEN
        NEW.quota_owner_kind:=coalesce(a.owner_kind,'legacy');
        NEW.quota_owner:=coalesce(a.quota_owner,NEW.owner_id);
        r:=to_jsonb(NEW);
    END IF;
    IF a.operation IS NULL AND session_user=current_user THEN
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    IF TG_TABLE_NAME='code_graph_snapshots' AND TG_OP='UPDATE' AND a.operation IS NULL
        AND prev->>'state'='ready'
        AND r-ARRAY['markdown_generation','markdown_revision']=prev-ARRAY['markdown_generation',
            'markdown_revision']
        AND NOT EXISTS (SELECT FROM iwiki.publication_principals WHERE
            publisher=session_user::regrole)
    THEN RETURN NEW; END IF;
    IF (a.iwiki_id,a.domain_id) IS DISTINCT FROM (r->>'iwiki_id',(r->>'domain_id')::bigint)
    THEN RAISE insufficient_privilege; END IF;
    IF TG_TABLE_NAME='code_graph_publication_attempts' THEN
        IF r->>'session_id' IS DISTINCT FROM a.session_id THEN RAISE insufficient_privilege; END IF;
        allowed := CASE TG_OP
            WHEN 'DELETE' THEN a.operation='cleanup'
            WHEN 'INSERT' THEN a.operation='begin' AND r->>'owner_id'=a.owner_id
                AND r->>'attempt_key'=a.arguments->>'attempt_key' AND r->>'terminal_result' IS NULL
                AND (r->>'recovery_deadline')::timestamptz=to_timestamp((
                    a.arguments->>'recovery_deadline')::bigint)
                AND r->>'max_batch_rows'=a.arguments->>'max_batch_rows'
                AND r->>'max_batch_bytes'=a.arguments->>'max_batch_bytes'
            ELSE a.operation IN ('finalize','abort') AND r-'terminal_result'=prev-'terminal_result'
                AND r->>'owner_id'=a.owner_id AND EXISTS (
                    SELECT FROM iwiki.code_graph_publication_sessions s WHERE
                    (s.iwiki_id,s.domain_id,s.session_id)=(a.iwiki_id,a.domain_id,a.session_id)
                    AND s.terminal_result=r->'terminal_result')
        END;
    ELSIF TG_TABLE_NAME='code_graph_domain_state' THEN
        allowed := CASE TG_OP WHEN 'INSERT' THEN a.operation IN ('begin','finalize')
            AND r->>'active_snapshot_id' IS NULL
            WHEN 'UPDATE' THEN a.operation='finalize' AND r->>'active_snapshot_id'=a.snapshot_id
                AND r-ARRAY['active_snapshot_id','active_snapshot_state','updated_at']=
                    prev-ARRAY['active_snapshot_id','active_snapshot_state','updated_at']
            ELSE false END;
    ELSE
        IF r->>'snapshot_id' IS DISTINCT FROM a.snapshot_id THEN RAISE insufficient_privilege;
            END IF;
        allowed := CASE TG_OP WHEN 'DELETE' THEN a.operation='cleanup'
            WHEN 'INSERT' THEN a.operation='begin' AND r->>'state'='staging'
                AND r->'header'=a.arguments->'header' AND r->>'ready_at' IS NULL
            ELSE (a.operation='finalize' AND r->>'state'='ready' OR a.operation='abort' AND
                r->>'state'='failed')
                AND prev->>'state'='staging' AND r->>'snapshot_id'=prev->>'snapshot_id'
                AND r->>'iwiki_id'=prev->>'iwiki_id' AND r->>'domain_id'=prev->>'domain_id'
        END;
    END IF;
    IF allowed IS DISTINCT FROM true THEN RAISE insufficient_privilege; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
"""

ROOT_TABLES = ("code_graph_publication_attempts", "code_graph_domain_state", "code_graph_snapshots")
ROOT_TRIGGERS = tuple(
    f"CREATE TRIGGER quota_guard BEFORE INSERT OR UPDATE OR DELETE ON iwiki.{table} "
    "FOR EACH ROW EXECUTE FUNCTION iwiki.guard_publication_root()"
    for table in ROOT_TABLES
)

ISSUE = "issue_publication_ticket(bytea,text,text,text,text,jsonb)"
AUTHORIZE = "authorize_code_publication(text,bigint,text,text,jsonb,bytea,text,text)"
CLEAN = (
    "cleanup_publication_staging(text,bigint,integer,integer)",
    "cleanup_publication_snapshot(text,bigint,text,text,integer,integer)",
)
VISIBILITY = "publication_session_visible(text,bigint,text,text,text)"
SNAPSHOT_VISIBILITY = "publication_snapshot_visible(text,bigint,text)"
PRIVATE_FUNCTIONS = (
    "publication_pair_status()",
    "publication_forget_authorization()",
    "publication_now()",
    ISSUE,
    AUTHORIZE,
    "publication_may_clean(text,bigint)",
    "set_publication_cleanup(text,bigint,text,text)",
    "enforce_publication_quota(boolean,bigint,text,text,timestamptz)",
    "guard_publication_session()",
    "guard_publication_child_insert()",
    "guard_publication_batch()",
    VISIBILITY,
    SNAPSHOT_VISIBILITY,
    "guard_publication_root()",
    *CLEAN,
)
PRIVATE_TABLES = (
    "publication_principals",
    "publication_policy",
    "publication_tickets",
    "publication_authorizations",
    "publication_usage",
)


CHILD_TRIGGERS = tuple(
    f"CREATE TRIGGER quota_child_insert AFTER INSERT ON iwiki.{table} "
    "REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION "
    "iwiki.guard_publication_child_insert()"
    for table in (
        "code_graph_files",
        "code_graph_symbols",
        "code_graph_relations",
        "code_graph_wiki_links",
    )
)


def quota_migration_sql():
    from .publication_cleanup_sql import CLEANUP_SQL

    return (
        PRIVATE_SCHEMA_SQL,
        RESTORE_ROLLBACK_OWNERS,
        ACCOUNTING_SQL,
        GUARDS_SQL,
        *ROOT_TRIGGERS,
        *CHILD_TRIGGERS,
        CLEANUP_SQL,
        "REVOKE ALL ON FUNCTION "
        + ", ".join("iwiki." + f for f in PRIVATE_FUNCTIONS)
        + " FROM PUBLIC",
        "REVOKE ALL ON " + ", ".join("iwiki." + t for t in PRIVATE_TABLES) + " FROM PUBLIC",
        """
        DO $$ DECLARE r record; BEGIN
            FOR r IN SELECT DISTINCT principal FROM iwiki.database_principal_domain_grants LOOP
                IF to_regrole(quote_ident(r.principal)) IS NOT NULL THEN
                    EXECUTE format('GRANT EXECUTE ON FUNCTION
                        iwiki.publication_now(),
                        iwiki.publication_session_visible(text,bigint,text,text,text),
                        iwiki.publication_snapshot_visible(text,bigint,text),
                        iwiki.authorize_code_publication(text,bigint,text,text,jsonb,bytea,text,
                            text),
                        iwiki.cleanup_publication_staging(text,bigint,integer,integer),
                        iwiki.cleanup_publication_snapshot(text,bigint,text,text,integer,integer)
                            TO %I',r.principal);
                END IF;
            END LOOP;
        END $$;
        """,
    )


# Keep known identities on rollback even when a terminal attempt outlives
# its session. The v10 delete cascades remove the matching saved identity.
def preserve_rollback_owners_sql():
    statements = []
    for suffix, source, kind, owner in (
        ("sessions", "publication_usage", "owner_kind", "session_owner"),
        ("attempts", "code_graph_publication_attempts", "quota_owner_kind", "owner_id"),
    ):
        table = "iwiki.publication_rollback_" + suffix
        parent = "iwiki.code_graph_publication_" + suffix
        statements.append(f"""
            CREATE TABLE {table} (
                iwiki_id text NOT NULL, domain_id bigint NOT NULL, session_id text NOT NULL,
                session_owner text NOT NULL, owner_kind text NOT NULL, quota_owner text NOT NULL,
                PRIMARY KEY(iwiki_id,domain_id,session_id),
                FOREIGN KEY(iwiki_id,domain_id,session_id)
                    REFERENCES {parent}(iwiki_id,domain_id,session_id) ON DELETE CASCADE
            );
            REVOKE ALL ON {table} FROM PUBLIC;
            INSERT INTO {table}
                SELECT iwiki_id,domain_id,session_id,{owner},{kind},quota_owner
                FROM iwiki.{source};
        """)
    return "".join(statements)


RESTORE_ROLLBACK_OWNERS = """
DO $$ BEGIN
    IF to_regclass('iwiki.publication_rollback_sessions') IS NOT NULL THEN
        UPDATE iwiki.publication_usage u SET owner_kind=r.owner_kind,quota_owner=r.quota_owner
        FROM iwiki.publication_rollback_sessions r WHERE
            (u.iwiki_id,u.domain_id,u.session_id,u.session_owner)=
            (r.iwiki_id,r.domain_id,r.session_id,r.session_owner);
        DROP TABLE iwiki.publication_rollback_sessions;
        UPDATE iwiki.code_graph_publication_attempts a
        SET quota_owner_kind=r.owner_kind,quota_owner=r.quota_owner
        FROM iwiki.publication_rollback_attempts r WHERE
            (a.iwiki_id,a.domain_id,a.session_id,a.owner_id)=
            (r.iwiki_id,r.domain_id,r.session_id,r.session_owner);
        DROP TABLE iwiki.publication_rollback_attempts;
    END IF;
END $$;
"""
