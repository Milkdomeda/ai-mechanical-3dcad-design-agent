# Design Job contract

Use the configured Mechanical Design MCP; do not turn a local path, model name,
or hand-written directory into Job identity.

## Routing precedence

1. A supplied Job UUID or display ID is an identity: call `design_job_get` for
   its authorized state. Do not send it to `design_job_resolve` as text.
2. An explicitly independent demand calls `design_job_create` directly, even if
   similar Jobs exist.
3. A continue/resume request without an explicit ID calls `design_job_resolve`
   only for `active` and `blocked` Jobs. Reuse one candidate; return multiple
   candidates and stop; with zero candidates, clarify whether this is an
   independent/new demand and create only after that intent.

Never select a candidate or create a Job from ambiguous intent. Product Family
review, knowledge, and database publication reuse their original
`product_family_onboarding` Job. A Design Lesson uses its originating
`mechanical_design` Job only; stop if that origin is missing or ambiguous.

## Job lifecycle and binding MCP calls

The release exposes these public MCP interfaces:

- `design_job_resolve` returns all authorized candidates for an intent query;
  it never selects a Job implicitly.
- `design_job_create` provisions one scoped Job from its type, title,
  organization, design group, optional family, and idempotency token.
- `design_job_get` reads one authorized UUID or display-ID reference, and
  `design_job_list` filters Jobs only within the configured authorized scope.
- `design_job_close` and `design_job_reopen` transition the lifecycle only
  with the required current revision, reason, phase, and user confirmation.
- `design_job_working_copy_create` captures one immutable existing-model source
  and creates its governed FCStd working copy with the current Job revision.
- `design_job_new_working_copy_create` creates a neutral governed FCStd seed for
  a new design with the current Job revision.

## Allowed Job types and phases

| Job type | Allowed phases |
| --- | --- |
| `mechanical_design` | `requirements`, `design`, `validation`, `delivery`, `lesson_capture`, `completed` |
| `product_family_onboarding` | `intake`, `analysis`, `knowledge_review`, `database_publication`, `completed` |

The only statuses are `active`, `blocked`, `completed`, `cancelled`, and
`archived`. `completed`, `cancelled`, and `archived` are terminal.

## Lifecycle calls and confirmations

| Operation | Required values | Canonical confirmation |
| --- | --- | --- |
| `design_job_close` | Current `expected_revision`, terminal `status` (`completed`, `cancelled`, or `archived`), valid phase, and reason. | `关闭 <job-reference>` |
| `design_job_reopen` | Current `expected_revision`, valid phase, and reason; only a terminal Job reopens to `active`. | `重开 <job-reference>` |
| CLI/service doctor and repair | `design_job_doctor` is read-only; `design_job_repair` also needs current `expected_revision`, doctor receipt SHA-256, and reason. | `修复 <job-reference>` for repair |

`design_job_doctor` and `design_job_repair` are service/CLI operations, not
MCP tools. Use `mechanical-design job doctor` for read-only inspection and
`mechanical-design job repair` only with the receipt-bound repair inputs and
the matching user confirmation. Never supply a confirmation on the user's
behalf.

## Governed source and downstream bindings

`design_job_create` accepts no source for a new design or exactly one FCStd,
STEP, or STP source for an existing-model Job. A supplied source is staged as
intent only; the returned `next_action` must be completed with
`design_job_working_copy_create` before editing. The source remains read-only,
and the Job stores an immutable snapshot and normalized FCStd working copy.

Each downstream `working_copy_id`, validation/delivery evidence record, Product
Family onboarding record, and Design Lesson record is bound to the Job ID and
expected revision. A stale revision, foreign Job, ambiguous origin, or missing
binding stops the operation. Do not manufacture filesystem metadata or cross a
Job boundary to bypass the governed interface.
