CREATE TABLE IF NOT EXISTS design_approval_envelopes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_change_set_id uuid NOT NULL UNIQUE REFERENCES design_change_sets(id),
    job_id uuid NOT NULL REFERENCES design_jobs(id),
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    design_intent jsonb NOT NULL,
    architecture jsonb NOT NULL,
    key_interfaces jsonb NOT NULL,
    user_constraints jsonb NOT NULL,
    manufacturing_method jsonb NOT NULL,
    material_constraints jsonb NOT NULL,
    validation_requirements jsonb NOT NULL,
    approved_by text NOT NULL REFERENCES actors(id),
    approval_text text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    approval_revision integer NOT NULL CHECK (approval_revision > 0),
    envelope_revision integer NOT NULL CHECK (envelope_revision > 0),
    superseded_by uuid REFERENCES design_approval_envelopes(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(working_copy_id, envelope_revision),
    FOREIGN KEY (working_copy_id, job_id, organization_id, design_group_id)
        REFERENCES design_working_copies(
            id, job_id, organization_id, design_group_id
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS design_approval_envelopes_one_active_idx
    ON design_approval_envelopes(working_copy_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS design_approval_envelopes_job_idx
    ON design_approval_envelopes(job_id, working_copy_id, envelope_revision DESC);

CREATE OR REPLACE FUNCTION protect_design_approval_envelope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'design approval envelopes cannot be deleted';
    END IF;
    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.approval_change_set_id IS DISTINCT FROM NEW.approval_change_set_id
       OR OLD.job_id IS DISTINCT FROM NEW.job_id
       OR OLD.working_copy_id IS DISTINCT FROM NEW.working_copy_id
       OR OLD.organization_id IS DISTINCT FROM NEW.organization_id
       OR OLD.design_group_id IS DISTINCT FROM NEW.design_group_id
       OR OLD.design_intent IS DISTINCT FROM NEW.design_intent
       OR OLD.architecture IS DISTINCT FROM NEW.architecture
       OR OLD.key_interfaces IS DISTINCT FROM NEW.key_interfaces
       OR OLD.user_constraints IS DISTINCT FROM NEW.user_constraints
       OR OLD.manufacturing_method IS DISTINCT FROM NEW.manufacturing_method
       OR OLD.material_constraints IS DISTINCT FROM NEW.material_constraints
       OR OLD.validation_requirements IS DISTINCT FROM NEW.validation_requirements
       OR OLD.approved_by IS DISTINCT FROM NEW.approved_by
       OR OLD.approval_text IS DISTINCT FROM NEW.approval_text
       OR OLD.approved_at IS DISTINCT FROM NEW.approved_at
       OR OLD.approval_revision IS DISTINCT FROM NEW.approval_revision
       OR OLD.envelope_revision IS DISTINCT FROM NEW.envelope_revision
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'approved design intent envelope content is immutable';
    END IF;
    IF OLD.status <> 'active' AND OLD.status IS DISTINCT FROM NEW.status THEN
        RAISE EXCEPTION 'inactive design approval envelope status is immutable';
    END IF;
    IF OLD.status = 'active'
       AND NEW.status NOT IN ('active', 'superseded', 'revoked') THEN
        RAISE EXCEPTION 'invalid design approval envelope status transition';
    END IF;
    IF OLD.superseded_by IS NOT NULL
       AND OLD.superseded_by IS DISTINCT FROM NEW.superseded_by THEN
        RAISE EXCEPTION 'design approval envelope successor is immutable';
    END IF;
    IF NEW.superseded_by IS NOT NULL AND NEW.status <> 'superseded' THEN
        RAISE EXCEPTION 'only a superseded envelope can name a successor';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS design_approval_envelopes_immutable
    ON design_approval_envelopes;
CREATE TRIGGER design_approval_envelopes_immutable
BEFORE UPDATE OR DELETE ON design_approval_envelopes
FOR EACH ROW EXECUTE FUNCTION protect_design_approval_envelope();

ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS approval_envelope_id uuid
        REFERENCES design_approval_envelopes(id);
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS approval_envelope_draft jsonb;
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS semantic_impact jsonb;
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS boundary_decision jsonb;
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS authorization_mode text NOT NULL DEFAULT 'human_required';
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS requires_human_approval boolean NOT NULL DEFAULT true;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_change_sets_authorization_mode_check'
    ) THEN
        ALTER TABLE design_change_sets
            ADD CONSTRAINT design_change_sets_authorization_mode_check
            CHECK (
                authorization_mode IN (
                    'human_required',
                    'human_approval',
                    'approval_envelope'
                )
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS design_change_audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    change_set_id uuid NOT NULL REFERENCES design_change_sets(id),
    approval_envelope_id uuid REFERENCES design_approval_envelopes(id),
    event_type text NOT NULL CHECK (
        event_type IN (
            'human_approval_required',
            'human_approved',
            'human_rejected',
            'autonomous_authorized',
            'boundary_fail_closed',
            'mutation_authorized',
            'change_applied',
            'envelope_superseded',
            'envelope_revoked'
        )
    ),
    actor_id text NOT NULL REFERENCES actors(id),
    decision jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS design_change_audit_events_change_idx
    ON design_change_audit_events(change_set_id, created_at, id);
CREATE INDEX IF NOT EXISTS design_change_audit_events_envelope_idx
    ON design_change_audit_events(approval_envelope_id, created_at, id);

CREATE OR REPLACE FUNCTION reject_design_change_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'design change audit events are append-only';
END;
$$;

DROP TRIGGER IF EXISTS design_change_audit_events_append_only
    ON design_change_audit_events;
CREATE TRIGGER design_change_audit_events_append_only
BEFORE UPDATE OR DELETE ON design_change_audit_events
FOR EACH ROW EXECUTE FUNCTION reject_design_change_audit_event_mutation();
