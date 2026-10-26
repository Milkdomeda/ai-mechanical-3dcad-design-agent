"""Closed, hash-pinned R2 patch; never an in-place installation operation."""
import hashlib
import json

from .analysis_types import AnalysisError
from .package_resources import analysis_resources_directory

PROFILE = 'freecad-1.1.3-fem-faceforce-r1-nodal-rf-r2'
BASE_SHA256 = 'f4a9c2e60671293de4c5781e02336121839c2dc6e4f59ea1037706be72ca4b1d'
PATCHED_SHA256 = '5f20da802c3e3f37161b0925194ad4952315f5d6887f334d2ecd2d588d5477c2'
PATCH_SHA256 = '5b6bac5aab9ae6515e15d4b35149f6a312032eed8c9486da692b01b51c9c22df'
LICENSE_SHA256 = '1dfab741fe12676a0a7e8166ad4a74670cb536ddff56b84b36a48fab28a1a110'
ORIGINAL_LINE = b'f.write("*NODE PRINT, NSET={}, TOTALS=ONLY\\n".format(femobj["Object"].Name))'
PATCHED_LINE = ORIGINAL_LINE.replace(b'TOTALS=ONLY', b'TOTALS=YES')


def patch_assets():
    with analysis_resources_directory() as resources:
        root = resources/'runtime-patches/nodal-rf-r2'
        patch, license = (root/'nodal-rf-r2.patch').read_bytes(), (root/'COPYING.LESSER').read_bytes()
        provenance = json.loads((root/'provenance.json').read_text(encoding='utf-8'))
    if hashlib.sha256(patch).hexdigest() != PATCH_SHA256 or hashlib.sha256(license).hexdigest() != LICENSE_SHA256 or provenance['original_lf_sha256'] != BASE_SHA256 or provenance['proposed_lf_sha256'] != PATCHED_SHA256 or provenance['profile'] != PROFILE:
        raise AnalysisError('FEA_PATCH_ASSET_CHANGED', 'packaged native patch or provenance changed')
    return {'patch': patch, 'license': license, 'provenance': provenance}


def patch_output_source(source):
    if type(source) is not bytes:
        raise AnalysisError('FEA_PATCH_BASE_CHANGED', 'native output source must be pinned bytes')
    original = source.replace(b'\r\n', b'\n')
    if hashlib.sha256(original).hexdigest() != BASE_SHA256:
        raise AnalysisError('FEA_PATCH_BASE_CHANGED', 'only the exact reviewed FreeCAD 1.1.3 output writer is supported')
    replacement = original.replace(ORIGINAL_LINE, PATCHED_LINE, 1)
    if hashlib.sha256(replacement).hexdigest() != PATCHED_SHA256:
        raise AnalysisError('FEA_PATCH_DIFF_CHANGED', 'output replacement differs from the approved single line')
    return replacement
