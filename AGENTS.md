# AI Mechanical 3D CAD Design Agent project instructions

## Project scope

- This repository develops a deterministic mechanical-design agent for FreeCAD. Keep changes focused on mechanical requirements, CAD working copies, FreeCAD integration, standard parts, design validation, engineering knowledge, and release infrastructure.
- The project does not include rendering, video, or assembly-animation subsystems. Do not add media-production dependencies or workflows to the public agent unless the project scope is explicitly expanded and reviewed.
- Use the configured `mechanical-design` MCP for controlled design state and knowledge operations, and the configured `freecad` MCP for interactive inspection and CAD edits in the running FreeCAD GUI.
- The Mechanical Design Agent coordinates engineering state and evidence; it does not replace an engineer's approval or independently certify strength, safety, manufacturability, or standards compliance.

## Mechanical requirements and design discovery

- `superpowers:brainstorming` is an optional external workflow, not a project dependency or mandatory gate. When it is already installed and the user wants structured discovery, recommend it for complex, ambiguous, or new mechanical-design requests before geometry or implementation is proposed.
- Never install or configure Superpowers automatically. Its absence must not block project setup, CAD work, validation, or delivery; perform proportionate structured clarification directly in the conversation when it is unavailable or declined.
- For non-trivial new designs or behavioral changes, obtain user approval of the proposed design before implementation. Keep the ceremony proportional to the task, but do not treat an ambiguous prompt as authorization to invent engineering requirements.
- Establish, as applicable: intended function, operating sequence, units and coordinate system, dimensional envelope, interfaces, loads and duty cycle, motion and travel, materials, environment, applicable standards, standard parts, fits and tolerances, manufacturing constraints, maintenance needs, safety constraints, and acceptance criteria.
- Separate facts supplied by the user from derived values and agent assumptions. Report assumptions explicitly, and stop for direction when a missing choice would materially affect geometry, safety, compatibility, cost, or validation.
- Record explicit numeric requirements with units and validation tolerances. Never invent manufacturing tolerances or silently convert preliminary geometry into release-approved dimensions.

## macOS and Windows support

- Support both macOS and Windows as first-class development and runtime platforms. A feature is not cross-platform complete until its applicable automated tests and FreeCAD integration checks pass on both systems, or the remaining platform limitation is documented as a release blocker.
- Require Python 3.12 or newer. Keep Python code, configuration, package resources, database migrations, CLI behavior, and MCP tool schemas platform-neutral.
- Use `pathlib`, package resources, environment variables, and explicit configuration. Never commit hard-coded user home paths, macOS application paths, Windows drive letters, temporary directories, usernames, or machine-specific service locations.
- Resolve `FreeCADCmd` and FreeCAD GUI executables from explicit configuration first, then from reviewed platform-specific discovery. Current release acceptance targets official FreeCAD 1.1.3; support for another version requires a compatibility run rather than an unverified version claim.
- Do not rely on POSIX-only shell syntax, executable permissions, symlinks, or case-sensitive filenames in cross-platform product code. Use Python or dedicated platform adapters when behavior differs.
- Treat spaces, non-ASCII characters, Windows path separators, file locking, line endings, and UTF-8 encoding as normal supported conditions. Avoid shell interpolation of user-controlled paths.
- Keep PostgreSQL, Neo4j, and other local services bound to loopback interfaces. Use the documented Docker Compose/bootstrap path on both macOS Docker Desktop and Windows Docker Desktop unless a reviewed native deployment is explicitly selected.

## FreeCAD MCP operating rules

- Before reading or creating geometry, call `list_documents` or otherwise confirm that the local FreeCAD MCP bridge is connected.
- Prefer typed tools such as `create_document`, `create_object`, `edit_object`, and `get_object` for simple operations. Use `execute_code` only for bounded mutations that genuinely require the FreeCAD Python API.
- Keep the FreeCAD RPC bridge local. `remote_enabled` must remain `false` unless the user explicitly requests a separately reviewed remote-access configuration.
- Never read unrelated user files, start remote connections, install add-ons, or update pinned vendor sources as a side effect of a CAD task.
- Use `get_view` to inspect meaningful geometry changes. Confirm object names, placements, dimensions, shape state, and recompute results before advancing the design lifecycle.
- The exact external FreeCAD GUI MCP identity is an integration boundary documented in `docs/FREECAD_GUI_MCP_INTEGRATION.md`; it is not vendored by the public repository. Change that accepted identity only in a dedicated maintenance change with provenance, license, security, compatibility, and regression review.

