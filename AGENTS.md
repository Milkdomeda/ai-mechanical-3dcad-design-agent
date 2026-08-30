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

## Default lightweight design workflow

- Ordinary new designs, existing-model edits, and resumptions use the default `design` MCP profile and one filesystem session under `designs/<design-id>/`. Do not create a Design Job, Working Copy record, Change Set, Approval Envelope, mutation authorization, engineering-obligation record, or delivery approval.
- Classify the model as `new_design` or `existing_model`, clarify material geometry-affecting unknowns, present a short proposal, and obtain one natural-language approval. Treat clear Chinese or English approval as `APPROVE`, clear rejection as `REJECT`, and conditional, contradictory, or unknown language as `UNCLEAR`; never demand one canonical phrase.
- After `APPROVE`, call `design_start`. The returned `model.FCStd` is the CAD source of truth. Existing FCStd/STEP inputs are snapshotted read-only; never edit the user's source file.
- Call `design_knowledge_retrieve` before substantive modeling. Use applicable matches. A no-match or unavailable backend is nonblocking unless the user explicitly required a named Product Family or Design Lesson.
- After approval, perform CAD edits and validation-driven corrections directly. Do not request another approval for parameter tuning, clearance repair, feature detail, or other implementation work inside the approved direction.
- A changed function, mechanism, key interface, specified material, manufacturing process, or explicit user constraint requires conversational clarification and a new design-direction approval, not an enterprise lifecycle.
- Validate the exact current FCStd, visually inspect it, correct failures automatically when safe, and call `design_record_result`. Only exact-hash passed evidence may set the session to `completed`.
- The optional `governed` profile retains the historical Job/change/approval workflow for explicitly requested audit-heavy or multi-user work. It is never an ordinary-design prerequisite.
- Product Family onboarding and Design Lesson publication remain durable knowledge workflows; they are not automatic post-design gates.
- Model confirmation and Design Lesson publication are separate decisions.

## Standard parts and provenance

- For gears, worms, bolts, screws, nuts, washers, bearings, keys, standard flanges, pins, structural profiles, guide rails, rollers, and comparable catalog components, use `freecad-standard-parts` and `step-parts` before modeling geometry.
- Select providers in this order: FreeCAD Fasteners Workbench, FreeCAD Gears, STEP.parts, then the verified configured FCStd/STEP catalog. Treat provider availability as configuration, not as a machine-specific constant.
- Preserve provider, manufacturer, standard, nominal size, source URL or catalog identity, part ID, version or source commit, license information, and SHA-256 metadata in the FreeCAD object and BOM when applicable.
- If search results are ambiguous, present candidates. If the configured providers contain no suitable part, record the attempted providers and queries and stop for user direction. Network or DNS failure is inconclusive and must not be described as a catalog miss.
- Never create or silently substitute custom geometry for a standard part unless the user explicitly requests or approves that exception. A machined derivative may be created only when it retains the base component's provenance and clearly records the derived operation.
- Keep standard parts as separate reusable objects unless the manufacturing design explicitly requires a fused solid. Verify placements, quantities, interfaces, and BOM agreement.

## Mandatory model validation and result recording

- After AI creation or any visible modification of an FCStd model, assembly, or imported STEP standard part, use `freecad-model-validation` before reporting the model complete.
- Create an explicit validation specification from the approved requirements. Every numeric requirement must have units and a stated validation tolerance.
- Bind validation evidence to the exact same-revision FCStd or STEP SHA-256. Any subsequent change invalidates the prior completion evidence.
- Validate recompute state, object and link requirements, shape validity, solid count, positive volume, dimensions, placements, standard-part provenance, BOM consistency, required interfaces, declared interference checks, and controlled compressible overlaps as applicable.
- Automatically detected fasteners require complete installation contracts and passing set-combination, axis, compatible-hole containment, axial position, bearing-contact, thread/clearance classification, and unintended-interference checks. Do not replace the authoritative detected inventory with a smaller hand-authored list.
- Use `get_view` and inspect the generated validation image in addition to machine-readable JSON and Markdown results. A geometry check does not replace visual review.
- Any missing artifact, stale hash, invalid model, failed mandatory check, incomplete standard-part provenance, unexplained geometry, or unapproved engineering assumption blocks completion. Repair and rerun, or report the task as blocked.
- A passed validation report is evidence of the checks that ran. It is not an engineering certification, finite-element assessment, manufacturing release, or legal declaration of standards compliance.

## Data and engineering-knowledge architecture

- Filesystem JSON is authoritative for a single lightweight design session. PostgreSQL is not a prerequisite for CAD mutation.
- PostgreSQL remains authoritative for durable Product Family Knowledge, Design Lessons, reviews, searchable assertions, and the optional governed lifecycle.
- Neo4j is a rebuildable relationship projection, never the authority. Projection failure must not overwrite or contradict PostgreSQL state; use the outbox and idempotent rebuild workflow.
- Keep database schemas, migrations, package-owned configuration, and installed resources deterministic and portable. Never require a developer's source checkout after installation.
- Design knowledge must remain scoped by organization, design group, product family, model, applicability conditions, and authorization. Do not expose source-family incident details outside an explicitly authorized scope.
- Publish Design Lessons only when that separate durable workflow is explicitly requested. Preserve immutable evidence, supersession/revocation state, and retrieval verification.

## Development priorities

- Prioritize reliable macOS and Windows installation, FreeCAD/FreeCADCmd discovery, local MCP connectivity, Docker database bootstrap, clean upgrades, and reproducible release acceptance.
- Continue improving lightweight-session safety, exact-hash evidence binding, crash-safe locking, retry behavior, and clear recovery diagnostics.
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
- Store ordinary design artifacts inside the ignored `designs/<design-id>/` session; governed compatibility artifacts remain in their originating ignored Job directory. Do not commit a design artifact merely because it is final or approved.
- Store project-owned distributable skills under `.agents/skills/`. Keep temporary skill build directories and installed user-level skill copies out of Git.
- Before publishing, scan for absolute paths, usernames, hostnames, secrets, private source identities, generated artifacts, and unapproved third-party content. Preserve license and provenance notices for every distributed dependency or asset.
- Do not push, publish a package, move a release tag, or open a pull request unless the user explicitly requests that external action.
