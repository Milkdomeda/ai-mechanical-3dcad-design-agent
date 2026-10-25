"""Private W1 native work directories with pinned ownership and safe cleanup."""
from contextlib import contextmanager
import os
import hashlib
import shutil
import stat
from pathlib import Path
import uuid
import time
from mechanical_design_agent import secure_fs


def _windows_principals():
    import win32api
    import win32con
    import win32security as security
    token=security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
    try:user=security.GetTokenInformation(token,security.TokenUser)[0]
    finally:token.Close()
    return security,[user,security.CreateWellKnownSid(security.WinLocalSystemSid,None),security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid,None)]


def _create_private(path):
    if os.name=='nt':
        import pywintypes
        import win32file
        security,sids=_windows_principals()
        strings=[security.ConvertSidToStringSid(sid) for sid in sids]
        sddl='O:'+strings[0]+'D:P'+''.join('(A;OICI;FA;;;'+sid+')' for sid in strings)
        attributes=pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR=security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl,security.SDDL_REVISION_1)
        win32file.CreateDirectory(str(path),attributes)
    else:path.mkdir(mode=0o700)


def _require_private(path):
    if os.name=='nt':
        security,sids=_windows_principals()
        allowed={security.ConvertSidToStringSid(sid) for sid in sids}
        sd=security.GetNamedSecurityInfo(str(path),security.SE_FILE_OBJECT,security.OWNER_SECURITY_INFORMATION|security.DACL_SECURITY_INFORMATION)
        acl=sd.GetSecurityDescriptorDacl()
        if not sd.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED:raise ValueError('NATIVE_SCRATCH_NOT_PRIVATE')
        if security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner()) not in allowed or acl is None or not acl.GetAceCount():raise ValueError('NATIVE_SCRATCH_NOT_PRIVATE')
        for i in range(acl.GetAceCount()):
            ace=acl.GetAce(i)
            if ace[0][0]!=security.ACCESS_ALLOWED_ACE_TYPE or security.ConvertSidToStringSid(ace[-1]) not in allowed:raise ValueError('NATIVE_SCRATCH_NOT_PRIVATE')
    else:
        facts=path.stat()
        if facts.st_uid!=os.geteuid() or facts.st_mode & 0o077:raise ValueError('NATIVE_SCRATCH_NOT_PRIVATE')


def _assert_identity(owned):
    if secure_fs.validate_managed_path(owned.path,allow_missing_leaf=False).identity!=owned.identity:raise ValueError('NATIVE_SCRATCH_IDENTITY_CHANGED')


def _clear_owned_scratch(owned):
    """Clear contents through a pinned directory; retain its lock/empty shell.

    POSIX cannot atomically compare an inode and unlink its directory name.
    Retaining the shell avoids deleting a replacement after a final check.
    Windows's enclosing owner lock pins the directory against rename/delete.
    """
    if os.name=='nt':
        _assert_identity(owned)
        for child in owned.path.iterdir():
            if child.name=='.native-owner.lock':continue
            checked=secure_fs.validate_managed_path(child,allow_missing_leaf=False)
            if checked.path.is_dir():secure_fs.remove_owned_tree(checked.path,expected_parent=owned.path,label='native scratch contents')
            else:checked.path.unlink()
        _assert_identity(owned)
        return
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    descriptor=os.open(owned.path,flags)
    try:
        facts=os.fstat(descriptor)
        if secure_fs.FileIdentity(facts.st_dev,facts.st_ino)!=owned.identity:raise ValueError('NATIVE_SCRATCH_IDENTITY_CHANGED')
        if not shutil.rmtree.avoids_symlink_attacks:raise ValueError('NATIVE_SCRATCH_SAFE_CLEANUP_UNAVAILABLE')
        for name in os.listdir(descriptor):
            if name=='.native-owner.lock':continue
            facts=os.stat(name,dir_fd=descriptor,follow_symlinks=False)
            if stat.S_ISDIR(facts.st_mode):shutil.rmtree(name,dir_fd=descriptor)
            else:os.unlink(name,dir_fd=descriptor)
        os.fsync(descriptor)
        _assert_identity(owned)
    finally:os.close(descriptor)


def verify_native_temp(actual, owned):
    try:
        if not isinstance(actual,str) or not actual:raise ValueError('missing native temp')
        _assert_identity(owned)
        if secure_fs.validate_managed_path(Path(actual),allow_missing_leaf=False).identity!=owned.identity:raise ValueError('wrong native temp')
    except (OSError,ValueError) as exc:raise ValueError('NATIVE_TEMP_MISMATCH') from exc


@contextmanager
def _owner_lock(path,checkpoint):
    if checkpoint is None:
        with secure_fs.exclusive_file_lock(path):yield
        return
    while True:
        checkpoint()
        with secure_fs.try_exclusive_file_lock(path) as acquired:
            if acquired:
                checkpoint();yield
                return
        time.sleep(.05)


@contextmanager
def native_scratch(root: Path,*,checkpoint=None):
    root=Path(root)
    if not root.is_absolute() or not str(root).isascii():raise ValueError('NATIVE_SCRATCH_REQUIRES_ABSOLUTE_ASCII_ROOT')
    repository=Path(__file__).resolve().parent
    if root.is_relative_to(repository):raise ValueError('NATIVE_SCRATCH_MUST_BE_OUTSIDE_SOURCE')
    # Use the existing backend's ancestor/reparse checks, creation lock and
    # lifetime lock. Windows pins directory handles against rename/deletion.
    bootstrap=root.parent/('.native-bootstrap-'+hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:24]+'.lock')
    with _owner_lock(bootstrap,checkpoint):
        checked=secure_fs.validate_managed_path(root,allow_missing_leaf=True)
        source=secure_fs.validate_managed_path(repository,allow_missing_leaf=False)
        if checked.path.is_relative_to(source.path):raise ValueError('NATIVE_SCRATCH_MUST_BE_OUTSIDE_SOURCE')
        if checked.identity is None:_create_private(checked.path)
        pinned=secure_fs.validate_managed_path(root,allow_missing_leaf=False)
        if not pinned.path.is_dir():raise ValueError('NATIVE_SCRATCH_NOT_DIRECTORY')
        if not str(pinned.path).isascii():raise ValueError('NATIVE_SCRATCH_REQUIRES_ASCII_ROOT')
        _require_private(pinned.path)
    with _owner_lock(pinned.path/'.native-root.lock',checkpoint):
        root_lock=secure_fs.validate_managed_path(pinned.path/'.native-root.lock',allow_missing_leaf=False)
        _assert_identity(pinned)
        child=pinned.path/uuid.uuid4().hex
        _create_private(child)
        owned=secure_fs.validate_managed_path(child,allow_missing_leaf=False)
        with _owner_lock(owned.path/'.native-owner.lock',checkpoint):
            owner_lock=secure_fs.validate_managed_path(owned.path/'.native-owner.lock',allow_missing_leaf=False)
            yield owned
            _assert_identity(pinned);_assert_identity(owned)
            _assert_identity(root_lock);_assert_identity(owner_lock)
            # Only after the caller's owned process scope finished normally.
            # Keep the owner lock held while clearing; exceptions preserve data.
            _clear_owned_scratch(owned)
