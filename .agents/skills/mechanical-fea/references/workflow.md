# Installed analysis workflow

Run `mechanical-design analysis --help` and each subcommand's `--help` for the
current argument contract. Runtime and design state belong to the selected
workspace. Schemas use `AnalysisPrepareRequest/v1` / `AnalysisSpec/v1` and
normalized N, mm, s, tonne and MPa; quantity values are decimal strings with
explicit units. Obtain the closed schema using `analysis schema --name
prepare-request` and related names. User physical values, selectors, evidence,
ROI policies and budgets are data; there is no caller-code execution field.

The request contains `source_path`, `expected_source_sha256`, `draft_spec`,
`geometry_request`, `region_selectors` and a scoped `knowledge` query. The
geometry request identifies one included object, explicit exclusions,
optional bounded axis partitions and a volume tolerance. Native geometry is
validated before surface selectors are bound. The draft includes material,
frame-mass basis if relevant, cases, C3D10 mesh policy, at least three declared
full-span rays, budgets, equilibrium/convergence tolerances, evaluation ROIs
and applicability bounds, criteria, assumptions and unresolved questions.
Do not put source paths into public code or examples.

## One-time explicit setup

Initialize or select a Design Job Workspace through the existing CAD workflow.
Use `analysis setup --workspace <workspace> --source-root <official-distribution>
--destination <new-isolated-directory> --native-scratch-root <private-ASCII-root>`.
The source must be the reviewed official FreeCAD 1.1.3 distribution with bundled
Gmsh 4.15.0 and qualified CalculiX 2.22/2.23. Explicit executable overrides must
remain inside that distribution. Setup clones and verifies the distribution,
applies only reviewed R1/R2 source patches and records provenance. It never
repairs, cleans or patches the source installation. On Windows CI,
`--software-opengl` selects only the hash-pinned official bundled Mesa driver;
it does not qualify a hardware GPU.

Run `analysis doctor --workspace <workspace> --qualify`. This runs fixed
synthetic benchmarks and actual native GUI checks using installed resources.
It does not approve any customer material, load case or design. A changed
package, interpreter/dependency identity, configuration or native runtime
invalidates activation. `analysis doctor` and `analysis capabilities` check
existing qualification without starting native programs. No implicit downloads
or installation of FreeCAD add-ons occur.

## Shared CLI and MCP operations

CLI commands accept `--workspace`; identity-bound commands take `--design-id`
and `--analysis-id`. `prepare --request <UTF8-json-file>` produces a review.
`approve --spec-sha256 <hash> --decision <human-text> --expected-revision <n>`
freezes only the reviewed physical specification. `run` additionally takes an
`--idempotency-key`. `status` accepts `--run-id` and an event `--cursor`.
`cancel` requires a run, reason and expected revision.

`report --run-id <id>` is read-only. After actual image inspection,
`report --action finalize_visual --evidence-refs <UTF8-json-string-array-file>
--expected-revision <n>` publishes reports. The agent creates that file from the
returned registered references; the user does not author evidence receipts.
`confirm` requires the exact manifest SHA, human decision and state revision.

`open --run-id <id> --case-id <declared-case>` starts a separate native window
and returns a session identifier. `open-status --session-id <id>` reports
readiness and the retained working-document path. `close --session-id <id>`
requests only that viewer's closure; it does not kill a process by an arbitrary
PID. Closing an interactive session does not finalize or accept a report.

## Recovery boundaries

Keep original logs and failed output directories. A source, tool, runtime or
executor hash mismatch blocks reuse. Requalification is explicit; it does not
rewrite prior run evidence. Report publication retries reuse sealed numerical
results without rerunning the solver. A failed/timeout/cancelled/interrupted
solve needs a new explicit retry run. Never relabel a failed test or an
unexecuted platform as passed.

For a presentation-only revision of a completed run, call `analysis_report`
with `action=rebuild_figures` and the current state revision. It reconstructs
registered figures from the same sealed solver outputs and returns a fresh
visual reference list. Inspect every new image, then finalize that exact list.
This does not rerun the solver. The new manifest has separate acceptance.
