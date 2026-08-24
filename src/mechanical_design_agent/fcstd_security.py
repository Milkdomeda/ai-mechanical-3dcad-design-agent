from __future__ import annotations

import binascii
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
import stat
import struct
import unicodedata
import xml.etree.ElementTree as ET
import zlib


_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ENTRY_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_ENTRY_COUNT = 4096
_MAX_COMPRESSION_RATIO = 500
_SUPPORTED_FLAGS = 0x0800
_SCRIPTED_TYPE_MARKERS = (
    "featurepython", "propertypythonobject", "pythonobject", "scriptedobject",
)
_SCRIPTED_PROPERTY_NAMES = frozenset(
    {"proxy", "pythoncode", "pythonscript", "script", "onrestore"}
)
_EXECUTABLE_ENTRY_SUFFIXES = frozenset(
    {
        ".bat", ".cmd", ".com", ".dll", ".dylib", ".exe", ".hta", ".jar",
        ".js", ".msi", ".ps1", ".py", ".pyc", ".pyd", ".scr", ".sh",
        ".so", ".vbs",
    }
)
_XML_ENCODING = re.compile(
    r"<\?xml\s+[^?]*\bencoding\s*=\s*(['\"])([^'\"]+)\1[^?]*\?>",
    re.IGNORECASE,
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


@dataclass(frozen=True)
class _Entry:
    name: str
    raw_name: bytes
    flags: int
    method: int
    crc32: int
    compressed_size: int
    size: int
    local_offset: int
    external_attributes: int


def _fail(code: str, message: str) -> FcstdSecurityError:
    return FcstdSecurityError(code, message)


def _portable_segment(value: str) -> str:
    return unicodedata.normalize("NFKC", value).rstrip(" .").casefold()


def _portable_name(name: str) -> str:
    return "/".join(_portable_segment(part) for part in name.split("/"))


def _decode_name(raw_name: bytes, flags: int) -> str:
    if flags & ~_SUPPORTED_FLAGS:
        if flags & 0x1:
            raise _fail("FCSTD_ARCHIVE_ENCRYPTED", "encrypted FCStd entries are unsupported")
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd entry flags are unsupported")
    try:
        return raw_name.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd entry names must be UTF-8") from exc


def _validate_entry(entry: _Entry) -> None:
    name = entry.name
    if "\\" in name or "\x00" in name:
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd entry path is unsafe")
    path = PurePosixPath(name)
    raw_parts = name.split("/")
    meaningful = raw_parts[:-1] if name.endswith("/") else raw_parts
    if (
        path.is_absolute()
        or not meaningful
        or any(part in {"", ".", ".."} for part in meaningful)
        or meaningful[0].endswith(":")
        or any(ord(character) < 32 for character in name)
        or any(not _portable_segment(part) for part in meaningful)
    ):
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd entry path is unsafe")
    portable_leaf = _portable_segment(meaningful[-1])
    if PurePosixPath(portable_leaf).suffix.casefold() in _EXECUTABLE_ENTRY_SUFFIXES:
        raise _fail(
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
            "executable FCStd archive entries are unsupported",
        )
    if entry.method not in {0, 8}:
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "FCStd compression method is unsupported")
    unix_mode = entry.external_attributes >> 16
    if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
        raise _fail("FCSTD_ARCHIVE_PATH_UNSAFE", "FCStd archive links are unsupported")
    if entry.size < 0 or entry.compressed_size < 0 or entry.size > _MAX_ENTRY_BYTES:
        raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd entry size exceeds the safety limit")
    if entry.size and entry.compressed_size == 0:
        raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd entry compression metadata is unsafe")
    if entry.compressed_size and entry.size / entry.compressed_size > _MAX_COMPRESSION_RATIO:
        raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd compression ratio exceeds the safety limit")


