# Adaptive Engineering Obligations and MCP Tool Profiles Implementation Plan

**Target:** v0.5.0

**Branch:** `codex/mcp-tool-profiles-v0.5.0`

## Release constraints

- Preserve every v0.4.1 Service/repository capability in the `all` profile.
- Do not model obligations as a fixed sequence.
- Do not weaken retrieval, approval-envelope, validation, delivery, or Design
  Lesson gates.
- Do not tag or publish until the feature branch is merged into `main` and the
  combined release checks pass.

## Task 1: Add obligation domain validation

Files:

- `src/mechanical_design_agent/engineering_obligations.py`
- `tests/test_engineering_obligations.py`

Implement and test:

- canonical `EngineeringScope/v1` validation and SHA-256;
- component-plan sourcing classes;
- deterministic standard-part and assembly expansion rules;
- outcome/kind consistency;
- `not_applicable` rejection on contradictory triggers;
- read-model rendering with multiple allowed actions and no sequence.

## Task 2: Add PostgreSQL migration 018

Files:

- `src/mechanical_design_agent/resources/migrations/postgres/018_design_job_obligations.sql`
- migration manifests and tests

Create append-only scoped obligation decisions with immutable content,
scope-hash binding, evidence JSON, predecessor reference, and outcome checks.
Update all package/database/Windows migration inventories.

## Task 3: Add repository obligation authority

Files:

- `src/mechanical_design_agent/repository.py`
- focused repository tests

Add methods to:

- append validated standard-part and assembly decisions;
- read latest decisions for one Job/working copy and exact scope;
- read latest Product Family match decision for the Job;
- cross-check knowledge receipts, registered standard parts, and assembly
  validation evidence;
- reject stale scope hashes and unauthorized actors.

## Task 4: Add adaptive Service operations

Files:

- `src/mechanical_design_agent/service.py`
- `src/mechanical_design_agent/jobs.py` only if the Job response adapter needs
  a focused extension
- service tests

Add:

- `design_job_obligations_resolve` batch operation;
- an additive `engineering_obligations` section in `design_job_get`;
- family and knowledge conclusions derived from existing receipts;
- standard/assembly conclusions derived from exact scope-bound decisions;
- recommended, allowed, and blocked action sets rather than one global next
  step.

## Task 5: Enforce applicable obligation gates

Files:

- `src/mechanical_design_agent/design.py`
- `src/mechanical_design_agent/repository.py`
- lifecycle tests

Require the first Design Intent to carry `engineering_scope`. Before mutation:

- family resolution must be terminal;
- retrieval must be completed;
- standard parts must be resolved for the exact scope;
- assembly must be `not_applicable`, or `required_pending/required_passed`.

Before delivery, an expanded assembly must have same-revision passing evidence.
Scope drift reopens affected decisions. Legacy expert calls remain reachable in
`all`, but they cannot bypass existing safety gates.

## Task 6: Introduce MCP exposure policy

Files:

- `src/mechanical_design_agent/tool_profiles.py`
- `src/mechanical_design_agent/server.py`
- profile tests

Implement centrally validated profiles:

- `design`
- `family-knowledge`
- `maintenance`
- `all`

Use explicit `create_mcp(tool_profile=...)` or
`MECH_DESIGN_MCP_TOOL_PROFILE`. Unset remains `all`; unknown values fail closed.
Do not mutate FastMCP internals.

## Task 7: Define and prove exact inventories

Files:

- `tests/test_tool_profiles.py`
- `tests/test_public_distribution.py`
- bootstrap/server tests

Acceptance:

- `design` contains no more than 32 tools;
- it includes Job routing, family matching, Job working copies, obligations,
  retrieval, change gates, validation, standard parts, delivery, and the five
  default Lesson tools;
- legacy working-copy, ingestion, raw Lesson staging, catalog mutation, audit,
  and projection tools are absent;
- `all` contains the complete old inventory plus the new obligation tool;
- schemas of hidden tools are absent from `tools/list`.

## Task 8: Update Agent routing and public documentation

Files:

- `AGENTS.md`
- `.agents/skills/mechanical-design-job-workspace/SKILL.md`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DESIGN_JOB_WORKSPACES.md`
- tests for public instructions

Document obligation closure, adaptive depth, invalidation, Profile selection,
simple-part behavior, complex-assembly behavior, and the compatibility window.

## Task 9: Set version 0.5.0

Update the package source of truth and every governed generated/audited version
surface. Mark the changelog entry `Unreleased` until final publication.

## Task 10: Validate and review the feature branch

Run:

- focused obligation/profile/migration/lifecycle tests;
- complete supported offline suite;
- live PostgreSQL/Neo4j tests when available;
- FreeCAD and Windows boundary tests where available;
- wheel and sdist builds;
- clean installed-wheel and MCP inventory checks;
- path, secret, private-asset, and public allowlist scans;
- final diff and code review.

## Task 11: Merge and publish once

After all gates pass:

1. merge the feature branch into `main`;
2. rerun the complete combined release suite;
3. finalize the v0.5.0 changelog date;
4. commit release metadata;
5. push `main`;
6. create and push annotated tag `v0.5.0`;
7. publish the GitHub release with verified wheel/sdist artifacts;
8. report any unavailable protected-host acceptance honestly.
