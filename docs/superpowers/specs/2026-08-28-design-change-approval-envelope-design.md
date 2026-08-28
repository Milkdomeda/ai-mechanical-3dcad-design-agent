# Design Change Approval Envelope

## Purpose

Replace per-change human approval with one human approval of a bounded design
intent. Every CAD implementation change remains auditable, but a valid Approval
Envelope authorizes routine engineering iteration until a material design-intent
boundary is crossed.

This is a public software contract. Examples and tests use synthetic components
only. Product Family selection remains optional and is not part of the approval
decision.

## Lifecycle

1. Before the first substantive CAD mutation, the agent records a
   `design_proposal` and a complete Approval Envelope draft.
2. The user approves or requests modification using a simple interaction:
   `批准` or `修改方案`. The MCP call retains the internal change-set ID, so the
   user never has to copy a UUID.
3. Approval transactionally creates an active immutable envelope and links the
   initial change set to it.
4. Every subsequent implementation iteration is recorded as a change set with a
   structured semantic impact declaration.
5. A deterministic classifier either:
   - authorizes the change under the active envelope and records an autonomous
     audit event; or
   - fails closed, leaves the change pending human approval, and records the
     material or ambiguous boundary reason.
6. Applying an authorized change records the resulting FCStd hash and an audit
   event. It does not require another user confirmation.
7. Human approval of a material successor creates a new envelope revision and
   supersedes the previous active envelope.

## Approval Envelope

The PostgreSQL-authoritative envelope contains:

- envelope ID and source approval/change-set ID;
- Design Job ID and working-copy ID;
- approved design intent;
- architecture and mechanism;
- key interfaces;
- important user constraints;
- manufacturing method;
- material constraints when specified;
- validation requirements;
- approval actor, source text, time, Job revision, and envelope revision;
- status: `active`, `superseded`, or `revoked`.

The source draft is stored on the proposed change set. Approval copies the
validated draft into an immutable envelope row. The active envelope is unique per
working copy.

## Semantic Boundary Decision

The change-impact declaration uses semantic fields, not percentage thresholds:

- controlled change kind;
- mechanism and architecture impact;
- affected key interfaces;
- functional impact;
- explicit approved-constraint outcomes: `within`, `exceeds`, or `unknown`;
- manufacturing-process impact;
- material-constraint impact;
- new standard-part or hardware categories;
- removed or weakened validation requirements;
- boundary certainty.

The following are eligible for autonomous authorization when all semantic impact
fields remain inside the approved envelope:

- parameter or dimension optimization;
- feature-detail optimization;
- chamfers and fillets;
- clearance adjustment;
- interference repair;
- geometry-validity repair;
- validation-driven repair;
- implementation refinement that preserves the approved intent.

Human approval is required when any rule reports:

- mechanism or architecture change;
- key-interface change;
- material functional change;
- an exceeded or unknown approved constraint;
- manufacturing-process or specified-material-constraint change;
- a new standard-part/hardware category;
- a removed or weakened validation requirement;
- ambiguous or missing boundary evidence.

Unknown change kinds and incomplete semantic declarations fail closed. No rule
uses a fixed percentage as a proxy for design intent.

## Audit Model

Each change set records its envelope, authorization mode, whether human approval
is required, and the complete deterministic boundary decision. A separate append-
only event table records proposal creation, human approval/rejection, autonomous
authorization, fail-closed boundary decisions, application, and envelope
supersession.

`reviewed_by` is never populated for autonomous changes, so an envelope-based
authorization cannot be mistaken for a new human review.

## Compatibility

- Existing change-set IDs and lifecycle states remain valid.
- Existing callers may continue to include ID-bearing Chinese confirmation text.
- New callers may pass the simple canonical user response while retaining the
  internal ID in the MCP arguments.
- Legacy proposed or approved changes without enough data to create an envelope
  fail closed and require a complete successor Design Intent proposal. Existing
  IDs and history remain readable and are never silently promoted.
- Product Family matching and binding are unchanged.

## Fail-Closed Mutation Gate

A change can be marked applied only when:

- it was explicitly approved and created an envelope; or
- it was deterministically authorized by the active envelope.

Proposed, rejected, outside-envelope, ambiguous, stale-envelope, or unbound
changes cannot be marked applied. This gate protects governed state even if a
caller attempts to skip the intended workflow.

## Synthetic Test Matrix

Tests use temporary workspaces, fake repositories, synthetic IDs, and generated
fixture data. They cover:

1. first mutation blocked before approval;
2. parameter optimization authorized after approval;
3. interference repair authorized;
4. validation-driven repair authorized;
5. mechanism change requires approval;
6. key-interface change requires approval;
7. manufacturing-process or specified-material-constraint change requires
   approval;
8. ambiguous boundary fails closed;
9. autonomous changes produce complete audit history;
10. Product Family remains optional;
11. no test reads or mutates a real Design Job;
12. schema migration, MCP compatibility, package resources, and distribution
    boundary checks.

## Release Acceptance

The branch must pass focused tests, the complete offline suite, applicable
FreeCAD 1.1.3 installed-wheel acceptance, public-distribution scans,
`git diff --check`, wheel/sdist build, and clean-wheel smoke tests before merge.
