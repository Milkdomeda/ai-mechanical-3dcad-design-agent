ALTER TABLE design_working_copies
    DROP CONSTRAINT IF EXISTS design_working_copies_delivery_artifact_check;

ALTER TABLE design_lesson_reviews
    DROP CONSTRAINT IF EXISTS design_lesson_reviews_approved_artifact_check;
