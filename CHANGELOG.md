# Changelog

## 0.7.1 - 2026-09-01

- Store immutable Design Lesson review cards under model-SHA-addressed paths so evidence from an earlier model revision cannot block a confirmed newer revision.
- Keep candidate validation ahead of card publication: `candidate_errors` creates no formal review card, while corrected candidates for the same model may proceed to `review_pending`.
- Preserve state-bound legacy `lesson-review/review.json` cards unchanged and compatible with existing publication decisions.
- Require semantic expansion to authoritative manufacturer, standards-body, industry-association, or attributable authorized-distributor sources when configured structured standard-part providers miss a component that is reasonably likely to exist commercially.
- Treat a structured zero result only as a structured-search miss; allow a final not-found report only after a reasonable authoritative-source search, with complete query and provenance records.

## 0.7.0 - 2026-08-31

- Establish one normal design process from requirements and direction approval through knowledge retrieval, CAD modeling, exact-model validation, final confirmation, and automatic Design Lesson evaluation.
- Accept Chinese and English confirmation by meaning as `APPROVE`, `REJECT`, or `UNCLEAR`; no fixed confirmation phrase is required.
- Keep completed model state independent from lesson evaluation and publication. Database or graph availability cannot invalidate a completed CAD result.
- Add immutable Design Lesson review cards and one explicit decision before durable publication.
- Keep individual design-session state in portable filesystem JSON and use PostgreSQL/pgvector plus Neo4j only for durable Product Family Knowledge and Design Lessons.
- Replace the previous process APIs, persistence schema, tests, documentation, and project skill with the version 0.7.0 contract. Existing databases from earlier releases require a fresh knowledge database.
- Preserve FreeCAD/CadQuery modeling, standard-part provenance, model validation, Product Family Knowledge, Design Lessons, macOS support, and Windows support.

## Earlier releases

Earlier release notes are available from their corresponding Git tags.
