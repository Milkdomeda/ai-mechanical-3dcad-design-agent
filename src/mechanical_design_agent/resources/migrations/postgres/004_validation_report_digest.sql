ALTER TABLE validation_reports
    ADD COLUMN IF NOT EXISTS report_sha256 char(64);
