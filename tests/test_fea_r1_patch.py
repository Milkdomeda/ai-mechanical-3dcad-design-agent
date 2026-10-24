import pytest


def test_unknown_native_source_is_rejected_before_patch_generation():
    from fea_r1_patch import patched_source
    with pytest.raises(ValueError,match='R1_BASE_MODULE_DIGEST'):
        patched_source(b'def get_force_obj_face_nodeload_table():\n    return []\n')
