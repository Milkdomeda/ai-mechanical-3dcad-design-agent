CREATE TABLE IF NOT EXISTS product_family_match_decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    job_id uuid,
    working_copy_id uuid,
    query_sha256 char(64) NOT NULL CHECK (query_sha256 ~ '^[0-9a-f]{64}$'),
    request_features jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (
        status IN ('authoritative_match','confirmation_required','unbound_no_match','conflict')
    ),
    binding_family_id text,
    candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    decision_source text NOT NULL CHECK (
        decision_source IN (
            'existing_job_binding',
            'source_model_binding',
            'explicit_family',
            'approved_product_identifier',
            'semantic_candidate',
            'no_match',
            'conflict'
        )
    ),
    specialized_knowledge_authorized boolean NOT NULL DEFAULT false,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT product_family_match_decisions_group_scope_fk
        FOREIGN KEY (design_group_id,organization_id)
        REFERENCES design_groups(id,organization_id),
    CONSTRAINT product_family_match_decisions_job_scope_fk
        FOREIGN KEY (job_id,organization_id,design_group_id)
        REFERENCES design_jobs(id,organization_id,design_group_id),
    CONSTRAINT product_family_match_decisions_working_scope_fk
        FOREIGN KEY (working_copy_id,job_id,organization_id,design_group_id)
        REFERENCES design_working_copies(id,job_id,organization_id,design_group_id),
    CONSTRAINT product_family_match_decisions_family_scope_fk
        FOREIGN KEY (binding_family_id,organization_id,design_group_id)
        REFERENCES product_families(id,organization_id,design_group_id),
    CONSTRAINT product_family_match_decisions_actor_scope_fk
        FOREIGN KEY (created_by,organization_id)
        REFERENCES actors(id,organization_id),
    CONSTRAINT product_family_match_decisions_working_job_check CHECK (
        working_copy_id IS NULL OR job_id IS NOT NULL
    ),
    CONSTRAINT product_family_match_decisions_authorization_check CHECK (
        specialized_knowledge_authorized = false
        OR (status = 'authoritative_match' AND binding_family_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS product_family_match_decisions_scope_idx
    ON product_family_match_decisions(
        organization_id,design_group_id,created_at,id
    );

CREATE INDEX IF NOT EXISTS product_family_match_decisions_job_idx
    ON product_family_match_decisions(job_id,created_at,id)
    WHERE job_id IS NOT NULL;

CREATE OR REPLACE FUNCTION reject_product_family_match_decision_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'product family match decisions are append-only';
END;
$$;

DROP TRIGGER IF EXISTS product_family_match_decisions_append_only
    ON product_family_match_decisions;
CREATE TRIGGER product_family_match_decisions_append_only
    BEFORE UPDATE OR DELETE ON product_family_match_decisions
    FOR EACH ROW EXECUTE FUNCTION reject_product_family_match_decision_mutation();