## Product Job routing

- Treat a new design, existing model, resume, Product Family onboarding, or Design Lessons request as a product operation. A supplied Job UUID or display ID calls `design_job_get` for authorized state; do not treat that identity as a `design_job_resolve` query.
- An explicitly independent demand calls `design_job_create` directly, even when similar Jobs exist. For continue/resume without an explicit ID, call `design_job_resolve` only for `active` and `blocked`: reuse the one same design candidate, return multiple candidates and stop, and with zero candidates clarify independent/new intent before creating. Never select or create from ambiguity.
- New, existing, and resumed mechanical design use `mechanical_design`; Product Family intake/onboarding uses `product_family_onboarding`. Product Family review, knowledge, and database publication reuse that original onboarding Job. A Design Lesson uses only its originating `mechanical_design` Job; stop if the origin is missing or ambiguous and never create a replacement or onboarding Job.
- Use `design_job_list`, `design_job_get`, `design_job_close`, or `design_job_reopen` only through the configured Mechanical Design MCP, never an arbitrary filesystem path, as Job identity.
- Product work uses its Job workspace. Do not create a Git branch or Git worktree. Software changes to agent implementation, schemas, migrations, or tests may use the normal Git workflow. Explicitly split mixed product/software requests before acting.
- Source snapshots, working copies, validation evidence, delivery records, Product Family knowledge, and Design Lessons must remain bound to their authoritative Job and expected revision. Preserve the resolved Job ID and stop when any required governed binding is unavailable or stale.
- Product Family and Design Lesson database writes are Job operations. Changing their implementation or schema is software development.
- Use `design_job_close` or `design_job_reopen` only with the exact current revision, reason, phase, and the user's matching confirmation. Do not provide confirmation on the user's behalf.

## Managed model and change lifecycle

