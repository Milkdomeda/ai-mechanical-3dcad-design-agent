# P0 manual GUI evidence helper

This is test infrastructure for the reviewed FreeCAD 1.1.3 R1 profile.
It does not enable text-to-FEA, modify the native runtime, or change P0's
numerical thresholds. Existing automated reports remain immutable.

The current preparation and live launcher support macOS. Windows GUI
acceptance remains blocked; GitHub Server evidence is not Windows 11 desktop
acceptance. The pure input comparator is platform-neutral. The macOS source
preparation tests explicitly skip on other platforms rather than claiming
native Windows protection or launch coverage.

Use the reviewed, local preparation manifest and external workspace:

```text
python scripts/fea_gui_acceptance.py prepare --manifest <case-manifest.json> --workspace <new-external-workspace>
python scripts/fea_gui_acceptance.py launch --session <workspace/session.json> --config <pinned-r1-config.json> --native-scratch-root <private-ascii-root>
python scripts/fea_gui_acceptance.py console --session <workspace/session.json>
```

`launch` is a living owner process. It holds W1 and process ownership until
the GUI exits, with a default two-hour bound. Its blank GUI never receives a
CAD document argument. `console` prints the fixed identity-only collector
for the verified GUI Python console; it does not execute it or open a model.
The owner validates native identity and descendant PID before recording
`receipts/launch.json` as ready. Do not use the daily FreeCAD MCP instance.

The operator must open the specified read-only FCStd copy, inspect its native
objects/mesh/results, recompute, select the fresh output directory, and click
the standard `Write .inp File` button. After writing, while the solver panel
is still open, collect the case snapshot:

```text
python scripts/fea_gui_acceptance.py console --session <session.json> --case axial_bar
python scripts/fea_gui_acceptance.py check --session <session.json> --case axial_bar --actions <explicit-human-actions.json>
python scripts/fea_gui_acceptance.py summary --session <session.json> --case-actions <case-to-actions-paths.json>
```

Actions carry the session, actual GUI PID, exact FCStd SHA, explicit human
statement/operator, timezone-aware ordered times and hashed PNG evidence.
The helper checks the native snapshot and new input, never infers manual
work from a successful solver or a file timestamp, and offers no `--passed`
or automatic document restore option. These local receipts document trusted
operator attestations; they are not cryptographic proof of physical mouse
input or a hostile-user sandbox.

The comparator supports one static C3D10 step and the exact native cards
used by the P0 fixtures. Unknown cards/options, missing data, ambiguous
coincident-node renumbering and unreviewed Proxy state fail closed. It
compares full ordered connectivity, material/section/set/DOF/step/output
semantics and signed per-node CLOADs, as well as force, moment and quadratic
virtual work. Files are never rewritten. Source, prepared-session, helper
and evidence hashes are checked again at use time.

`check` writes JSON and Markdown; exit zero means this case passed. `summary`
revalidates every configured case and action file. Even after macOS passes,
its overall P0 status stays blocked and `eligible_for_p1` remains false while
Windows GUI is not executed. Missing evidence remains blocked; no manual
record changes the historical automatic report.

To request shutdown, use `stop --session <session.json>` or close only the
owned GUI normally. Only the living owner terminates its process tree; a
different session's stop request is ignored. W1 contents are cleared only
after owned process exit. Startup/identity failures retain scratch for
diagnosis. No existing installation or user CAD is part of cleanup.
