# Engineer learning playbook

This playbook describes how a mechanical engineer and a connected coding agent
use deterministic MCP tools to build reviewed product knowledge. The service
never invents prose, silently publishes knowledge, or treats geometry
similarity as engineering authority.

## A. Initialize and define a product family

1. Run `mechanical-design init` for an explicit workspace. Zero product
   families is a valid bootstrap state.
2. Use `family create` to register organization, design group, family identity,
   and owner. Select it explicitly for the operational session or set it as the
   workspace default.
3. Create or resume one `product_family_onboarding` Design Job for the family
   intake. Keep its source snapshots, analysis, engineer review, approved
   knowledge, and database-publication receipt in that same Job. Do not create a
   Git worktree for the onboarding operation.
4. `library_scan` may report candidate CAD files but never imports them.
5. The engineer confirms the canonical family name, aliases, owning group, and
   source-folder mapping. Nested folders remain source metadata rather than
   automatic subfamilies.
6. Only an explicit `library_ingest_changes` selection starts FreeCADCmd.

## B. Learn one model

1. The coding agent calls `model_get_analysis` and may use a compatible
   external FreeCAD GUI MCP to view or select relevant objects.
2. `learning_start_session` opens an auditable model-scoped session.
3. `learning_next_targets` returns a bounded batch. Identity, structure, and
   product-boundary ambiguity take priority.
4. The coding agent writes a natural-language question from `prompt_intent`;
   the deterministic service never generates the question prose.
5. `learning_record_exchange` stores the engineer answer verbatim and keeps
   the agent interpretation separate. Evidence can be registered first with
   `evidence_artifact_register`.
6. The agent decomposes the answer into atomic assertions. Every reviewed
   proposal cites evidence from the same session.
7. `knowledge_review` approves, corrects, rejects, or supersedes each revision.
   Only approved assertions are searchable or usable by design tools.
8. `learning_defer_targets` records questions that cannot yet be answered so
   they do not recur during the same session.

The learning order is identity, structure, terminology, function, interfaces,
parameters, requirements, and design outcomes. Repeated geometry may support a
definition/occurrence candidate; it never proves shared names or functions.

## C. Learn across models

- Hash-identical files reuse existing analysis. Changed content creates a linked
  revision rather than mutating history.
- Moving a model between confirmed family folders creates an assignment
  conflict; it does not silently change scope.
- `family_compare_models` produces deterministic dimensions, structure
  statistics, vector similarity, and review candidates.
- Statistical generalization requires the configured number of distinct
  models. A direct expert rule may follow its explicit evidence path.
- A subfamily exists only after `subfamily_propose` and owner review; a nested
  source directory never creates one.

## D. Use knowledge in design

1. Route the request through a `mechanical_design` Job. Continue the same design
   in the same active or blocked Job; create a new Job only for an independent
   requirement. A missing or ambiguous resume requires user direction. Do not
   create a Git worktree for product design.
2. Build `DesignContext/v2` before retrieval or editing.
3. Without family authority, ask for requirements in neutral terms; do not
   borrow specialized roles, names, or parameter ranges.
4. Existing models become immutable-source-snapshot and
   source-revision-bound FCStd working copies inside the Job. New
   designs start from a neutral seed.
5. Record proposal, structure, and parameter changes as separately reviewable
   change phases.
6. Apply only approved changes through the appropriate FreeCAD integration and
   record the resulting FCStd hash.
7. Run FreeCAD model validation. Require the complete model-detected fastener
   inventory and the same-revision FCStd SHA-256.
8. Run declared mechanical-interface validation and then
   `AssemblyCompleteness/v2`. Require exactly-once fastened-joint assignment
   for the whole inventory.
9. Missing, duplicate, unknown, failed, or stale evidence is a mandatory model
   and assembly failure. `AssemblyCompleteness/v1` is rejected.
10. A reusable failure, correction, omission, or review outcome may enter the
   separate lesson workflow. Lesson capture never changes the delivery result.

The service serializes working-copy operations with a workspace-owned lock.
Lesson capture holds the same boundary while it snapshots the FCStd revision
and commits approval, preventing a concurrent edit between evidence hashing and
database commit.

## E. Review and publish a design lesson

The default publication path is:

```text
design outcome -> design_lesson_review_context
-> coding-agent summary when material and generalizable
-> design_lesson_review_prepare -> one immutable review card
-> engineer decision -> storage, projection, and retrieval verification
```

1. Gather before/after model hashes, change IDs, validation reports, measured
   facts, causes, corrections, prevention checks, applicability, explicit
   non-applicability, and evidence-bound neutral assertions.
2. `design_lesson_review_context` returns the reviewable history. If it has no
   material generalizable lesson, record no lesson.
3. `design_lesson_review_prepare` creates one immutable review card. The
   engineer makes one batch decision; the default flow does not request
   assertion-by-assertion approval.
4. Approved storage continues to PostgreSQL, outbox projection, and retrieval
   verification until the review is `stored-and-retrievable`.
5. `design_lesson_review_status(retry=True)` may perform one bounded retry
   using the already approved immutable card.

All of these lesson operations reuse the originating `mechanical_design` Job.
The staging package, immutable evidence, review card, publication receipt,
supersession, and revocation cannot be moved to another Job. If origin is
missing or ambiguous, stop; never create an onboarding or replacement Job for
the lesson.

The hash-bound `design_lesson_stage`, `design_lesson_staged_get`,
`design_lesson_approve`, and `design_lesson_supersede` tools remain an
expert/audit and recovery path. A staged package is never described as
approved. Any content change creates a new package and hash. Supersession or
revocation preserves history and never exists merely to clear a workflow gate.
