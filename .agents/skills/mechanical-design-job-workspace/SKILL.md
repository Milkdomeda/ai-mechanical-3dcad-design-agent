---
name: mechanical-design-job-workspace
description: Route FreeCAD product operations through controlled Design Jobs. Use for new designs, existing models, resumptions, Product Family onboarding, or Design Lessons; do not use for software-only changes.
---

# Mechanical Design Job Workspace

Use this Skill before any product operation: a new design, an existing model,
a resume, Product Family onboarding, or Design Lessons. Its public scope is
mechanical FreeCAD work on macOS and Windows.

## Routing decision matrix

Apply the first matching row through the configured Mechanical Design MCP.

| Incoming request | Required action | Stop rule |
| --- | --- | --- |
| Explicit Job UUID or display ID | Call `design_job_get` for authorized state. | Never pass the ID as a `design_job_resolve` query. |
| Explicitly independent demand | Call `design_job_create` directly with an idempotency token. | Do not resolve merely similar Jobs first. |
| Resume without an explicit ID; one active/blocked candidate | Call `design_job_resolve` for `active` and `blocked`; reuse it. | Do not create a duplicate Job. |
| Resume without an explicit ID; multiple candidates | Return candidates. | Stop; never select or create automatically. |
| Resume without an explicit ID; zero candidates | Clarify whether the demand is independent/new. | Create only after that explicit intent. |

Never treat a UUID or display ID as text to resolve, and never auto-create from
an ambiguous intent.

## Provenance and Job type

| Product operation | Job type | Provenance rule |
| --- | --- | --- |
| New, existing, or resumed mechanical design | `mechanical_design` | Use the routed Job. |
| Product Family intake/onboarding | `product_family_onboarding` | Create or reuse the onboarding Job through the routing matrix. |
| Product Family review, knowledge, or database publication | `product_family_onboarding` | Reuse the original onboarding Job. |
| Design Lesson | Originating `mechanical_design` only | Stop if origin is missing or ambiguous; never create a replacement or onboarding Job. |

Job identity is not an arbitrary filesystem path. Read [the Job
contract](references/job-contract.md) for lifecycle, working-copy, provenance,
and recovery behavior.

## Work within the Job

Product work uses the resolved Job workspace.
Do not create a Git branch or Git worktree.
Treat an existing source model as read-only. Stage exactly one FCStd/STEP source
through `design_job_create`, then call `design_job_working_copy_create` with the
returned Job revision. For a new design, call
`design_job_new_working_copy_create` after creating the Job.

Keep the Job ID and expected revision with every downstream working-copy,
evidence, Product Family, and Design Lesson operation. Do not invent an unbound
substitute or write a Job binding directly to the filesystem.

Product Family and Design Lesson database writes are Job operations. Changing
their implementation, schema, migrations, or tests is software development.
For a mixed request, state the split: route the product portion through its Job
and handle the software portion with the normal Git workflow.

`superpowers:brainstorming` is an optional external capability for structured
discovery. Do not bundle, install, configure, or count it as a project-owned
Skill.
