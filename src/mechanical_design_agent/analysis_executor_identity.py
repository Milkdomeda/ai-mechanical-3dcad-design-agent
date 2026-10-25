"""Own execution sources/resources and interpreter/dependency identities.

This is an immutable run binding, not a claim to attest an entire host OS.
"""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys

from . import secure_fs
from .analysis_types import AnalysisError


def _package_root():return Path(__file__).resolve().parent


def executor_identity():
    root=secure_fs.validate_managed_path(_package_root(),allow_missing_leaf=False).path
    files=[]
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if '__pycache__' in relative.parts or path.suffix in ('.pyc','.pyo'):continue
        located=secure_fs.validate_managed_path(path,allow_missing_leaf=False)
        if located.path.is_dir():continue
        data=secure_fs.read_managed_file(located.path)
        if data.identity!=located.identity or data.link_count!=1:raise AnalysisError('ANALYSIS_EXECUTOR_CHANGED','execution source identity changed')
        files.append({'relative_path':relative.as_posix(),'sha256':data.sha256})
    dependencies={}
    for name in ('ai-mechanical-3dcad-design-agent','jsonschema','mcp','pywin32'):
        try:dependencies[name]=metadata.version(name)
        except metadata.PackageNotFoundError:dependencies[name]=None
    record={'schema_version':'AnalysisExecutorIdentity/v1','python':platform.python_version(),'implementation':platform.python_implementation(),'platform':sys.platform,'architecture':platform.machine(),'dependencies':dependencies,'files':files}
    encoded=json.dumps(record,ensure_ascii=True,allow_nan=False,sort_keys=True,separators=(',',':')).encode()
    return {'record':record,'sha256':hashlib.sha256(encoded).hexdigest(),'bytes':encoded}


def verify_executor_identity(expected):
    actual=executor_identity()
    if actual['sha256']!=expected:raise AnalysisError('ANALYSIS_EXECUTOR_CHANGED','installed execution sources or dependencies changed; preserve this run and create an explicit qualified retry')
    return actual
