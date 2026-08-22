CREATE INDEX IF NOT EXISTS design_lesson_assertions_assertion_id_idx
    ON design_lesson_assertions(assertion_id, lesson_event_id);

CREATE INDEX IF NOT EXISTS design_lesson_lineage_idx
    ON design_lesson_events(organization_id, lesson_key, revision DESC);

CREATE TABLE IF NOT EXISTS design_lesson_evidence_artifacts (
    lesson_event_id uuid NOT NULL REFERENCES design_lesson_events(id) ON DELETE CASCADE,
    evidence_id text NOT NULL,
    evidence_role text NOT NULL CHECK (evidence_role IN (
        'source_before_model',
        'source_after_model',
        'geometry_validation',
        'assembly_completeness_validation',
        'fastener_interface_validation',
        'mechanical_interface_validation',
        'review_image',
        'supporting_report'
    )),
    artifact_sha256 char(64) NOT NULL,
    artifact_storage_path text NOT NULL,
    artifact_source_path text NOT NULL,
    media_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(lesson_event_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS design_lesson_evidence_sha_idx
    ON design_lesson_evidence_artifacts(artifact_sha256);

CREATE TABLE IF NOT EXISTS design_lesson_report_bindings (
    lesson_event_id uuid NOT NULL,
    evidence_id text NOT NULL,
    validation_report_id uuid NOT NULL REFERENCES validation_reports(id),
    validation_kind text NOT NULL,
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id),
    change_set_id uuid NOT NULL REFERENCES design_change_sets(id),
    working_sha256 char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(lesson_event_id, evidence_id),
    FOREIGN KEY(lesson_event_id, evidence_id)
        REFERENCES design_lesson_evidence_artifacts(lesson_event_id, evidence_id)
        ON DELETE CASCADE
);

ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS aggregate_version bigint;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY aggregate_type, aggregate_id
               ORDER BY created_at, id
           ) AS version
    FROM outbox_events
)
UPDATE outbox_events AS event
SET aggregate_version = ranked.version
FROM ranked
WHERE event.id = ranked.id
  AND event.aggregate_version IS NULL;

ALTER TABLE outbox_events ALTER COLUMN aggregate_version SET NOT NULL;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_by text;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS outbox_aggregate_version_idx
    ON outbox_events(aggregate_type, aggregate_id, aggregate_version);

CREATE INDEX IF NOT EXISTS outbox_claimable_idx
    ON outbox_events(processed_at, claimed_at, created_at, id);
