import pytest


def test_unknown_native_source_is_rejected_before_patch_generation():
    from fea_r1_patch import patched_source
    with pytest.raises(ValueError,match='R1_BASE_MODULE_DIGEST'):
        patched_source(b'def get_force_obj_face_nodeload_table():\n    return []\n')


def test_known_packaged_line_endings_require_both_raw_and_canonical_identity(monkeypatch):
    import hashlib
    import fea_r1_patch as recipe
    raw=b'# SPDX example\nprint(1)\n';windows=raw.replace(b'\n',b'\r\n')
    sha=lambda value:hashlib.sha256(value).hexdigest()
    monkeypatch.setattr(recipe,'BASE_SHA256',sha(raw))
    monkeypatch.setattr(recipe,'BASE_VARIANTS',{sha(raw):'LF',sha(windows):'CRLF'})
    assert recipe.canonical_baseline(raw)==raw
    assert recipe.canonical_baseline(windows)==raw
    # Identical LF content is insufficient without its exact approved raw hash.
    mixed=raw.replace(b'\n',b'\r\n',1)
    with pytest.raises(ValueError,match='R1_BASE_MODULE_DIGEST'):recipe.canonical_baseline(mixed)
    monkeypatch.setattr(recipe,'BASE_VARIANTS',{sha(windows+b' '):'CRLF'})
    with pytest.raises(ValueError,match='R1_CANONICAL_MODULE_DIGEST'):recipe.canonical_baseline(windows+b' ')
