CREATE TABLE IF NOT EXISTS design_jobs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    display_id text NOT NULL,
    job_type text NOT NULL CHECK (
        job_type IN ('mechanical_design','product_family_onboarding')
    ),
    title text NOT NULL CHECK (btrim(title) <> ''),
    slug text NOT NULL CHECK (btrim(slug) <> ''),
    status text NOT NULL CHECK (
        status IN ('active','blocked','completed','cancelled','archived')
    ),
    phase text NOT NULL,
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    family_id text REFERENCES product_families(id),
    directory_name text,
    idempotency_token text NOT NULL CHECK (btrim(idempotency_token) <> ''),
    blocked_reason jsonb,
    provisioning_state text NOT NULL DEFAULT 'provisioning' CHECK (
        provisioning_state IN ('provisioning','ready')
    ),
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT design_jobs_phase_check CHECK (
        (job_type = 'mechanical_design' AND phase IN (
            'requirements','design','validation','delivery','lesson_capture','completed'
        ))
        OR
        (job_type = 'product_family_onboarding' AND phase IN (
            'intake','analysis','knowledge_review','database_publication','completed'
        ))
    ),
    CONSTRAINT design_jobs_blocked_reason_check CHECK (
        (status = 'blocked' AND blocked_reason IS NOT NULL)
        OR
        (status <> 'blocked' AND blocked_reason IS NULL)
    ),
    CONSTRAINT design_jobs_directory_provisioning_check CHECK (
        directory_name IS NOT NULL OR provisioning_state = 'provisioning'
    ),
    UNIQUE(workspace_id,idempotency_token),
    UNIQUE(workspace_id,display_id)
);

CREATE INDEX IF NOT EXISTS design_jobs_scope_idx
    ON design_jobs(organization_id,design_group_id,status,job_type,updated_at DESC,id);
CREATE INDEX IF NOT EXISTS design_jobs_family_scope_idx
    ON design_jobs(organization_id,design_group_id,family_id,updated_at DESC,id);

CREATE TABLE IF NOT EXISTS design_job_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES design_jobs(id),
    revision integer NOT NULL CHECK (revision >= 0),
    event_type text NOT NULL CHECK (
        event_type IN ('created','directory_recorded','transitioned')
    ),
    status text NOT NULL CHECK (
        status IN ('active','blocked','completed','cancelled','archived')
    ),
    phase text NOT NULL,
    provisioning_state text NOT NULL CHECK (
        provisioning_state IN ('provisioning','ready')
    ),
    directory_name text,
    blocked_reason jsonb,
    actor_id text NOT NULL REFERENCES actors(id),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id,revision)
);

CREATE INDEX IF NOT EXISTS design_job_events_job_idx
    ON design_job_events(job_id,revision);

CREATE OR REPLACE FUNCTION reject_design_job_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'design_job_events are append-only';
END;
$$;

DROP TRIGGER IF EXISTS design_job_events_append_only ON design_job_events;
CREATE TRIGGER design_job_events_append_only
    BEFORE UPDATE OR DELETE ON design_job_events
    FOR EACH ROW EXECUTE FUNCTION reject_design_job_event_mutation();
