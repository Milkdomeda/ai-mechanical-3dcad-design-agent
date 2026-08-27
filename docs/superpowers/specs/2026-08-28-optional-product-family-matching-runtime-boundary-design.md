# Optional Product-Family Matching and Runtime Data Boundary

**Date:** 2026-08-28

**Status:** User-approved architecture; implementation pending

## 1. Purpose

Correct the product-family boundary so ordinary mechanical design never requires a Product Family, while preserving the agent's ability to identify an existing family from a user's request and obtain clarification when the match is uncertain.

The public software repository defines reusable behavior. Product families, models, Design Jobs, Design Lessons, knowledge assertions, validation evidence, and approvals are user-owned runtime data and must never become public product source.

## 2. Problem statement

The current runtime contains three coupled defects:

1. Several ordinary working-copy and design-lifecycle tools declare `product_family` as a required bootstrap component even though their persisted records support `family_id=null`.
2. The lazy service construction path requires a selected family configuration for nearly every non-Job tool, so a valid family-independent design is blocked before its working-copy scope can be evaluated.
3. The workspace family-list command enumerates active configuration files rather than the PostgreSQL authority. A database family can therefore remain intact but disappear from the runtime-visible inventory when its local configuration file is absent or inactive.

The result incorrectly conflates:

- no selected family;
- no matching family;
- a family stored in PostgreSQL but not represented by an active local file; and
- a family-specific operation that genuinely requires explicit authority.

## 3. Ownership boundary

### 3.1 Public product source

The public repository may contain only reusable software assets:

- application source;
- MCP and CLI contracts;
- database migrations and schemas;
- portable configuration templates;
- validation and lifecycle rules;
- Agent skills and public documentation;
- packaging and release infrastructure;
- tests that use synthetic identities and generated fixtures.

It must not contain real user models, Job manifests, product-family identities, model lists, lessons, knowledge content, approvals, validation artifacts, credentials, or machine-local paths.

### 3.2 User runtime data

The following are user-owned data:

- source CAD and governed working copies;
- Design Jobs, revisions, proposals, approvals, validations, and deliveries;
- Product Families, aliases, subfamilies, profiles, onboarding evidence, and match metadata;
- Design Lessons, knowledge assertions, applicability records, and review evidence;
- BOMs, drawings, images, reports, and standard-part usage provenance;
- PostgreSQL authority and rebuildable Neo4j projections.

These belong to an explicitly selected local runtime root and its governed Job workspaces. They remain outside the public repository and outside public Git history.

### 3.3 Developer and user roles

One person may be both the software developer and a mechanical-design user. The workflows remain separate:

- software changes use the public product Git workflow and synthetic tests;
- mechanical design uses a Design Job and does not create a software branch or worktree;
- a software fix must not mutate real user design data;
- after a software fix is installed and accepted, the original Design Job resumes without replacement or recreation.

## 4. Required behavior

### 4.1 Product Family remains optional

- A new or existing mechanical design may have `family_id=null`.
- No-match and unselected-family states are normal operational states.
- Ordinary Job creation, working-copy creation, scoped knowledge retrieval, change lifecycle, validation, and delivery must work without a Product Family.
- The system must not auto-create a family to clear a gate.
- The system must not select the only registered family merely because it is the only candidate.

### 4.2 Automatic matching remains available

Before a new independent design is bound, the agent evaluates the request against the user's authorized family discovery inventory.

The matching outcome is one of:

| Outcome | Meaning | Required action |
|---|---|---|
| `authoritative_match` | An authoritative relationship identifies one family | Bind automatically and record evidence |
| `confirmation_required` | One or more semantic candidates are plausible but not authoritative | Ask the user before binding |
| `unbound_no_match` | No credible candidate exists | Continue with `family_id=null` |
| `conflict` | Authoritative sources disagree | Stop and ask the user; do not replace an existing binding |

### 4.3 Authoritative matches

The following may establish an automatic binding:

1. the active Design Job is already bound to a family;
2. an existing source model revision has an approved family binding;
3. the user explicitly names an exact family ID, canonical name, or approved alias for a new independent design;
4. an approved product or model identifier has an exact family relationship.

