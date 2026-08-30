# Changelog

## 0.6.0 - 2026-08-30

- Make the lightweight filesystem-backed design session the default workflow,
  with one natural-language approval followed by direct CAD mutation,
  validation, bounded automatic correction, and result recording.
- Add Chinese and English `APPROVE`, `REJECT`, and `UNCLEAR` approval semantics
  without requiring a fixed confirmation phrase.
- Keep Product Family Knowledge and Design Lessons as best-effort design inputs
  that do not create lifecycle gates when no match or backend is available.
- Move Design Jobs, Change Sets, Approval Envelopes, mutation authorization,
  engineering obligations, and PostgreSQL lifecycle persistence into the
  explicit `governed` compatibility profile.
- Preserve standard-parts, FreeCAD/CadQuery modeling, model validation,
  long-term knowledge storage, and existing governed APIs for compatibility.

## 0.5.0 - 2026-08-29

- Add adaptive Product Family, knowledge-retrieval, standard-parts, and
  assembly obligations whose conclusions are mandatory but whose order and
  depth remain proportional to the approved design scope.
- Add strict `EngineeringScope/v1` component plans, append-only scope-hash
  decisions, stale-scope invalidation, and mutation/delivery enforcement.
- Add `design`, `family-knowledge`, `maintenance`, and backward-compatible
  `all` MCP exposure profiles; the recommended ordinary-design profile exposes
  32 canonical tools without removing Service capabilities.
- Add `design_job_obligations_resolve` and an additive adaptive obligation read
  model to Design Job status.

- Simplify the default Design Lessons workflow to one engineer confirmation
  after the complete immutable Review Card is displayed.
- Add `design_lesson_review_publish` with automatic bounded
  projection/retrieval completion and no repeated approval after durable
  publication starts.
- Add an immutable `reviewed-no-publishable-lesson` decision path that records
  completed engineering screening without creating a Design Lesson or shared
  knowledge projection.
- Preserve existing Design Lesson approval, audit, recovery, supersession, and
  revocation tools as backward-compatible expert surfaces.
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
