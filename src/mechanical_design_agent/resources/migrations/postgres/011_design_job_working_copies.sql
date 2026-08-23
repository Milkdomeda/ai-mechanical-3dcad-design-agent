ALTER TABLE design_working_copies
    ADD COLUMN IF NOT EXISTS job_id uuid;

ALTER TABLE design_jobs
    ADD COLUMN IF NOT EXISTS active_working_copy_id uuid;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_jobs_id_scope_unique'
    ) THEN
        ALTER TABLE design_jobs
            ADD CONSTRAINT design_jobs_id_scope_unique
            UNIQUE(id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'model_revisions_id_scope_unique'
    ) THEN
        ALTER TABLE model_revisions
            ADD CONSTRAINT model_revisions_id_scope_unique
            UNIQUE(id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_working_copies_id_job_scope_unique'
    ) THEN
        ALTER TABLE design_working_copies
            ADD CONSTRAINT design_working_copies_id_job_scope_unique
            UNIQUE(id,job_id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_working_copies_job_scope_fk'
    ) THEN
        ALTER TABLE design_working_copies
            ADD CONSTRAINT design_working_copies_job_scope_fk
            FOREIGN KEY (job_id,organization_id,design_group_id)
            REFERENCES design_jobs(id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_working_copies_model_scope_fk'
    ) THEN
        ALTER TABLE design_working_copies
            ADD CONSTRAINT design_working_copies_model_scope_fk
            FOREIGN KEY (source_model_revision_id,organization_id,design_group_id)
            REFERENCES model_revisions(id,organization_id,design_group_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_working_copies_family_scope_fk'
    ) THEN
        ALTER TABLE design_working_copies
            ADD CONSTRAINT design_working_copies_family_scope_fk
            FOREIGN KEY (family_id,organization_id,design_group_id)
            REFERENCES product_families(id,organization_id,design_group_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_working_copies_creator_scope_fk'
    ) THEN
        ALTER TABLE design_working_copies
            ADD CONSTRAINT design_working_copies_creator_scope_fk
            FOREIGN KEY (created_by,organization_id)
            REFERENCES actors(id,organization_id)
            NOT VALID;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS design_working_copies_job_idx
    ON design_working_copies(job_id,created_at,id)
    WHERE job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS design_job_source_snapshots (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    source_model_revision_id uuid,
    source_filename text NOT NULL CHECK (
        btrim(source_filename) <> ''
        AND source_filename !~ '[\\/]'
    ),
    stored_path text NOT NULL CHECK (
        stored_path ~ '^inputs/source/[^/]+/[^/]+$'
        AND stored_path !~ '\\'
    ),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    source_kind text NOT NULL CHECK (
        source_kind IN ('existing_model','product_family_input')
    ),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT design_job_source_snapshots_job_scope_fk
        FOREIGN KEY (job_id,organization_id,design_group_id)
        REFERENCES design_jobs(id,organization_id,design_group_id),
    CONSTRAINT design_job_source_snapshots_model_scope_fk
        FOREIGN KEY (source_model_revision_id,organization_id,design_group_id)
        REFERENCES model_revisions(id,organization_id,design_group_id),
    CONSTRAINT design_job_source_snapshots_creator_scope_fk
        FOREIGN KEY (created_by,organization_id)
        REFERENCES actors(id,organization_id),
    UNIQUE(job_id,stored_path),
    UNIQUE(job_id,id)
);

CREATE INDEX IF NOT EXISTS design_job_source_snapshots_job_idx
    ON design_job_source_snapshots(job_id,created_at,id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_jobs_active_working_copy_scope_fk'
    ) THEN
        ALTER TABLE design_jobs
            ADD CONSTRAINT design_jobs_active_working_copy_scope_fk
            FOREIGN KEY (active_working_copy_id,id,organization_id,design_group_id)
            REFERENCES design_working_copies(id,job_id,organization_id,design_group_id);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION reject_legacy_null_job_working_copy_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.job_id IS NULL)
       OR (TG_OP = 'UPDATE' AND OLD.job_id IS NOT NULL AND NEW.job_id IS NULL) THEN
        RAISE EXCEPTION 'new governed design working copies require job_id';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS design_working_copies_require_job ON design_working_copies;
CREATE TRIGGER design_working_copies_require_job
    BEFORE INSERT OR UPDATE OF job_id ON design_working_copies
    FOR EACH ROW EXECUTE FUNCTION reject_legacy_null_job_working_copy_insert();

CREATE OR REPLACE FUNCTION reject_design_job_source_snapshot_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'design_job_source_snapshots are append-only';
END;
$$;

DROP TRIGGER IF EXISTS design_job_source_snapshots_append_only
    ON design_job_source_snapshots;
CREATE TRIGGER design_job_source_snapshots_append_only
    BEFORE UPDATE OR DELETE ON design_job_source_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_design_job_source_snapshot_mutation();

ALTER TABLE design_job_events
    DROP CONSTRAINT IF EXISTS design_job_events_event_type_check;
ALTER TABLE design_job_events
    ADD CONSTRAINT design_job_events_event_type_check CHECK (
        event_type IN (
            'created','directory_recorded','transitioned','working_copy_bound'
        )
    );
