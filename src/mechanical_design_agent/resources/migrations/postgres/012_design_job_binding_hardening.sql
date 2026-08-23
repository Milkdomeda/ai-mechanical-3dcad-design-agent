ALTER TABLE design_working_copies
    ADD COLUMN IF NOT EXISTS source_snapshot_id uuid;

ALTER TABLE design_working_copies
    ADD COLUMN IF NOT EXISTS bound_job_revision integer;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'design_job_source_snapshots_binding_scope_unique'
          AND conrelid = 'public.design_job_source_snapshots'::regclass
    ) THEN
        ALTER TABLE public.design_job_source_snapshots
            ADD CONSTRAINT design_job_source_snapshots_binding_scope_unique
            UNIQUE(id,job_id,organization_id,design_group_id);
    END IF;
END $$;

UPDATE public.design_working_copies AS working
SET source_snapshot_id = (
    SELECT snapshot.id
    FROM public.design_job_source_snapshots AS snapshot
    WHERE snapshot.job_id = working.job_id
      AND snapshot.organization_id = working.organization_id
      AND snapshot.design_group_id = working.design_group_id
      AND snapshot.source_model_revision_id = working.source_model_revision_id
      AND snapshot.sha256 = working.source_sha256
    ORDER BY abs(extract(epoch FROM (snapshot.created_at - working.created_at))), snapshot.id
    LIMIT 1
)
WHERE working.job_id IS NOT NULL
  AND working.design_origin = 'existing_model'
  AND working.source_snapshot_id IS NULL;

UPDATE public.design_working_copies AS working
SET bound_job_revision = event.revision - 1
FROM public.design_job_events AS event
WHERE working.job_id IS NOT NULL
  AND working.bound_job_revision IS NULL
  AND event.job_id = working.job_id
  AND event.event_type = 'working_copy_bound'
  AND event.reason = 'bound working copy ' || working.id::text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_snapshot_scope_fk'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_snapshot_scope_fk
            FOREIGN KEY (source_snapshot_id,job_id,organization_id,design_group_id)
            REFERENCES public.design_job_source_snapshots(
                id,job_id,organization_id,design_group_id
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conname = 'design_working_copies_governed_binding_check'
          AND conrelid = 'public.design_working_copies'::regclass
    ) THEN
        ALTER TABLE public.design_working_copies
            ADD CONSTRAINT design_working_copies_governed_binding_check CHECK (
                (
                    job_id IS NULL
                    AND source_snapshot_id IS NULL
                    AND bound_job_revision IS NULL
                )
                OR
                (
                    job_id IS NOT NULL
                    AND bound_job_revision IS NOT NULL
                    AND bound_job_revision >= 0
                    AND (
                        (
                            design_origin = 'existing_model'
                            AND source_snapshot_id IS NOT NULL
                            AND source_model_revision_id IS NOT NULL
                        )
                        OR
                        (
                            design_origin = 'new_design'
                            AND source_snapshot_id IS NULL
                            AND source_model_revision_id IS NULL
                        )
                    )
                )
            ) NOT VALID;
    END IF;
END $$;

ALTER TABLE public.design_working_copies
    VALIDATE CONSTRAINT design_working_copies_snapshot_scope_fk;
ALTER TABLE public.design_working_copies
    VALIDATE CONSTRAINT design_working_copies_governed_binding_check;

CREATE OR REPLACE FUNCTION public.reject_governed_working_copy_job_rebinding()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.job_id IS NOT NULL THEN
        IF OLD.job_id IS DISTINCT FROM NEW.job_id
           OR OLD.source_snapshot_id IS DISTINCT FROM NEW.source_snapshot_id
           OR OLD.bound_job_revision IS DISTINCT FROM NEW.bound_job_revision
           OR OLD.source_model_revision_id IS DISTINCT FROM NEW.source_model_revision_id
           OR OLD.source_sha256 IS DISTINCT FROM NEW.source_sha256
           OR OLD.design_origin IS DISTINCT FROM NEW.design_origin THEN
            RAISE EXCEPTION 'governed working-copy Job and source binding are immutable';
        END IF;
    ELSIF NEW.job_id IS NOT NULL AND coalesce(
        current_setting(
            'mechanical_design.allow_legacy_working_copy_binding',
            true
        ),
        'off'
    ) <> 'on' THEN
        RAISE EXCEPTION 'legacy working-copy Job binding requires the dedicated migration path';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS design_working_copies_immutable_job_binding
    ON public.design_working_copies;
CREATE TRIGGER design_working_copies_immutable_job_binding
    BEFORE UPDATE OF
        job_id,
        source_snapshot_id,
        bound_job_revision,
        source_model_revision_id,
        source_sha256,
        design_origin
    ON public.design_working_copies
    FOR EACH ROW
    EXECUTE FUNCTION public.reject_governed_working_copy_job_rebinding();