def _central_entries(contents: bytes) -> tuple[list[_Entry], int]:
    if len(contents) < 22 or contents[-22:-18] != b"PK\x05\x06":
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd has no canonical end record")
    try:
        values = struct.unpack_from("<4s4H2IH", contents, len(contents) - 22)
    except struct.error as exc:
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd end record is malformed") from exc
    _, disk, central_disk, disk_count, total_count, central_size, central_offset, comment_size = values
    if (
        disk != 0
        or central_disk != 0
        or disk_count != total_count
        or comment_size != 0
        or not total_count
        or total_count > _MAX_ENTRY_COUNT
        or central_offset + central_size != len(contents) - 22
    ):
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd central directory is unsupported")
    entries: list[_Entry] = []
    cursor = central_offset
    central_end = central_offset + central_size
    for _ in range(total_count):
        if cursor + 46 > central_end or contents[cursor:cursor + 4] != b"PK\x01\x02":
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd central entry is malformed")
        try:
            unpacked = struct.unpack_from("<4s6H3I5H2I", contents, cursor)
        except struct.error as exc:
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd central entry is malformed") from exc
        (
            _, _made, _needed, flags, method, _time, _date, crc32,
            compressed_size, size, name_size, extra_size, entry_comment_size,
            start_disk, _internal_attributes, external_attributes, local_offset,
        ) = unpacked
        end = cursor + 46 + name_size + extra_size + entry_comment_size
        if (
            end > central_end
            or not name_size
            or extra_size
            or entry_comment_size
            or start_disk
            or 0xFFFFFFFF in {compressed_size, size, local_offset}
        ):
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd central entry extensions are unsupported")
        raw_name = contents[cursor + 46:cursor + 46 + name_size]
        entry = _Entry(
            name=_decode_name(raw_name, flags),
            raw_name=raw_name,
            flags=flags,
            method=method,
            crc32=crc32,
            compressed_size=compressed_size,
            size=size,
            local_offset=local_offset,
            external_attributes=external_attributes,
        )
        _validate_entry(entry)
        entries.append(entry)
        cursor = end
    if cursor != central_end:
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd central directory has hidden bytes")
    return entries, central_offset


def _expanded_entries(
    contents: bytes, entries: list[_Entry], central_offset: int
) -> dict[str, bytes]:
    expected_offset = 0
    expanded: dict[str, bytes] = {}
    for entry in sorted(entries, key=lambda item: item.local_offset):
        offset = entry.local_offset
        if offset != expected_offset or offset + 30 > central_offset:
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd local entries overlap or contain hidden bytes")
        try:
            local = struct.unpack_from("<4s5H3I2H", contents, offset)
        except struct.error as exc:
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd local entry is malformed") from exc
        (
            signature, _needed, flags, method, _time, _date, crc32,
            compressed_size, size, name_size, extra_size,
        ) = local
        payload_start = offset + 30 + name_size + extra_size
        payload_end = payload_start + compressed_size
        raw_name = contents[offset + 30:offset + 30 + name_size]
        if (
            signature != b"PK\x03\x04"
            or extra_size
            or raw_name != entry.raw_name
            or flags != entry.flags
            or method != entry.method
            or crc32 != entry.crc32
            or compressed_size != entry.compressed_size
            or size != entry.size
            or payload_end > central_offset
        ):
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd local and central metadata do not match")
        compressed = contents[payload_start:payload_end]
        try:
            if method == 0:
                payload = compressed
            else:
                decompressor = zlib.decompressobj(-15)
                payload = decompressor.decompress(compressed, entry.size + 1)
                payload += decompressor.flush()
                if decompressor.unused_data or decompressor.unconsumed_tail:
                    raise zlib.error("non-canonical DEFLATE stream")
        except zlib.error as exc:
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd compressed entry is malformed") from exc
        if len(payload) != entry.size or (binascii.crc32(payload) & 0xFFFFFFFF) != entry.crc32:
            raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd entry CRC or size is invalid")
        expanded[entry.name] = payload
        expected_offset = payload_end
    if expected_offset != central_offset:
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd local directory has hidden bytes")
    return expanded


