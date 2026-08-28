# Changelog

## 0.4.0 - 2026-08-28

- Accept the canonical `FreeCAD Document` archive comment emitted by official
  FreeCAD FCStd files while continuing to reject every other non-empty ZIP
  comment.
- Run FreeCADCmd scripts through the non-interactive console command path and
  remove only strictly recognized complete official progress blocks; unknown
  stdout and stderr remain evidence and continue to fail closed.
- Replace per-iteration Design Change approval with a PostgreSQL-authoritative
  Design Intent Approval Envelope, semantic boundary classification,
  pre-mutation authorization, simple `批准` / `修改方案` interaction, and
  append-only audit history for autonomous in-envelope repairs.
- Make Product Family association optional for ordinary Design Jobs and route
  non-family operations through family-neutral runtime settings.
- Add PostgreSQL-authoritative family inventory and deterministic, append-only
  request matching without exposing specialized family knowledge before an
  authoritative binding or user confirmation.
- Label workspace family listing as bootstrap configuration only and allow new
  workspaces to establish organization/design-group scope without creating a
  Product Family.
- Harden Windows release verification with deterministic synthetic FCStd ZIP
  metadata and semantic validation of escaped FreeCADCmd script paths and
  arguments, without weakening process, stdout, stderr, or release gates.

## 0.3.1 - 2026-08-24

- Normalize Windows managed workspace paths before ownership, containment, and
  locked working-copy comparisons so ordinary and `\\?\` extended spellings
  refer to the same Design Job artifacts.
- Preserve canonical Windows paths when isolating FreeCADCmd subprocesses and
  validate equivalent paths by managed file identity.
- Keep the v0.3.0 Design Job, knowledge, validation, and packaging contracts
  unchanged; this release is a cross-platform correctness patch.

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
