# Changelog

## 0.3.0 - 2026-08-24

- Route every product operation through a controlled Design Job workspace;
  continue the same design in the same Job and create a new Job only for an
  explicitly independent requirement.
- Keep source snapshots, FCStd working copies, validation and delivery evidence,
  Product Family onboarding knowledge, and Design Lessons under their correct
  Job provenance without creating Git worktrees for product design.
- Add crash-safe Job lifecycle, repair diagnostics, legacy working-copy
  migration, PostgreSQL migrations `010` through `014`, and MCP/CLI Job tools.
- Preserve v0.2 compatibility wrappers for one documented transition window
  while all new writes use Job-aware contracts.
- Add macOS and Windows release documentation and opt-in Design Job FreeCAD
  acceptance coverage. New v0.3 live certification remains a release gate until
  the protected-platform runs are recorded.

## 0.2.0 - 2026-08-23

- Publish the project-level `AGENTS.md` operating and cross-platform guidance.
- Add the project-owned `freecad-standard-parts` and
  `freecad-model-validation` agent skills.
- Document Superpowers brainstorming as an optional external workflow without
  automatic installation or configuration.
- Add a looping six-model CAD showcase to the GitHub project homepage.

## 0.1.0 - 2026-08-22

- Initial public release of the deterministic mechanical-design MCP service,
  CLI, packaging, database resources, and validated FreeCAD integration
  boundary.