def _inspect_xml(contents: bytes, *, require_document: bool) -> str | None:
    folded = contents.lower()
    if b"<!doctype" in folded or b"<!entity" in folded:
        raise _fail("FCSTD_DOCUMENT_XML_INVALID", "Document.xml declarations are unsupported")
    try:
        decoded = contents.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("FCSTD_DOCUMENT_XML_INVALID", "Document.xml must be UTF-8") from exc
    declaration = _XML_ENCODING.search(decoded)
    if declaration is not None and declaration.group(2).casefold() not in {
        "utf-8", "utf8"
    }:
        raise _fail("FCSTD_DOCUMENT_XML_INVALID", "FCStd XML must declare UTF-8")
    if "xmlns" in decoded.casefold():
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "Document.xml namespaces are unsupported")
    try:
        root = ET.fromstring(decoded, parser=ET.XMLParser())
    except (ET.ParseError, ValueError) as exc:
        raise _fail("FCSTD_DOCUMENT_XML_INVALID", "Document.xml is malformed") from exc
    if any(
        "}" in element.tag or any("}" in key for key in element.attrib)
        for element in root.iter()
    ):
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "FCStd XML namespaces are unsupported")
    for element in root.iter():
        tag = unicodedata.normalize("NFKC", element.tag).casefold()
        if "python" in tag or "script" in tag:
            raise _fail("FCSTD_SCRIPTED_OBJECT_UNSUPPORTED", "scripted FCStd objects are unsupported")
        for key, raw_value in element.attrib.items():
            value = unicodedata.normalize("NFKC", raw_value).casefold()
            attribute = unicodedata.normalize("NFKC", key).casefold()
            if any(marker in value for marker in _SCRIPTED_TYPE_MARKERS):
                raise _fail("FCSTD_SCRIPTED_OBJECT_UNSUPPORTED", "scripted FCStd objects are unsupported")
            if attribute in {"name", "property", "key"} and value in _SCRIPTED_PROPERTY_NAMES:
                raise _fail(
                    "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
                    "executable FCStd persistence properties are unsupported",
                )
    if not require_document:
        return None
    if root.tag != "Document":
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "Document.xml has an unsupported root")
    schema_version = root.attrib.get("SchemaVersion")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "Document.xml has no schema version")
    return schema_version.strip()


def inspect_fcstd_bytes(contents: bytes) -> FcstdStaticEvidence:
    """Inspect an FCStd archive in memory without extracting or loading FreeCAD."""
    if not isinstance(contents, bytes) or not contents or len(contents) > _MAX_ARCHIVE_BYTES:
        raise _fail("FCSTD_ARCHIVE_INVALID", "FCStd archive bytes are invalid")
    entries, central_offset = _central_entries(contents)
    portable_names: set[str] = set()
    total_uncompressed = 0
    for entry in entries:
        portable = _portable_name(entry.name)
        if portable in portable_names:
            raise _fail("FCSTD_ARCHIVE_NAME_COLLISION", "FCStd entry names collide portably")
        portable_names.add(portable)
        total_uncompressed += entry.size
        if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise _fail("FCSTD_ARCHIVE_LIMIT_EXCEEDED", "FCStd expanded size exceeds the safety limit")
    expanded = _expanded_entries(contents, entries, central_offset)
    document_xml = expanded.get("Document.xml")
    if document_xml is None:
        raise _fail("FCSTD_STRUCTURE_UNSUPPORTED", "FCStd has no root Document.xml")
    document_schema: str | None = None
    for name, payload in expanded.items():
        if PurePosixPath(_portable_name(name)).suffix == ".xml":
            inspected_schema = _inspect_xml(
                payload, require_document=name == "Document.xml"
            )
            if name == "Document.xml":
                document_schema = inspected_schema
    assert document_schema is not None
    return FcstdStaticEvidence(
        entry_count=len(entries),
        total_uncompressed_bytes=total_uncompressed,
        document_schema_version=document_schema,
    )