- Classify every managed model as `existing_model` or `new_design`.
- For an existing STEP or FCStd model, create the working copy through the Mechanical Design Agent and do not edit it unless it is uniquely bound to `source_model_revision_id`. Treat the source model as read-only.
- For a new design, create and register the controlled working copy before substantive modeling. Keep FCStd as the source of truth for designed parts and assemblies.
- Treat Product Family resolution, knowledge retrieval, standard-parts assessment, and assembly assessment as independent engineering obligations, not a fixed A → B → C pipeline. Every obligation needs an explicit conclusion, but order and depth must remain proportional to the approved scope.
- Put one strict `EngineeringScope/v1` snapshot in the first Design Intent. Record the component plan, sourcing class, deliverable kind, motion, and assembly interfaces; bind screening decisions to its SHA-256 with `design_job_obligations_resolve`.
- A simple custom single part may close standard parts and assembly as `not_applicable`. A triggered standard component must be searched or explicitly excepted with evidence. An assembly may use `required_pending` to begin approved modeling, but delivery requires `required_passed` and same-revision evidence.
- Never use `not_applicable` to bypass a scope trigger. If the scope changes, treat earlier standard-parts and assembly conclusions as stale and resolve the affected obligations again.
- Before proposing or applying a CAD change, call `design_knowledge_retrieve` for the applicable organization, design group, family, model, and working copy. A completed receipt with no matches is acceptable; a missing or `not_executed` receipt is not.
- Keep proposals, approvals, applied changes, validation evidence, and delivery records bound to the exact working-copy revision and file hash. Do not reuse stale evidence after any geometry or metadata change.
- When a new proposal replaces or abandons an unapplied proposal, close the old change with `design_change_close` as `superseded` or `cancelled`. Never mark a proposal as applied merely to clear a delivery gate.
- Before the first substantive CAD mutation, record a complete Design Intent proposal with an Approval Envelope draft and obtain the user's explicit approval. The user may respond with the simple canonical `批准` or `修改方案`; keep internal change-set and envelope IDs in governed tool arguments and audit records rather than requiring the user to copy UUIDs.
- After approval, record every CAD iteration with a complete semantic-impact declaration. Parameter tuning, feature details, clearance and interference repairs, geometry-validity repairs, validation-driven repairs, and implementation refinements may proceed autonomously only when `design_change_record` classifies them inside the active Approval Envelope.
- Before every substantive CAD mutation, call `design_change_mutation_authorize` for the exact change set. Do not mutate when approval is absent, the envelope is stale, a material design-intent boundary is exceeded, or classification is incomplete or ambiguous.
- Mechanism or architecture changes, key-interface or approved-function changes, exceeded explicit constraints, manufacturing-process or specified-material changes, new standard-part categories, and removed validation requirements require a successor Design Intent proposal and new user approval. Do not use percentage-only thresholds to decide materiality.
- Do not call an approval or delivery operation on the user's behalf. Approval phrases must come from the user and match the operation being approved.
- When the user confirms with “模型设计确认”, immediately summarize the material design lessons and call `design_confirmation_record`, even if another delivery gate is still pending. Continue the governed review and publication flow when the remaining gates become ready.
- Model confirmation and Design Lesson publication are separate decisions. `模型设计确认` never approves or publishes a Design Lesson.
- After delivery, call `design_lesson_review_context`, retain only material, generalizable, evidence-backed candidates, and prepare one immutable Review Card. Display the complete current card before requesting a decision. Treat requested edits or filtering as review feedback; prepare a replacement card and supersede the prior pending card.
- For a publishable card, request only `确认发布设计经验`, keep the active Review ID and Job binding internal, and call `design_lesson_review_publish`. Report completion only for public status `published`. If status is `publishing`, retry the existing status path without requesting another confirmation.
- If no candidate survives engineering review, display the complete immutable screening card, request only `确认无可发布设计经验`, and call `design_lesson_review_no_publish`. The terminal `reviewed-no-publishable-lesson` outcome must create no shared Design Lesson.

## Standard parts and provenance

- For gears, worms, bolts, screws, nuts, washers, bearings, keys, standard flanges, pins, structural profiles, guide rails, rollers, and comparable catalog components, use `freecad-standard-parts` and `step-parts` before modeling geometry.
- Select providers in this order: FreeCAD Fasteners Workbench, FreeCAD Gears, STEP.parts, then the verified configured FCStd/STEP catalog. Treat provider availability as configuration, not as a machine-specific constant.
- Preserve provider, manufacturer, standard, nominal size, source URL or catalog identity, part ID, version or source commit, license information, and SHA-256 metadata in the FreeCAD object and BOM when applicable.
- If search results are ambiguous, present candidates. If the configured providers contain no suitable part, record the attempted providers and queries and stop for user direction. Network or DNS failure is inconclusive and must not be described as a catalog miss.
- Never create or silently substitute custom geometry for a standard part unless the user explicitly requests or approves that exception. A machined derivative may be created only when it retains the base component's provenance and clearly records the derived operation.
- Keep standard parts as separate reusable objects unless the manufacturing design explicitly requires a fused solid. Verify placements, quantities, interfaces, and BOM agreement.

## Mandatory model validation and delivery gates

- After AI creation or any visible modification of an FCStd model, assembly, or imported STEP standard part, use `freecad-model-validation` before reporting the model complete.
- Create an explicit validation specification from the approved requirements. Every numeric requirement must have units and a stated validation tolerance.
- Bind validation evidence to the exact same-revision FCStd or STEP SHA-256. Any subsequent change invalidates the prior completion evidence.
- Validate recompute state, object and link requirements, shape validity, solid count, positive volume, dimensions, placements, standard-part provenance, BOM consistency, required interfaces, declared interference checks, and controlled compressible overlaps as applicable.
- Automatically detected fasteners require complete installation contracts and passing set-combination, axis, compatible-hole containment, axial position, bearing-contact, thread/clearance classification, and unintended-interference checks. Do not replace the authoritative detected inventory with a smaller hand-authored list.
- Use `get_view` and inspect the generated validation image in addition to machine-readable JSON and Markdown results. A geometry check does not replace visual review.
- Any missing artifact, stale hash, invalid model, failed mandatory check, incomplete standard-part provenance, unexplained geometry, or unapproved engineering assumption blocks completion. Repair and rerun, or report the task as blocked.
- A passed validation report is evidence of the checks that ran. It is not an engineering certification, finite-element assessment, manufacturing release, or legal declaration of standards compliance.

