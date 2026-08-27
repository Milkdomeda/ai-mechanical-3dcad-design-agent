# Design Job workspaces

A Design Job is the durable workspace for one product-design intent. Continue
the same design in the same Job. Create another Job only when the user states
that the requirement is independent. Product design is not software
development. Do not create a Git worktree, Git branch, or nested repository for
a mechanical-design Job.

PostgreSQL is authoritative for Job identity, lifecycle, revisions, bindings,
and approvals. The filesystem is a controlled projection for CAD files and
evidence. A directory name or path is never accepted as Job authority; resolve
the Job through the Mechanical Design MCP or CLI first.

## Routing rules

- If the user supplies a Job UUID or `JOB-YYYYMMDD-NNN` display ID, call
  `design_job_get`. Do not search for similar titles.
- For an explicitly independent requirement, call `design_job_create` directly
  with a stable idempotency token.
- For “continue” or “resume” without an ID, call `design_job_resolve` against
  `active` and `blocked` Jobs. Reuse exactly one same design candidate. Return
  multiple candidates and stop on ambiguity. With no candidate, clarify whether
  the request is new before creating a Job.
- Never choose the “closest” Job, merge two Jobs, or create a replacement Job to
  bypass a missing origin.

The project-owned `mechanical-design-job-workspace` Agent Skill contains the
same routing matrix in an agent-consumable form.

## Job types and knowledge provenance

`mechanical_design` covers new designs, existing models, and later work on the
same design. A Design Lesson remains attached to its originating
`mechanical_design` Job, including staging, immutable evidence, review,
publication, supersession, and revocation. If that origin is missing or
ambiguous, stop rather than creating a substitute Job.

`product_family_onboarding` covers product-family intake. Analysis, knowledge
review, and database publication continue in that original onboarding Job.
Creating or changing the software, database schema, migrations, or tests is
still a normal Git development task; only the product-family knowledge
operation belongs to the Job.

## Optional Product Family routing

A `mechanical_design` Job may keep `family_id` null for its entire lifecycle.
At intake, use PostgreSQL-backed `product_family_inventory` followed by
`product_family_match`; do not infer inventory from workspace JSON. An existing
Job or source-model binding and an exact approved family/product identifier are
authoritative. Descriptor-only similarity is a candidate that requires user
confirmation. No credible match proceeds unbound, and no match creates a new
family. Family discovery metadata is visible for matching, but specialized
assertions, lessons, and analogous models remain excluded until binding is
authorized.

## Directory contract

Each ready Job is stored below the configured workspace at
`jobs/<job-directory>/`. The portable directory name combines its display ID
and sanitized title. The controlled tree is:

```text
jobs/<job-directory>/
├── job.json
├── inputs/source/
├── requirements/draft/
├── requirements/approved/
├── models/working/
├── models/revisions/
├── models/exports/
├── components/standard-parts/
├── analysis/
├── validation/specifications/
├── validation/reports/
├── validation/images/
├── knowledge/retrieval-receipts/
├── knowledge/extracted/
├── knowledge/design-lessons/
├── previews/
├── delivery/
├── provenance/
└── logs/
```

`job.json` is a checked projection of PostgreSQL state. Source snapshots are
immutable. FCStd is the source of truth for designed parts and assemblies.
Working copies, validation evidence, delivery snapshots, and Design Lesson
evidence are bound to the exact Job and revision. Generated Job contents stay
outside Git and outside wheel/sdist packages. No `.git` directory may exist
inside a Job.

## New, existing, and resumed designs

For a new design, create a `mechanical_design` Job and then call
`design_job_new_working_copy_create` with the current Job revision. It creates a
neutral FCStd seed inside the Job before substantive modeling.

For an existing FCStd or STEP model, stage exactly one source when creating the
Job, then call `design_job_working_copy_create`. The original source remains
read-only; the Job receives an immutable source snapshot and a normalized FCStd
working copy. Never edit a working copy until its source, Job, and revision
bindings are complete.

For a resumed design, resolve or get the existing Job and inspect its current
revision and active working-copy identity. Reuse them. Optimistic revision
checks deliberately reject stale calls, so refresh with `design_job_get` before
retrying an operation.

## CLI examples

On macOS, create a Job with an explicit workspace:

```bash
mechanical-design job create \
  --workspace /path/to/mechanical-design-workspace \
  --job-type mechanical_design \
  --title "Pump support redesign" \
  --organization-id example-org \
  --design-group-id example-design-group \
  --idempotency-token pump-support-20260824
```

On Windows PowerShell, use the same contract with native paths:

```powershell
mechanical-design job create `
  --workspace "D:\Mechanical Design Workspace" `
  --job-type mechanical_design `
  --title "Pump support redesign" `
  --organization-id example-org `
  --design-group-id example-design-group `
  --idempotency-token pump-support-20260824
```

Use `job status`, `job list`, and `job resolve` for read-only inspection. Close
or reopen only with the exact current revision, valid phase and status, reason,
and the user's canonical confirmation. The agent must not manufacture the
confirmation.

## Upgrade from pre-Job working copies

Migration is explicit and receipt-bound. First create a dry-run plan and save
its UTF-8 JSON output:

```bash
mechanical-design job migrate-legacy --dry-run \
  --workspace /path/to/mechanical-design-workspace
```

Review the plan, then apply that exact saved file and receipt:

```bash
mechanical-design job migrate-legacy --apply \
  --workspace /path/to/mechanical-design-workspace \
  --plan-file /path/to/legacy-plan.json \
  --receipt-sha256 <receipt-sha256> \
  --confirmation "迁移旧设计 <receipt-sha256>"
```

PowerShell uses the same arguments and a Windows plan path. Each legacy working
copy becomes one independent Legacy Job; migration never guesses that two
designs belong together. It verifies source bytes and hashes, preserves the old
file, writes an immutable receipt, and is idempotent only while the saved plan
and source evidence remain unchanged.

Version 0.3.0 retains the documented v0.2 read wrappers for one transition
window. New writes must use Job-aware calls. Remove reliance on the deprecated
wrappers before the next breaking contract release.

## Diagnostics and recovery

Use `mechanical-design job doctor --job <job-reference>` before repair. Doctor
is read-only and returns a receipt SHA-256. Repair requires that receipt, the
current revision, a reason, and `修复 <job-reference>` confirmation. It may
finish a provably owned partial projection or quarantine a provably owned
attempt; it never guesses ownership or deletes unrelated data.

Common fail-closed codes include:

- `JOB_AMBIGUOUS`: more than one authorized identity matches; use the immutable
  UUID.
- `JOB_STALE_REVISION` or `JOB_REVISION_MISMATCH`: refresh Job state and repeat
  only after reviewing the new revision.
- `JOB_MIGRATION_REQUIRED`: a pre-Job working copy needs the explicit migration
  flow.
- `JOB_MIGRATION_PLAN_STALE` or `JOB_MIGRATION_DIVERGED`: regenerate the dry-run
  plan; do not force the old receipt.
- `JOB_PROJECTION_INCOMPLETE`, `JOB_PRESERVED_ATTEMPT_FOUND`, or
  `JOB_ATTEMPT_RECOVERY_REQUIRED`: run doctor, retain the evidence, and use the
  receipt-bound repair path if it is authorized.
- `JOB_NOT_FOUND_OR_UNAUTHORIZED`: do not infer whether the Job exists outside
  the caller's scope.

After recovery, rerun `job doctor`, inspect the active working copy and hashes,
and repeat every validation or approval invalidated by a geometry or metadata
change.
