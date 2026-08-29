ALTER TABLE design_lesson_reviews
    ADD COLUMN IF NOT EXISTS review_outcome text;

UPDATE design_lesson_reviews
SET review_outcome = 'publish'
WHERE review_outcome IS NULL;

ALTER TABLE design_lesson_reviews
    ALTER COLUMN review_outcome SET DEFAULT 'publish';
ALTER TABLE design_lesson_reviews
    ALTER COLUMN review_outcome SET NOT NULL;

ALTER TABLE design_lesson_reviews
    ADD COLUMN IF NOT EXISTS confirmation_mode text;

UPDATE design_lesson_reviews
SET confirmation_mode = 'legacy_review_id'
WHERE confirmation_mode IS NULL;

ALTER TABLE design_lesson_reviews
    ALTER COLUMN confirmation_mode SET DEFAULT 'legacy_review_id';
ALTER TABLE design_lesson_reviews
    ALTER COLUMN confirmation_mode SET NOT NULL;

ALTER TABLE design_lesson_reviews
    ADD COLUMN IF NOT EXISTS decision_receipt_sha256 char(64);
ALTER TABLE design_lesson_reviews
    ADD COLUMN IF NOT EXISTS decision_receipt_path text;

ALTER TABLE design_lesson_reviews
    ALTER COLUMN lesson_id DROP NOT NULL;

ALTER TABLE design_lesson_reviews
    DROP CONSTRAINT IF EXISTS design_lesson_reviews_status_check;
ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_status_check CHECK (status IN (
        'awaiting-engineer-review','superseded','rejected','invalid',
        'approved-retrieval-pending','stored-and-retrievable',
        'reviewed-no-publishable-lesson'
    ));

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_outcome_check
    CHECK (review_outcome IN ('publish','no_publish'));

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_confirmation_mode_check
    CHECK (confirmation_mode IN ('legacy_review_id','single_confirmation'));

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_lesson_outcome_check
    CHECK (
        (review_outcome = 'publish' AND lesson_id IS NOT NULL)
        OR (review_outcome = 'no_publish' AND lesson_id IS NULL)
    );

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_no_publication_check
    CHECK (
        (review_outcome = 'publish'
            AND status <> 'reviewed-no-publishable-lesson')
        OR
        (review_outcome = 'no_publish'
            AND status NOT IN ('approved-retrieval-pending','stored-and-retrievable')
            AND published_design_lesson_id IS NULL)
    );

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_decision_receipt_check
    CHECK (
        (decision_receipt_sha256 IS NULL AND decision_receipt_path IS NULL)
        OR
        (decision_receipt_sha256 ~ '^[0-9a-f]{64}$'
            AND decision_receipt_path IS NOT NULL
            AND btrim(decision_receipt_path) <> '')
    );

ALTER TABLE design_lesson_reviews
    ADD CONSTRAINT design_lesson_reviews_single_confirmation_receipt_check
    CHECK (
        confirmation_mode <> 'single_confirmation'
        OR status IN ('awaiting-engineer-review','superseded','rejected','invalid')
        OR decision_receipt_sha256 IS NOT NULL
    );