## Data and engineering-knowledge architecture

- PostgreSQL is the authoritative store for identities, revisions, lifecycle events, approvals, validation bindings, design lessons, and retrieval state.
- Neo4j is a rebuildable relationship projection, never the authority. Projection failure must not overwrite or contradict PostgreSQL state; use the outbox and idempotent rebuild workflow.
- Keep database schemas, migrations, package-owned configuration, and installed resources deterministic and portable. Never require a developer's source checkout after installation.
- Design knowledge must remain scoped by organization, design group, product family, model, applicability conditions, and authorization. Do not expose source-family incident details outside an explicitly authorized scope.
- Publish design lessons only through the governed review and approval workflow. Preserve immutable evidence, supersession/revocation state, and retrieval verification.

## Development priorities

- Prioritize reliable macOS and Windows installation, FreeCAD/FreeCADCmd discovery, local MCP connectivity, Docker database bootstrap, clean upgrades, and reproducible release acceptance.
- Continue improving deterministic working-copy management, same-revision change and evidence binding, crash-safe locking, retry behavior, and clear recovery diagnostics.
- Extend standard-part providers through portable configuration and truthful provenance rather than hard-coded product examples or machine-local catalogs.
- Improve validation coverage for assemblies, joints, fasteners, fits, interfaces, interference, derived components, and delivery completeness without weakening mandatory gates.
- Improve retrieval quality, applicability filtering, lesson review, publication, supersession, revocation, and projection recovery while keeping PostgreSQL authoritative.
- Preserve backward compatibility for documented CLI commands, MCP tool schemas, configuration files, database migrations, and installed package resources. When a breaking change is necessary, version the contract and document migration steps.
- Keep security boundaries explicit: loopback-only services, no telemetry by default, no hidden remote access, no secret material in logs or artifacts, and no execution of untrusted CAD-embedded code.

## Testing and release requirements

- Add or update focused tests for every behavioral change. Use unit tests for deterministic logic and bounded integration tests for FreeCAD, PostgreSQL, Neo4j, packaging, and MCP boundaries.
- Cross-platform code changes must cover macOS and Windows path, process, encoding, locking, discovery, and installation behavior where applicable.
- Run the relevant focused tests first, then the complete supported offline suite before reporting implementation complete. Run live database and interactive FreeCAD acceptance when the change affects those boundaries.
- Verify wheel and source distribution contents, clean installation, CLI and MCP entry points, package resources, migrations, default configuration bootstrap, public release allowlists, and relative documentation links before a release.
- Do not weaken, skip, or relabel a failing test or release gate merely to obtain a passing result. Record expected environmental skips precisely and keep unsupported claims out of public documentation.

## Repository and public-release boundary

- Git tracks reusable Agent capabilities: source code, tests, migrations, schemas, portable configuration templates, project-owned skills, documentation, and reviewed project-wide rules.
- Keep generated FCStd/STEP models, drawings, renders, screenshots, BOM exports, validation outputs, runtime databases, knowledge contents, caches, credentials, machine-local configuration, and customer or project-specific design evidence out of the public repository.
- Store governed mechanical-design deliverables inside the originating ignored Job directory; use `output/` only for explicitly requested non-Job local exports. Do not commit a design artifact merely because it is final or approved.
- Store project-owned distributable skills under `.agents/skills/`. Keep temporary skill build directories and installed user-level skill copies out of Git.
- Before publishing, scan for absolute paths, usernames, hostnames, secrets, private source identities, generated artifacts, and unapproved third-party content. Preserve license and provenance notices for every distributed dependency or asset.
- Do not push, publish a package, move a release tag, or open a pull request unless the user explicitly requests that external action.
