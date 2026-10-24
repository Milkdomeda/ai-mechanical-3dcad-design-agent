import os

import pytest


def test_worker_owner_lock_is_nonblocking_and_has_one_owner(tmp_path):
    from mechanical_design_agent.secure_fs import try_exclusive_file_lock
    path = tmp_path / 'worker.lock'
    with try_exclusive_file_lock(path) as first:
        assert first is True
        with try_exclusive_file_lock(path) as second:
            assert second is False
    with try_exclusive_file_lock(path) as third:
        assert third is True


def test_worker_owner_lock_releases_after_exception(tmp_path):
    from mechanical_design_agent.secure_fs import try_exclusive_file_lock
    path = tmp_path / 'worker.lock'
    with pytest.raises(RuntimeError, match='synthetic owner crash'):
        with try_exclusive_file_lock(path) as acquired:
            assert acquired
            raise RuntimeError('synthetic owner crash')
    with try_exclusive_file_lock(path) as acquired:
        assert acquired


@pytest.mark.skipif(os.name != 'posix', reason='POSIX symlink fixture; NTFS junction/reparse checks use native Windows suite')
def test_owner_lock_rejects_symlink_and_multiple_names(tmp_path):
    from mechanical_design_agent.secure_fs import try_exclusive_file_lock
    target = tmp_path / 'target'; target.write_bytes(b'')
    alias = tmp_path / 'alias'; alias.symlink_to(target)
    with pytest.raises(ValueError):
        with try_exclusive_file_lock(alias): pass
    alias.unlink(); os.link(target, alias)
    with pytest.raises(ValueError):
        with try_exclusive_file_lock(alias): pass
