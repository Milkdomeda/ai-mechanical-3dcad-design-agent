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

## Optional Product Family match

Before creating a new independent mechanical-design Job, call
`product_family_inventory` and then `product_family_match` with the request and
structured design features. PostgreSQL inventory is authoritative;
`workspace_product_family_list` is bootstrap configuration only.

- `authoritative_match`: create the Job or working copy with the returned
  `binding_family_id` and preserve the match audit.
- `confirmation_required`: present the candidates and the no-family option;
  bind only after the user's choice.
- `unbound_no_match`: continue with `family_id=null` without creating or
  selecting a family.
- `conflict`: stop product-family reassignment and request direction.

Discovery metadata does not authorize specialized family knowledge. Retrieve
that knowledge only after an authoritative or user-confirmed binding. For a
resumed Job, preserve its existing family or null binding; never substitute the
only configured family.

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

## Design Lesson decision

After delivery, call `design_lesson_review_context` and filter candidates for
material, generalizable, evidence-backed learning. Prepare and display the
complete immutable Review Card before asking the engineer to decide. Requested
edits create a replacement card and supersede the prior pending card.

- For a publishable card, ask only `确认发布设计经验` and call
  `design_lesson_review_publish` with the internal Review ID and Job binding.
  Report complete only for `published`; retry `publishing` internally without
  another confirmation.
- If nothing remains publishable, display the immutable screening card, ask
  only `确认无可发布设计经验`, and call
  `design_lesson_review_no_publish`. This records
  `reviewed-no-publishable-lesson` without creating shared knowledge.

Never combine either Lesson decision with `模型设计确认`. Keep Review IDs,
digests, and recovery polling out of the engineer's confirmation text.

`superpowers:brainstorming` is an optional external capability for structured
discovery. Do not bundle, install, configure, or count it as a project-owned
Skill.
