# AI Mechanical Design Experimentation Workflow Implementation Plan

**Design:** `docs/superpowers/specs/2026-08-30-ai-mechanical-design-experimentation-workflow-design.md`

**Branch:** `codex/lightweight-design-workflow`

## Objective

Make the default mechanical-design MCP workflow a filesystem-backed, single-approval experimentation path that can create and complete FreeCAD designs without PostgreSQL. Preserve knowledge, Design Lessons, Product Families, standard parts, validation, and the existing governed lifecycle behind explicit profiles.

## Task 1: Approval semantics

Files:

- add `src/mechanical_design_agent/approval_semantics.py`;
- add `tests/test_approval_semantics.py`.

Work:

1. Write failing parameterized tests for required Chinese and English approval/rejection phrases.
2. Add tests for Unicode normalization, punctuation, whitespace, negation, contradictions, conditionals, unknown text, and substring false positives.
3. Implement a dependency-free `classify_approval(text) -> str` returning `APPROVE`, `REJECT`, or `UNCLEAR`.
4. Run `pytest tests/test_approval_semantics.py`.

## Task 2: Lightweight settings and session storage

Files:

- modify `src/mechanical_design_agent/config.py`;
- modify `src/mechanical_design_agent/bootstrap_runtime.py`;
- add `src/mechanical_design_agent/lightweight_design.py`;
- add `tests/test_lightweight_design.py`.

Work:

1. Add tests for session creation, non-mutating reject/unclear results, idempotent resume, incompatible collisions, safe IDs, path containment, symlink rejection, UTF-8, and spaces.
2. Define `LightweightDesignSettings` without database or Neo4j fields.
3. Add a bootstrap resolver that requires only workspace, artifact/design root, actor identity if available, and reviewed FreeCADCmd identity.
4. Implement `LightweightDesignService.start` using a pluggable seed creator in unit tests and the packaged neutral seed script in production.
5. Add existing-model source snapshot and normalization support while preserving the source.
6. Implement atomic `design.json` writes using a same-directory temporary file, sync, replacement, and a workspace-owned lock.
7. Run `pytest tests/test_lightweight_design.py tests/test_workspace_bootstrap.py tests/test_freecad_runner.py`.

## Task 3: Result recording and exact-hash validation

Files:

- extend `src/mechanical_design_agent/lightweight_design.py`;
- extend `tests/test_lightweight_design.py`.

Work:

1. Add failing tests for passed exact-hash reports, failed reports, stale hashes, malformed reports, missing artifacts, and path escapes.
2. Implement `record_result` with direct SHA-256 calculation and strict validation-result contract checks.
3. Set `completed` only for a passed same-revision report with JSON, Markdown, and PNG evidence; set `needs_attention` otherwise.
4. Verify correction behavior invalidates the prior report when the FCStd hash changes.

## Task 4: Optional knowledge adapter

Files:

- add `src/mechanical_design_agent/lightweight_knowledge.py`;
- add `tests/test_lightweight_knowledge.py`;
- extend `src/mechanical_design_agent/lightweight_design.py` only for local snapshot updates.

Work:

1. Test match, no-match, optional-backend-unavailable, required-backend-unavailable, and redacted-family behavior through injected context builders.
2. Implement a small adapter that calls the existing context builder only when requested.
3. Store only retrieval state, selected IDs, and concise warnings in `design.json`.
4. Ensure optional knowledge errors never instantiate or mutate CAD lifecycle state.

## Task 5: MCP tools and profiles

Files:

- modify `src/mechanical_design_agent/tool_profiles.py`;
- modify `src/mechanical_design_agent/server.py`;
- modify `tests/test_tool_profiles.py`;
- modify `tests/test_bootstrap_mcp.py`;
- add or extend lightweight MCP tests.

Work:

1. Change the default profile to `design` and add explicit `governed`.
2. Redefine `design` to contain lightweight session tools, status, and applicable standard-part tools only.
3. Move existing Job/change/approval/delivery tools to `governed` while retaining them in explicit `all`.
4. Register `design_start`, `design_knowledge_retrieve`, and `design_record_result` without constructing `MechanicalDesignService` for start/result.
5. Lazily construct the database-backed knowledge adapter only for retrieval and translate optional backend failures.
6. Verify tool inventories are complete and no legacy lifecycle tool appears in the default schema.
7. Run focused server/profile tests.

## Task 6: Agent instructions, skills, and public documentation

Files:

- modify `AGENTS.md`;
- modify `.agents/skills/mechanical-design-job-workspace/SKILL.md` or replace its default routing role with lightweight instructions;
- modify `.agents/skills/README.md`;
- modify `README.md`;
- modify `docs/ARCHITECTURE.md`;
- modify `docs/DESIGN_JOB_WORKSPACES.md` to label governed compatibility;
- modify `docs/OPTIONAL_AGENT_WORKFLOWS.md`;
- modify packaging/public documentation tests and allowlists where necessary.

Work:

1. Remove mandatory ordinary-design references to Jobs, obligations, envelopes, mutation authorization, canonical `批准`, delivery approval, and mandatory lesson review.
2. Document one natural-language design approval and direct CAD mutation.
3. Document database-optional knowledge fallback and mandatory exact-hash validation.
4. Keep governed and durable knowledge workflows explicit and separate.
5. Run documentation, skill, packaging, and public-distribution tests.

## Task 7: Regression and cross-platform verification

Work:

1. Run all focused new tests.
2. Run the full supported offline suite with the repository virtual environment.
3. Repair real regressions without weakening assertions or skipping tests.
4. Run build and package-content checks.
5. Inspect the final diff for private paths, generated artifacts, secrets, and unintended governance coupling.
6. Commit cohesive implementation changes on the branch.

## Task 8: Basketball-carrier live acceptance

Work:

1. Start `four-basketball-carrier-20260830` through the new lightweight flow using the already-approved requirements and existing empty FCStd only as an inspected neutral seed.
2. Retrieve optional knowledge; continue on completed-no-match or unavailable.
3. Build the approved one-piece PLA lattice carrier in the FreeCAD GUI.
4. Save to the lightweight session FCStd.
5. Run the project `freecad-model-validation` script with the approved numeric specification.
6. Inspect JSON, Markdown, saved PNG, and FreeCAD top/front/isometric views.
7. Automatically repair and revalidate until mandatory checks pass or a genuine engineering blocker remains.
8. Record the exact result in `design.json` and present links to the final local artifacts.

## Acceptance commands

```text
.venv/bin/pytest tests/test_approval_semantics.py
.venv/bin/pytest tests/test_lightweight_design.py tests/test_lightweight_knowledge.py
.venv/bin/pytest tests/test_tool_profiles.py tests/test_bootstrap_mcp.py
.venv/bin/pytest
.venv/bin/python -m build
```

Live PostgreSQL, Neo4j, Windows protected-host, and interactive FreeCAD tests remain environment-specific. Run the live FreeCAD acceptance for the basketball carrier in the connected local GUI. Do not claim Windows live acceptance unless it actually runs.
