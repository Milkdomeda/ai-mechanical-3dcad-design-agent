# Design Lessons Single-Confirmation Publishing

**Status:** Approved design; release coordination revised
**Date:** 2026-08-29
**Target release:** AI Mechanical 3DCAD Design Agent v0.4.1

## Purpose

Simplify the default Design Lessons publication experience without weakening
engineering review, immutable evidence, Job provenance, authorization, or
retrieval verification.

The engineer must review and filter the exact Design Lessons content before it
enters shared knowledge. After that review, one simple confirmation authorizes
the complete publication workflow. Internal preparation, approval, PostgreSQL
storage, outbox projection, Neo4j synchronization, retrieval verification, and
bounded recovery must not require additional engineer confirmations.

The normal user interaction becomes:

```text
AI prepares and displays the final immutable Review Card
-> engineer requests any content changes
-> AI prepares and displays a replacement Review Card
-> engineer says "确认发布设计经验" once
-> software completes publication and reports the truthful result
```

Model-design confirmation remains separate. `模型设计确认` never approves or
publishes Design Lessons.

## Existing problem

The v0.4.0 default workflow preserves strong governance, but exposes too much
workflow machinery to an ordinary engineer:

```text
design_lesson_review_context
-> design_lesson_review_prepare
-> copy a Review ID into a canonical approval phrase
-> design_lesson_review_approve
-> design_lesson_review_status
-> retry until stored-and-retrievable
```

Only one human approval is conceptually required today, but Review IDs,
approval-tool selection, status polling, and recovery are visible to the user
and are easy for a connected agent to mishandle. The new design preserves the
same governed stages while moving their orchestration behind one default
publication surface.

## Goals

- Require the engineer to see, review, filter, and approve the final Review
  Card before publication.
- Require exactly one engineer confirmation for an unchanged Review Card.
- Use the simple canonical phrase `确认发布设计经验`, without a user-visible
  Review ID or digest.
- Bind approval to the exact immutable Review Card, Job, working copy, final
  FCStd SHA-256, delivery approval, evidence, and Job revision.
- Complete PostgreSQL storage, outbox processing, Neo4j projection, and
  retrieval verification automatically when infrastructure is available.
- Preserve the approval when projection or retrieval is temporarily pending;
  recovery must never request the engineer to approve the same card again.
- Report publication complete only at `stored-and-retrievable`.
- Record an explicit reviewed outcome when there is no publishable lesson.
- Keep existing review, staging, audit, supersession, and revocation tools
  compatible.

## Non-goals

- Publishing Lessons as a side effect of `模型设计确认`.
- Allowing the coding agent to approve Lessons on the engineer's behalf.
- Removing Review Cards, evidence manifests, or content hashes.
- Automatically changing Lesson content after approval.
- Changing Design Job routing, Product Family matching, Design Intent Approval
  Envelopes, CAD modeling, standard parts, validation, or delivery gates.
- Changing Lesson organization, design-group, family, applicability, or
  non-applicability authorization.
- Removing expert staging, audit, supersession, or revocation workflows.
- Adding a background daemon solely for Lesson publication.
- Updating the external FreeCAD GUI MCP integration.

## User-visible workflow

### Publishable lessons

1. The connected coding agent calls `design_lesson_review_context` after the
   final model is delivery-approved.
2. The agent selects only material, generalizable, evidence-backed Lessons. It
   removes duplicate, product-specific, unsupported, or non-actionable
   candidates.
3. The agent prepares an immutable Review Card through
   `design_lesson_review_prepare`.
4. The agent displays the complete Review Card, including:
   - title;
   - observed facts or problem;
   - cause;
   - correction;
   - prevention checks;
   - applicability;
   - explicit non-applicability;
   - neutral reusable assertions; and
   - source and evidence summary.
5. The engineer may request edits, deletions, or scope changes. An edit creates
   a new immutable Review Card and supersedes the prior unapproved card.
6. When satisfied, the engineer says exactly `确认发布设计经验`.
7. The agent calls `design_lesson_review_publish` with the active internal
   Review ID and the engineer's confirmation text.
8. The tool returns one of the public states `published`, `publishing`, or
   `blocked`.
