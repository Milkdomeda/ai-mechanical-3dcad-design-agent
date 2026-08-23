---
name: mechanical-design-job-workspace
description: Route FreeCAD product operations through controlled Design Jobs. Use for new designs, existing models, resumptions, Product Family onboarding, or Design Lessons; do not use for software-only changes.
---

# Mechanical Design Job Workspace

Use this Skill before any product operation: a new design, an existing model,
a resume, Product Family onboarding, or Design Lessons. It covers public
FreeCAD work on macOS and Windows only; it does not add Blender, rendering, or
video work.

## Route the request first

The first product operation resolves or creates a Job through the configured
Mechanical Design MCP:

1. Call `design_job_resolve` with the known scope and design intent.
2. Reuse the one matching Job for the same design; call `design_job_get` to
   confirm its current identity and state. An independent demand creates a new
   Job with `design_job_create` and an idempotency token.
3. If resolution returns more than one candidate, or the design intent is not
   sufficient to distinguish one, present the candidates and stop for user
   direction. Treat that ambiguity as blocking; never choose a Job implicitly.

Job identity is not an arbitrary filesystem path. Read [the Job
contract](references/job-contract.md) for the Task 4 interfaces, their
lifecycle limits, and the later Task 6 binding behavior.

## Work within the Job

Product work uses the resolved Job workspace.
Do not create a Git branch or Git worktree.
Treat an existing source model as read-only. Task 6 supplies the governed
source-snapshot and downstream-binding behavior; Task 4's lifecycle APIs do not
yet provide an executable snapshot operation.

Keep the Job ID with every downstream working-copy, evidence, and Design Lesson
ID when the governed downstream interfaces are available. Do not invent an
unbound substitute or write a Job binding directly to the filesystem.

Product Family and Design Lesson database writes are Job operations. Changing
their implementation, schema, migrations, or tests is software development.
For a mixed request, state the split: route the product portion through its Job
and handle the software portion with the normal Git workflow.

`superpowers:brainstorming` is an optional external capability for structured
discovery. Do not bundle, install, configure, or count it as a project-owned
Skill.
