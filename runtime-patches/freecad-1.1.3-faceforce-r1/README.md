# FreeCAD 1.1.3 native face-force R1

This is a reviewed dependency-maintenance patch candidate, not a FreeCAD addon
or an enabled text-to-FEA feature. It changes native T6 surface integration and
one total force across unique reference faces. Direction/Reversed remain native.
No agent modifies Force values, CLOAD records or generated input files.

`native_faceforce.py` is source payload installed into the standard
`femmesh/meshtools.py` before FreeCAD starts. The deterministic recipe in
`tests/fea_r1_patch.py` requires the exact source digest in `provenance.json`;
`meshtools.patch` is the generated, reviewable upstream diff. The original
module license header and non-T6 weight branches remain intact.

Upstream module attribution: Copyright (c) 2016 Bernd Hahnebach and FreeCAD
contributors. This R1 modification is maintained by this project, not an
official FreeCAD release. `COPYING.LESSER` contains the license text.

The native payload and diff are LGPL-2.0-or-later, matching the modified
upstream file. The surrounding project remains under its own license. Preserve
the upstream license, provenance and modified-source notice when conveying an
integrated FreeCAD runtime. This repository does not distribute such binaries.

The T6 metric is integrated with deterministic Duffy/Gauss-Legendre rules.
A positive quadratic Bernstein bound on the projected Jacobian is sufficient
for orientation; uncertifiable surfaces fail, including some valid extreme
curvatures. No claim is made for arbitrary curved quadrilateral elements or
global solid-mesh validity. Signed corner weights are intentional.

Only new isolated runtime copies may receive this patch. Original installs,
the FreeCAD MCP bridge and SMESH remain unchanged. Runtime manifests must pin
the actual loaded module and dependency identity. W1 private ASCII temporary
directories remain required by the Windows P0 setup. GUI acceptance remains a
separate operator gate and cannot be inferred from automated solver success.
