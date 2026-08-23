ALTER TABLE public.design_working_copies
    ADD COLUMN IF NOT EXISTS working_sha256 char(64);
ALTER TABLE public.design_working_copies
    ADD COLUMN IF NOT EXISTS working_size_bytes bigint;
ALTER TABLE public.design_working_copies
    ADD COLUMN IF NOT EXISTS working_relative_path text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_jobs_id_scope_unique_v2'
          AND conrelid = 'public.design_jobs'::regclass
    ) THEN
        ALTER TABLE public.design_jobs
            ADD CONSTRAINT design_jobs_id_scope_unique_v2
            UNIQUE(id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'model_revisions_id_scope_unique_v2'
          AND conrelid = 'public.model_revisions'::regclass
    ) THEN
        ALTER TABLE public.model_revisions
            ADD CONSTRAINT model_revisions_id_scope_unique_v2
            UNIQUE(id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_id_job_scope_unique_v2'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_id_job_scope_unique_v2
            UNIQUE(id,job_id,organization_id,design_group_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_job_scope_fk_v2'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_job_scope_fk_v2
            FOREIGN KEY (job_id,organization_id,design_group_id)
            REFERENCES public.design_jobs(id,organization_id,design_group_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_model_scope_fk_v2'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_model_scope_fk_v2
            FOREIGN KEY (source_model_revision_id,organization_id,design_group_id)
            REFERENCES public.model_revisions(id,organization_id,design_group_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_family_scope_fk_v2'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_family_scope_fk_v2
            FOREIGN KEY (family_id,organization_id,design_group_id)
            REFERENCES public.product_families(id,organization_id,design_group_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_creator_scope_fk_v2'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_creator_scope_fk_v2
            FOREIGN KEY (created_by,organization_id)
            REFERENCES public.actors(id,organization_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_jobs_active_working_copy_scope_fk_v2'
          AND conrelid = 'public.design_jobs'::regclass
    ) THEN
        ALTER TABLE public.design_jobs
            ADD CONSTRAINT design_jobs_active_working_copy_scope_fk_v2
            FOREIGN KEY (active_working_copy_id,id,organization_id,design_group_id)
            REFERENCES public.design_working_copies(id,job_id,organization_id,design_group_id)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_governed_evidence_check_v3'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_governed_evidence_check_v3 CHECK (
                job_id IS NULL OR (
                    working_sha256 ~ '^[0-9a-f]{64}$'
                    AND working_size_bytes > 0
                    AND working_relative_path ~ '^models/working/[^/]+/[^/]+[.]FCStd$'
                    AND working_relative_path !~ '(^|/)[.][.]?(/|$)'
                )
            ) NOT VALID;
    END IF;
END $$;

-- These repaired scoped relationships are authoritative. Refuse the upgrade
-- with PostgreSQL's relation/constraint diagnostic if earlier rows violate one.
ALTER TABLE public.design_working_copies
    VALIDATE CONSTRAINT design_working_copies_job_scope_fk_v2;
ALTER TABLE public.design_working_copies
    VALIDATE CONSTRAINT design_working_copies_model_scope_fk_v2;
ALTER TABLE public.design_working_copies
    VALIDATE CONSTRAINT design_working_copies_family_scope_fk_v2;
ALTER TABLE public.design_working_copies
    VALIDATE CONSTRAINT design_working_copies_creator_scope_fk_v2;
ALTER TABLE public.design_jobs
    VALIDATE CONSTRAINT design_jobs_active_working_copy_scope_fk_v2;

CREATE OR REPLACE FUNCTION public.reject_governed_working_copy_job_rebinding()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.job_id IS NULL THEN
        IF OLD.id IS DISTINCT FROM NEW.id
           OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
           OR OLD.design_group_id IS DISTINCT FROM NEW.design_group_id
           OR OLD.job_id IS DISTINCT FROM NEW.job_id
           OR OLD.source_snapshot_id IS DISTINCT FROM NEW.source_snapshot_id
           OR OLD.bound_job_revision IS DISTINCT FROM NEW.bound_job_revision
           OR OLD.source_model_revision_id IS DISTINCT FROM NEW.source_model_revision_id
           OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256
           OR OLD.source_kind IS DISTINCT FROM NEW.source_kind
           OR OLD.design_origin IS DISTINCT FROM NEW.design_origin
           OR OLD.family_id IS DISTINCT FROM NEW.family_id
           OR OLD.working_path IS DISTINCT FROM NEW.working_path
           OR OLD.created_by IS DISTINCT FROM NEW.created_by
           OR OLD.working_sha256 IS DISTINCT FROM NEW.working_sha256
           OR OLD.working_size_bytes IS DISTINCT FROM NEW.working_size_bytes
           OR OLD.working_relative_path IS DISTINCT FROM NEW.working_relative_path
           OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'legacy working-copy Job binding is immutable';
        END IF;
    ELSE
        IF OLD.id IS DISTINCT FROM NEW.id
           OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
           OR OLD.design_group_id IS DISTINCT FROM NEW.design_group_id
           OR OLD.job_id IS DISTINCT FROM NEW.job_id
           OR OLD.source_snapshot_id IS DISTINCT FROM NEW.source_snapshot_id
           OR OLD.bound_job_revision IS DISTINCT FROM NEW.bound_job_revision
           OR OLD.source_model_revision_id IS DISTINCT FROM NEW.source_model_revision_id
           OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256
           OR OLD.source_kind IS DISTINCT FROM NEW.source_kind
           OR OLD.design_origin IS DISTINCT FROM NEW.design_origin
           OR OLD.family_id IS DISTINCT FROM NEW.family_id
           OR OLD.working_path IS DISTINCT FROM NEW.working_path
           OR OLD.created_by IS DISTINCT FROM NEW.created_by
           OR OLD.working_sha256 IS DISTINCT FROM NEW.working_sha256
           OR OLD.working_size_bytes IS DISTINCT FROM NEW.working_size_bytes
           OR OLD.working_relative_path IS DISTINCT FROM NEW.working_relative_path
           OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION 'governed working-copy provenance is immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS design_working_copies_immutable_job_binding
    ON public.design_working_copies;
CREATE TRIGGER design_working_copies_immutable_job_binding
    BEFORE UPDATE OF
        id,
        organization_id,
        design_group_id,
        job_id,
        source_snapshot_id,
        bound_job_revision,
        source_model_revision_id,
        source_sha256,
        source_kind,
        design_origin,
        family_id,
        working_path,
        created_by,
        working_sha256,
        working_size_bytes,
        working_relative_path,
        created_at
    ON public.design_working_copies
    FOR EACH ROW
    EXECUTE FUNCTION public.reject_governed_working_copy_job_rebinding();