An existing Job or source-model binding takes precedence over a new inferred request. If the user's text conflicts with that binding, the result is `conflict`, not silent reassignment.

### 4.4 Semantic candidates

A semantic match may use only approved discovery metadata such as:

- canonical family name and aliases;
- approved product and model identifiers;
- product type and domain descriptors;
- high-level component classes, functions, and interface categories;
- explicitly published applicability summaries.

A semantic match never binds automatically. The agent presents the candidate family, its evidence, and a no-family option. Multiple plausible candidates are always presented for user selection.

### 4.5 No credible candidate

When no credible candidate exists:

- the design proceeds unbound;
- no irrelevant family is suggested merely to populate a field;
- no family is created automatically;
- the user is asked only when the request itself indicates that an existing family should exist but the system cannot identify it.

## 5. Discovery and knowledge-authorization boundary

Family discovery is not family-knowledge authorization.

Before binding, the matcher may read only the discovery metadata defined in Section 4.4. It may not read specialized family lessons, parameter ranges, incident details, family profiles, or source-model content.

After an authoritative binding or user confirmation:

- the working copy records the selected `family_id`;
- `explicit_family_authorization=true` becomes valid for that working-copy scope;
- specialized family knowledge may be retrieved under the existing authorization and applicability rules.

For `family_id=null`:

- `family_knowledge=false`;
- `explicit_family_authorization=false`;
- the DesignContext contains only authorized organization, design-group, general, model-scoped, and Job-scoped knowledge;
- registered but unrelated families are not queried.

## 6. Authoritative inventory

PostgreSQL remains authoritative for Product Family identity and lifecycle state.

The runtime must provide a database-backed family inventory that returns safe discovery fields and explicit state, including:

- `family_id`;
- canonical name and approved aliases;
- organization and design-group scope;
- lifecycle status;
- discovery descriptors;
- `workspace_configured` when a separate runtime configuration exists;
- `selected_for_session` when the user explicitly selected it.

The inventory must include an authorized database family even when no active local family JSON file exists. Missing optional runtime configuration must be reported as a separate readiness state, not as nonexistence.

The existing workspace-file listing may remain as a bootstrap diagnostic, but it must identify its source as `workspace_config` and must not be presented as the authoritative Product Family inventory.

## 7. Runtime service profiles

Replace the single family-dependent operational service path with purpose-specific profiles.

### 7.1 Family-neutral design profile

Provides the minimum dependencies for:

- Design Job and working-copy lookup;
- family-neutral DesignContext construction;
- retrieval receipts;
- change proposal, review, closure, and applied-revision records;
- validation and assembly-completeness records;
- delivery and artifact binding;
- database-backed family discovery and matching.

This profile requires workspace authority, actor identity, PostgreSQL, and only the additional components genuinely used by a requested operation. It does not require `family_config_path`.

### 7.2 Family-specific profile

Requires explicit family identity for operations whose subject is the family itself, including:

- Product Family onboarding, analysis, review, and publication;
- family profiles and subfamilies;
- family owner learning sessions;
- family-specific assertion review or publication;
- explicit family configuration management.

An unselected family may block these tools, but must not block the family-neutral profile.

### 7.3 Local configuration

Local machine configuration may hold paths, provider bindings, or execution settings needed by a selected runtime. It is runtime data, not Product Family identity authority and not public product source.

## 8. MCP contracts

### 8.1 Authoritative list

Add or version a database-backed Product Family inventory tool. Its response distinguishes:

- database registration;
- optional workspace/runtime configuration;
- explicit session selection.

It must not select or activate a family as a side effect.

### 8.2 Match operation

Expose a deterministic match result suitable for the coordinating agent:

```json
{
  "status": "confirmation_required",
  "binding_family_id": null,
  "candidates": [
    {
      "family_id": "synthetic-family-a",
      "match_kind": "semantic_candidate",
      "evidence": ["approved alias token", "component class match"]
    }
  ],
  "specialized_knowledge_authorized": false,
  "next_action": "ask_user"
}
```

No numeric confidence threshold may silently convert a semantic candidate into an authoritative match.

### 8.3 Existing design tools

