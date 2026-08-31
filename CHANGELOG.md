# Changelog

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
