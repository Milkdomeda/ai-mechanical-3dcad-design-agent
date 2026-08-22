ALTER TABLE design_working_copies
    ADD COLUMN IF NOT EXISTS approved_final_artifact_path text;

ALTER TABLE design_working_copies
    ADD CONSTRAINT design_working_copies_delivery_artifact_check
    CHECK (
        status <> 'approved_for_delivery'
        OR approved_final_artifact_path IS NOT NULL
    ) NOT VALID;

ALTER TABLE design_lesson_reviews
    ADD COLUMN IF NOT EXISTS approved_final_artifact_path text;

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_approved_artifact_check
    CHECK (approved_final_artifact_path IS NOT NULL) NOT VALID;
