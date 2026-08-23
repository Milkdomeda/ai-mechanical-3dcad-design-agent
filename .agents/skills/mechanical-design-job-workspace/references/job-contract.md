# Design Job contract

Use the configured Mechanical Design MCP; do not turn a local path, model name,
or hand-written directory into Job identity. Task 4 exposes these public
interfaces:

- `design_job_resolve` returns all authorized candidates for an intent query;
  it never selects a Job implicitly.
- `design_job_create` provisions one scoped Job from its type, title,
  organization, design group, optional family, and idempotency token.
- `design_job_get` reads one authorized UUID or display-ID reference, and
  `design_job_list` filters Jobs only within the configured authorized scope.
- `design_job_close` and `design_job_reopen` require the current Job revision,
  reason, phase, and the user's matching confirmation. Do not supply that
  confirmation on the user's behalf.

Same design means reuse the uniquely resolved Job. A genuinely independent
demand gets a new Job. Multiple plausible candidates, missing scope, or an
unclear relation to an existing design is ambiguous: show the candidates and
stop for direction.

## Current boundary and later bindings

Task 4 is a Job lifecycle contract. In this release, `design_job_create`
rejects `source_files`: it cannot create source snapshots, working-copy
bindings, evidence bindings, or Design Lesson bindings. Do not claim that an
unsupported Task 4 call performed any of those operations.

Task 6 governs immutable source snapshots and binds each downstream
`working_copy_id`, evidence artifact ID, and Design Lesson ID to the Job ID.
Until that governed interface is available, preserve the resolved Job identity,
keep sources read-only, and stop rather than manufacture filesystem metadata or
an unsupported binding.
