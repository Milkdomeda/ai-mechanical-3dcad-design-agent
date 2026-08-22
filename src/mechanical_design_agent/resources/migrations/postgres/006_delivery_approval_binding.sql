ALTER TABLE design_working_copies
    ADD COLUMN IF NOT EXISTS approved_final_sha256 char(64);

ALTER TABLE design_working_copies
    ADD CONSTRAINT design_working_copies_delivery_sha_check
    CHECK (
        status <> 'approved_for_delivery'
        OR approved_final_sha256 IS NOT NULL
    ) NOT VALID;