9. The agent reports completion only when the public state is `published`.

The engineer never copies a Review ID, package digest, Job ID, or status token.

### No publishable lesson

When no candidate is material and generalizable, the agent displays a screening
summary that identifies why candidates were excluded. Typical reasons include:

- product-specific dimensions with no reusable rule;
- insufficient or stale evidence;
- duplication of an existing approved Lesson;
- no meaningful failure, correction, omission, or prevention outcome; or
- applicability too uncertain for safe reuse.

The screening summary is stored as an immutable no-publication Review Card.
The engineer says exactly `确认无可发布设计经验`, and the agent calls
`design_lesson_review_no_publish`. The resulting terminal state is
`reviewed-no-publishable-lesson`. It creates no Design Lesson event and no
Neo4j Lesson projection.

This is one confirmation in the no-publication path, not an additional
confirmation after publication review.

## Immutable Review Card contract

Every Review Card is created before engineer confirmation and contains or
binds:

- `review_id`;
- `review_outcome`: `publish` or `no_publish`;
- originating `job_id` and expected Job revision;
- `working_copy_id`;
- approved final artifact path and SHA-256;
- package schema and package SHA-256;
- Review Card SHA-256;
- typed evidence manifest and evidence digests;
- Lesson content, or the no-publication screening decision;
- creation actor and timestamp; and
- predecessor Review ID when replacing an unapproved card.

Only the latest non-terminal Review Card for the same review intent is
approvable. A replacement marks its predecessor `superseded`. A confirmation
for a superseded, rejected, invalid, or foreign card fails closed.

The server can prove that approval is bound to one immutable card. It cannot
independently prove what an arbitrary third-party user interface displayed.
Project-owned Agent instructions therefore require displaying the complete
card immediately before requesting confirmation. A future client-integrated
approval UI may provide stronger display attestation without changing the
publication state machine.

## MCP tool contracts

### `design_lesson_review_publish`

This is the default publication tool for ordinary users.

Inputs:

```json
{
  "review_id": "DLR-...",
  "confirmation": "确认发布设计经验",
  "job_id": "internal optional compatibility binding",
  "expected_job_revision": 1
}
```

The connected agent supplies internal identifiers. The engineer supplies only
the confirmation phrase.

The tool must:

1. trim surrounding whitespace and otherwise require the exact canonical
   confirmation;
2. authorize the configured actor and scope;
3. lock the originating Job working copy;
4. re-read the Review Card, Job, working copy, delivery binding, evidence, and
   current FCStd hash;
5. reject stale, superseded, changed, unauthorized, or non-publish Review
   Cards before approval;
6. atomically store the approval and approved Design Lesson in PostgreSQL;
7. enqueue immutable outbox events;
8. make one bounded projection and retrieval-completion attempt; and
9. return a stable public result without requiring another confirmation.

The result schema is `DesignLessonPublication/v1` and includes:

```json
{
  "status": "published | publishing | blocked",
  "review_id": "DLR-...",
  "review_card_sha256": "...",
  "publication_receipt_sha256": "...",
  "internal_status": "stored-and-retrievable",
  "next_action": "none"
}
```

`publication_receipt_sha256` may be absent until the durable approval exists.
Failure output contains a stable reason code and must not expose secrets,
absolute paths, or private source-family incident details.

### `design_lesson_review_no_publish`

This finalizes an immutable screening decision without creating a Lesson.

Inputs:

```json
{
  "review_id": "DLR-...",
  "confirmation": "确认无可发布设计经验",
  "job_id": "internal optional compatibility binding",
  "expected_job_revision": 1
}
```

It performs the same actor, Job, revision, artifact, hash, and Review Card
checks. It accepts only `review_outcome=no_publish`. Its successful public
result is `reviewed-no-publishable-lesson` and includes an immutable decision
receipt. It emits no approved Design Lesson event.

### Existing tools

The following tools keep their current parameter and result contracts:

