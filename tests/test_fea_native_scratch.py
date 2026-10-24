import os
from pathlib import Path
import pytest
import importlib


@pytest.fixture(autouse=True, params=['fea_native_scratch', 'mechanical_design_agent.analysis_scratch'])
def scratch_backend(request, monkeypatch):
    module = importlib.import_module(request.param)
    monkeypatch.setitem(globals(), 'native_scratch', module.native_scratch)
    monkeypatch.setitem(globals(), 'verify_native_temp', module.verify_native_temp)
    monkeypatch.setitem(globals(), 'scratch', module)


def test_cache_is_private_unique_and_cleared_after_owner_scope(tmp_path):
    root=tmp_path/'native-cache'
    with native_scratch(root) as owned:
        first=owned.path
        assert first.is_dir() and str(first).isascii()
        (first/'native.tmp').write_bytes(b'synthetic native data')
        verify_native_temp(str(first),owned)
        if os.name=='posix':assert first.stat().st_mode & 0o077 == 0
    assert {p.name for p in first.iterdir()}=={'.native-owner.lock'}
    with native_scratch(root) as another:assert another.path!=first


def test_wrong_or_missing_actual_temp_is_rejected(tmp_path):
    with native_scratch(tmp_path/'cache') as owned:
        for value in ('',str(tmp_path)):
            with pytest.raises(ValueError,match='NATIVE_TEMP_MISMATCH'):verify_native_temp(value,owned)


def test_unicode_root_is_rejected_without_creating_it(tmp_path):
    root=tmp_path/'缓存'
    with pytest.raises(ValueError,match='ASCII'):
        with native_scratch(root):pass
    assert not root.exists()


def test_cache_exception_preserves_owned_evidence(tmp_path):
    with pytest.raises(RuntimeError):
        with native_scratch(tmp_path/'cache') as owned:
            (owned.path/'failure.tmp').write_bytes(b'evidence')
            raise RuntimeError('worker failed')
    assert (owned.path/'failure.tmp').read_bytes()==b'evidence'


def test_untrusted_or_aliased_root_is_rejected(tmp_path):
    root=tmp_path/'cache';root.mkdir(mode=0o700)
    if os.name=='posix':
        root.chmod(0o777)
        with pytest.raises(ValueError,match='PRIVATE'):
            with native_scratch(root):pass
        root.chmod(0o700)
        link=tmp_path/'alias';link.symlink_to(root,target_is_directory=True)
        with pytest.raises(ValueError):
            with native_scratch(link):pass
    else:
        import win32security
        dacl=win32security.ACL();dacl.AddAccessAllowedAce(win32security.ACL_REVISION,0x1f01ff,win32security.CreateWellKnownSid(win32security.WinWorldSid,None))
        win32security.SetNamedSecurityInfo(str(root),win32security.SE_FILE_OBJECT,win32security.DACL_SECURITY_INFORMATION|win32security.PROTECTED_DACL_SECURITY_INFORMATION,None,None,dacl,None)
        with pytest.raises(ValueError,match='PRIVATE'):
            with native_scratch(root):pass


def test_directory_identity_change_never_deletes_replacement(tmp_path):
    if os.name=='nt':
        with native_scratch(tmp_path/'cache') as owned:
            with pytest.raises(OSError):owned.path.rename(owned.path.with_name('moved'))
    else:
        with pytest.raises(ValueError,match='IDENTITY'):
            with native_scratch(tmp_path/'cache') as owned:
                owned.path.rename(owned.path.with_name('moved'))
                owned.path.mkdir(mode=0o700);(owned.path/'foreign').write_text('do not delete')
        assert (owned.path/'foreign').read_text()=='do not delete'


def test_cleanup_entry_swap_preserves_foreign_directory(tmp_path, monkeypatch):
    real=scratch._clear_owned_scratch
    def swap(owned):
        moved=owned.path.with_name('moved-at-cleanup')
        if os.name=='nt':
            with pytest.raises(OSError):owned.path.rename(moved)
        else:
            owned.path.rename(moved);owned.path.mkdir(mode=0o700)
            (owned.path/'foreign').write_text('keep me')
        return real(owned)
    monkeypatch.setattr(scratch,'_clear_owned_scratch',swap)
    if os.name=='nt':
        with native_scratch(tmp_path/'cache'):pass
    else:
        with pytest.raises(ValueError,match='IDENTITY'):
            with native_scratch(tmp_path/'cache') as owned:pass
        assert (owned.path/'foreign').read_text()=='keep me'


@pytest.mark.skipif(os.name!='posix',reason='POSIX permits rename of a pinned directory')
def test_swap_after_cleanup_pin_cannot_delete_replacement(tmp_path, monkeypatch):
    real=scratch.shutil.rmtree
    def swap(name, *, dir_fd):
        owned.path.rename(owned.path.with_name('moved-after-pin'))
        owned.path.mkdir(mode=0o700)
        (owned.path/'foreign').write_text('keep me')
        return real(name,dir_fd=dir_fd)
    swap.avoids_symlink_attacks=real.avoids_symlink_attacks
    monkeypatch.setattr(scratch.shutil,'rmtree',swap)
    with pytest.raises(ValueError,match='IDENTITY'):
        with native_scratch(tmp_path/'cache') as owned:
            (owned.path/'nested').mkdir();(owned.path/'nested'/'native.tmp').write_bytes(b'ours')
    assert (owned.path/'foreign').read_text()=='keep me'


@pytest.mark.skipif(os.name!='posix',reason='Windows pins the lock and its ancestors')
def test_replaced_root_lock_is_detected_before_cleanup(tmp_path):
    with pytest.raises(ValueError,match='IDENTITY'):
        with native_scratch(tmp_path/'cache') as owned:
            lock=owned.path.parent/'.native-root.lock'
            lock.rename(lock.with_suffix('.moved'));lock.write_bytes(b'foreign')
            (owned.path/'evidence').write_text('preserve')
    assert (owned.path/'evidence').read_text()=='preserve'
