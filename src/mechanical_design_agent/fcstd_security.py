from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
import stat
import unicodedata
import xml.etree.ElementTree as ET
import zipfile


_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ENTRY_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_ENTRY_COUNT = 4096
_MAX_COMPRESSION_RATIO = 500
_SCRIPTED_TYPE_MARKERS = (
    "featurepython",
    "propertypythonobject",
    "pythonobject",
    "scriptedobject",
)
_SCRIPTED_PROPERTY_NAMES = frozenset(
    {"proxy", "pythoncode", "pythonscript", "script", "onrestore"}
)
_EXECUTABLE_ENTRY_SUFFIXES = frozenset(
    {".py", ".pyc", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bat", ".cmd", ".ps1", ".js"}
)


class FcstdSecurityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FcstdStaticEvidence:
    entry_count: int
    total_uncompressed_bytes: int
    document_schema_version: str


def _fail(code: str, message: str) -> FcstdSecurityError:
    return FcstdSecurityError(code, message)


def _portable_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def _validate_entry(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise _fail("FCSTD_ARCHIVE_ENCRYPTED", "encrypted FCStd entries are unsupported")
    if "\\" in info.filename or "\x00" in info.filename:
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd entry path is unsafe")
    path = PurePosixPath(info.filename)
    raw_parts = info.filename.split("/")
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in raw_parts[:-1])
        or raw_parts[0].endswith(":")
        or any(ord(character) < 32 for character in info.filename)
    ):
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd entry path is unsafe")
    if path.suffix.casefold() in _EXECUTABLE_ENTRY_SUFFIXES:
        raise _fail(
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
            "executable FCStd archive entries are unsupported",
        )
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "FCStd compression method is unsupported")
    unix_mode = info.external_attr >> 16
    if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd archive links are unsupported")
    if info.file_size < 0 or info.compress_size < 0 or info.file_size > _MAX_ENTRY_BYTES:
        raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd entry size exceeds the safety limit")
    if info.file_size and info.compress_size == 0:
        raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd entry compression metadata is unsafe")
    if info.compress_size and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
        raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd compression ratio exceeds the safety limit")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _inspect_document_xml(contents: bytes) -> str:
    folded_prefix = contents[:4096].lower()
    if b"<!doctype" in folded_prefix or b"<!entity" in folded_prefix:
        raise _fail("FCSTD_DOCUMENT_XML_INVALID", "Document.xml declarations are unsupported")
    try:
        root = ET.fromstring(contents)
    except (ET.ParseError, ValueError) as exc:
        raise _fail("FCSTD_DOCUMENT_XML_INVALID", "Document.xml is malformed") from exc
    if _local_name(root.tag) != "Document":
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "Document.xml has an unsupported root")
    schema_version = root.attrib.get("SchemaVersion")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "Document.xml has no schema version")
    for element in root.iter():
        tag = _local_name(element.tag).casefold()
        if "python" in tag or "script" in tag:
            raise _fail(
                "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
                "scripted FCStd objects are unsupported",
            )
        for key, raw_value in element.attrib.items():
            value = unicodedata.normalize("NFKC", raw_value).casefold()
            attribute = _local_name(key).casefold()
            if any(marker in value for marker in _SCRIPTED_TYPE_MARKERS):
                raise _fail(
                    "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
                    "scripted FCStd objects are unsupported",
                )
            if attribute in {"name", "property", "key"} and value in _SCRIPTED_PROPERTY_NAMES:
                raise _fail(
                    "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
                    "executable FCStd persistence properties are unsupported",
                )
    return schema_version.strip()


def inspect_fcstd_bytes(contents: bytes) -> FcstdStaticEvidence:
    """Inspect an FCStd archive without importing FreeCAD or executing its objects."""
    if not isinstance(contents, bytes) or not contents or len(contents) > _MAX_ARCHIVE_BYTES:
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd archive bytes are invalid")
    try:
        archive = zipfile.ZipFile(BytesIO(contents), "r")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd is not a supported ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_ENTRY_COUNT:
            raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd entry count exceeds the safety limit")
        portable_names: set[str] = set()
        total_uncompressed = 0
        document_info: zipfile.ZipInfo | None = None
        for info in infos:
            _validate_entry(info)
            portable = _portable_name(info.filename)
            if portable in portable_names:
                raise _fail("FCSTD_ARCHIVE_NAME_COLLISION", "FCStd entry names collide portably")
            portable_names.add(portable)
            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd expanded size exceeds the safety limit")
            if info.filename == "Document.xml":
                document_info = info
        if document_info is None:
            raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "FCStd has no root Document.xml")
        try:
            document_xml = archive.read(document_info)
        except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd entries cannot be read safely") from exc
    schema_version = _inspect_document_xml(document_xml)
    return FcstdStaticEvidence(
        entry_count=len(infos),
        total_uncompressed_bytes=total_uncompressed,
        document_schema_version=schema_version,
    )