- `design_lesson_review_context`;
- `design_lesson_review_prepare`;
- `design_lesson_review_approve`;
- `design_lesson_review_reject`;
- `design_lesson_review_status`;
- `design_lesson_stage`;
- `design_lesson_staged_get`;
- `design_lesson_approve`;
- `design_lesson_search`;
- `design_lesson_get`;
- `design_lesson_audit_get`;
- `design_lesson_supersede`; and
- `design_lesson_revoke`.

`design_lesson_review_prepare` gains additive support for a versioned
no-publication screening package while preserving existing
`DesignLessonPackage/v1` behavior. Existing approval and status tools remain
available for older clients and explicit recovery. They are no longer the
documented default user workflow.

## State model

Internal review states remain authoritative:

```text
awaiting-engineer-review
superseded
rejected
invalid
approved-retrieval-pending
stored-and-retrievable
reviewed-no-publishable-lesson
```

The new default tools render them into four user-facing states:

| Public state | Meaning |
| --- | --- |
| `published` | PostgreSQL storage, projection witnesses, and retrieval verification succeeded. |
| `publishing` | Approval is durable, but projection or retrieval verification is pending. No new confirmation is allowed or required. |
| `blocked` | Approval was not accepted because the card, evidence, authority, Job, or model is invalid or stale. |
| `reviewed-no-publishable-lesson` | Review completed and intentionally produced no shared Lesson. |

The software must never render `approved-retrieval-pending` as `published`.

## Transactions, retries, and idempotency

PostgreSQL remains authoritative. Approval, the Design Lesson event, Review
state transition, evidence bindings, and outbox writes occur in the existing
PostgreSQL transaction. Neo4j is never included in a distributed transaction.

After durable approval, the publish tool performs one bounded completion
attempt. If projection or retrieval verification is temporarily unavailable:

- the public state is `publishing`;
- the approval and immutable card remain durable;
- the agent calls the existing status/retry path without user interaction;
- every retry uses the same Review ID and approved package;
- no retry may modify Lesson content; and
- a later successful retry produces the same publication receipt and terminal
  result.

No background daemon is introduced. The connected agent performs silent,
bounded status retries using existing deterministic service behavior. A future
worker may consume the same durable state without changing user confirmation
semantics.

Repeated publish calls for an already stored-and-retrievable Review return the
same result and receipt. Repeated calls for an approved-pending Review resume
completion. They never insert a duplicate Lesson or outbox event.

## Failure behavior

### Before durable approval

The operation returns `blocked` with no approval side effect when:

- the Review Card was superseded, rejected, or invalidated;
- the Job or expected revision is stale;
- the working copy is not bound to the originating Job;
- the actor is outside the organization or design group;
- the final FCStd or approved delivery artifact changed;
- a required validation or evidence digest is stale or missing;
- the Review Card or package digest changed; or
- the requested outcome does not match the card.

Any content change requires a replacement Review Card and a new engineer
review. The old confirmation cannot authorize changed content.

### After durable approval

Infrastructure failures produce `publishing`, not `blocked`, when the approved
content remains valid. The same approval is retried without asking the engineer
again.

If recovery reveals a permanent content or binding error, the approved record
and failure evidence are retained. Software must not repair Lesson content
under the prior approval. A corrected package requires a new immutable Review
Card and a new confirmation because the approved content changed.

## Database migration

Add PostgreSQL migration `017_design_lesson_single_confirmation.sql`.

The migration is additive and must:

- add `review_outcome` with existing rows backfilled to `publish`;
- allow a no-publication Review to omit a Lesson ID while enforcing that a
  publish Review retains one;
- add the terminal `reviewed-no-publishable-lesson` state;
- store the canonical confirmation mode and immutable decision receipt;
- enforce that no-publication rows cannot reference a published Lesson event;
- retain package and Review Card SHA-256 constraints; and
- preserve all existing rows and foreign-key relationships.

The implementation should reuse the existing review table and outbox model. It
must not add a second competing source of authority merely to simplify the MCP
surface.

## Agent behavior

Update `AGENTS.md`, the Design Job Skill, the Engineer Learning Playbook, the
architecture guide, README, and MCP tool documentation so that connected
agents:

