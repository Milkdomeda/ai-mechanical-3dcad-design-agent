CREATE TABLE IF NOT EXISTS design_job_obligation_decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    job_id uuid NOT NULL,
    working_copy_id uuid,
    obligation_kind text NOT NULL CHECK (
        obligation_kind IN ('standard_parts_assessment','assembly_assessment')
    ),
    outcome text NOT NULL,
    resolution_level text NOT NULL CHECK (
        resolution_level IN ('screening','expanded')
    ),
    rationale text NOT NULL CHECK (btrim(rationale) <> ''),
    engineering_scope jsonb NOT NULL,
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    supersedes_decision_id uuid REFERENCES design_job_obligation_decisions(id),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT design_job_obligation_decisions_outcome_check CHECK (
        (obligation_kind = 'standard_parts_assessment' AND outcome IN (
            'not_applicable','no_candidates','candidates_resolved',
            'approved_custom_exception'
        ))
        OR
        (obligation_kind = 'assembly_assessment' AND outcome IN (
            'not_applicable','required_pending','required_passed'
        ))
    ),
    CONSTRAINT design_job_obligation_decisions_group_scope_fk
        FOREIGN KEY (design_group_id,organization_id)
        REFERENCES design_groups(id,organization_id),
    CONSTRAINT design_job_obligation_decisions_job_scope_fk
        FOREIGN KEY (job_id,organization_id,design_group_id)
        REFERENCES design_jobs(id,organization_id,design_group_id),
    CONSTRAINT design_job_obligation_decisions_working_scope_fk
        FOREIGN KEY (working_copy_id,job_id,organization_id,design_group_id)
        REFERENCES design_working_copies(id,job_id,organization_id,design_group_id),
    CONSTRAINT design_job_obligation_decisions_actor_scope_fk
        FOREIGN KEY (created_by,organization_id)
        REFERENCES actors(id,organization_id),
    CONSTRAINT design_job_obligation_decisions_scope_shape_check CHECK (
        jsonb_typeof(engineering_scope) = 'object'
        AND engineering_scope->>'schema_version' = 'EngineeringScope/v1'
        AND jsonb_typeof(evidence_refs) = 'array'
    )
);

CREATE INDEX IF NOT EXISTS design_job_obligation_decisions_latest_idx
    ON design_job_obligation_decisions(
        job_id,working_copy_id,obligation_kind,created_at DESC,id DESC
    );

CREATE UNIQUE INDEX IF NOT EXISTS design_job_obligation_decisions_successor_idx
    ON design_job_obligation_decisions(supersedes_decision_id)
    WHERE supersedes_decision_id IS NOT NULL;

CREATE OR REPLACE FUNCTION reject_design_job_obligation_decision_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'design job obligation decisions are append-only';
END;
$$;

DROP TRIGGER IF EXISTS design_job_obligation_decisions_append_only
    ON design_job_obligation_decisions;
CREATE TRIGGER design_job_obligation_decisions_append_only
    BEFORE UPDATE OR DELETE ON design_job_obligation_decisions
    FOR EACH ROW EXECUTE FUNCTION reject_design_job_obligation_decision_mutation();