Remove the Product Family bootstrap dependency from family-neutral design tools. Preserve existing request and response fields where possible; version any response whose meaning changes.

`PRODUCT_FAMILY_UNSELECTED` becomes an informational state for general design. `PRODUCT_FAMILY_SELECTION_REQUIRED` remains valid only for explicitly family-specific operations.

## 9. Audit contract

Every match decision records:

- Job and working-copy scope when available;
- normalized request features used for matching;
- candidate family IDs;
- match kind and evidence for each candidate;
- final family ID or `null`;
- decision source: authoritative relationship, user confirmation, or no match;
- whether specialized family knowledge was authorized;
- actor and timestamp.

The audit record must not copy specialized family content into an unbound Job.

## 10. Data migration and compatibility

- Do not add real Product Family JSON files to the public repository.
- Do not reconstruct a database family from recovery assets when the PostgreSQL record already exists.
- Existing database families become visible through the authoritative inventory without being automatically selected.
- Existing family-bound Jobs retain their bindings.
- Existing unbound Jobs remain unbound.
- Legacy local family configuration may be imported only through an explicit private runtime migration and may never be required for identity visibility.
- Existing MCP tools remain compatible for one documented transition window when a contract must be versioned.

## 11. Error handling

- Database unavailable: return an authoritative-inventory availability error; do not fall back to a partial workspace-file list as if it were complete.
- Invalid family configuration: report configuration readiness separately while preserving database identity visibility.
- Unauthorized family: exclude it from discovery and matching.
- Multiple authoritative relationships: return `conflict` and require user direction.
- Semantic ambiguity: return `confirmation_required`; never choose the first candidate.
- No match: return `unbound_no_match`, not `setup_required`.
- Family-specific operation without selection: return `PRODUCT_FAMILY_SELECTION_REQUIRED` with the relevant family operation identified.

## 12. Testing

All public tests use synthetic families and generated Job data.

Required focused tests:

1. An empty family registry permits a familyless new Job, retrieval receipt, change lifecycle, validation, and delivery.
2. A database contains a synthetic family while the workspace family directory is empty; the authoritative inventory returns it.
3. The same database-only family is not automatically selected for an unrelated request.
4. A familyless working copy builds a context with `family_knowledge=false` and never queries specialized family knowledge.
5. Exact Job, source-model, explicit family name, approved alias, and product/model relations produce `authoritative_match`.
6. A semantic candidate produces `confirmation_required` and does not authorize specialized knowledge.
7. Multiple semantic candidates require user selection.
8. No candidate produces `unbound_no_match` and permits design continuation.
9. A conflict with an existing Job or source-model binding is blocking and preserves the existing binding.
10. Family-specific onboarding, profile, and learning tools still require explicit family scope.
11. Selected-family behavior remains backward compatible.
12. Public distribution tests reject real family IDs, user paths, Design Job artifacts, models, lessons, and runtime data.
13. macOS and Windows bootstrap and path tests cover the family-neutral profile.

Run focused unit and MCP boundary tests first, then the complete supported offline suite. Run live PostgreSQL acceptance for the authoritative inventory and familyless lifecycle. No real user design data is used by automated tests.

## 13. Documentation changes

Update the public architecture, bootstrap, Design Job, database deployment, and user workflow documentation to state:

- Product Family is optional for ordinary design;
- automatic family matching is advisory unless the evidence is authoritative;
- specialized knowledge requires a completed binding or explicit authorization;
- PostgreSQL is the identity authority;
- workspace family files are optional runtime configuration, not the authoritative inventory;
- user design data never belongs in the public repository.

## 14. Acceptance criteria

The fix is complete when:

- an unselected family no longer blocks ordinary mechanical design;
- database-only authorized families appear in the authoritative inventory;
- no family is automatically bound from semantic similarity alone;
- authoritative matches can bind automatically with auditable evidence;
- ambiguous matches ask the user;
- no match proceeds with `family_id=null`;
- specialized family knowledge remains inaccessible before binding;
- family-specific tools retain their explicit scope gate;
- synthetic focused tests and the supported offline suite pass;
- public-release scans find no real user design data;
- a separately authorized private runtime acceptance confirms visibility without moving or recreating its authoritative records.