- prepare and display the complete current Review Card;
- never request confirmation before displaying it;
- accept edits as review feedback, not approval;
- request only `确认发布设计经验` for publication;
- request only `确认无可发布设计经验` for the no-publication outcome;
- pass internal identifiers without exposing them to the engineer;
- retry `publishing` without requesting confirmation;
- report completion only for `published`;
- never combine model confirmation and Lesson confirmation; and
- keep supersession, revocation, rejection, and audit as separate governed
  operations.

The Agent Skill guides orchestration, but PostgreSQL state, immutable hashes,
authorization, and publication transitions remain software-enforced gates.

## Security and privacy

- The configured actor must retain the existing family-owner publication
  authority and organization/design-group checks.
- Specialized source-family details remain redacted outside authorized scope.
- Publication receipts contain opaque identifiers and digests, not absolute
  paths or source incident prose.
- Job files, evidence, Review Cards, and receipts remain under the originating
  Design Job.
- The public repository receives no generated Lesson, Job, customer, or
  runtime evidence.
- The new tools do not grant remote access or change loopback-only service
  boundaries.

## Compatibility and release version

This is an additive, backward-compatible MCP capability and a new default user
workflow. It does not remove or silently change existing tool contracts. The
selected release version is v0.4.1.

Other independently developed bug fixes are expected to enter the same v0.4.1
release. This feature branch owns only the Design Lessons change and its
focused validation. It must not be released independently. After all approved
change streams are merged into `main`, the combined `main` state must run the
complete supported suite, package and installation checks, public-boundary
checks, and one overall code review before the single v0.4.1 release is
authorized.

Compatibility requirements:

- existing Review IDs and states remain readable;
- existing `design_lesson_review_approve` and status retry calls remain valid;
- existing staged Lesson and expert/audit workflows remain valid;
- existing database rows migrate without re-approval;
- existing approved-pending rows remain resumable; and
- existing stored-and-retrievable rows remain terminal and idempotent.

## Test strategy

### Focused unit and service tests

- exact simple confirmation succeeds without an ID in user text;
- wrong, missing, or extra confirmation text fails before approval;
- the agent-supplied Review ID remains bound to the active immutable card;
- publish and no-publication outcome mismatches fail closed;
- replacement cards supersede predecessors;
- stale Job revisions, FCStd hashes, artifacts, validation, and evidence fail;
- foreign organization, design group, working copy, and Job requests fail;
- repeated calls are idempotent;
- approved-pending retries never require another confirmation;
- `publishing` is never rendered as complete;
- no-publication writes a decision receipt but no Lesson or Neo4j projection;
- content changes after approval require a new Review Card; and
- old approve, reject, status, stage, audit, supersede, and revoke paths remain
  compatible.

### PostgreSQL and projection tests

- migrations 001 through 017 apply from a clean database and upgrade v0.4.0;
- publication approval, Lesson event, evidence bindings, Review transition,
  and outbox rows commit atomically;
- transient projection and retrieval failures remain resumable;
- durable projection witnesses prove both Lesson and Review events;
- retrieval verifies the exact approved Lesson and applicability; and
- Neo4j rebuild reproduces the PostgreSQL-authoritative result.

### Packaging and release tests

- MCP schema and public tool inventory include the two new tools;
- installed wheel owns migration 017 and every required resource;
- complete supported offline tests pass;
- wheel and sdist build and install cleanly;
- CLI and MCP entry points work from the clean wheel;
- public documentation links and examples pass;
- public distribution scans find no generated Job or private evidence; and
- macOS and Windows portability tests cover path, encoding, locking, and
  installed-resource behavior applicable to the new migration and Job receipt.

## Acceptance criteria

The feature is complete when:

1. an engineer reviews the exact final immutable Review Card;
2. one `确认发布设计经验` confirmation authorizes the full unchanged-card
   publication workflow;
3. no Review ID or digest is required in user text;
4. the normal path reaches `published` and is retrievable;
5. transient post-approval failures resume without another confirmation;
6. stale or changed content cannot use the old confirmation;
7. a reviewed no-publication outcome is auditable and creates no Lesson;
8. existing clients and expert workflows remain compatible;
9. the complete required test and package gates pass; and
10. the combined release documentation accurately describes v0.4.1 behavior,
    bug fixes, and limits.
