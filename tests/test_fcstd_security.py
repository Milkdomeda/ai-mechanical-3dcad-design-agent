from __future__ import annotations

from io import BytesIO
import struct
import zipfile

import pytest

from mechanical_design_agent.fcstd_security import (
    FcstdSecurityError,
    inspect_fcstd_bytes,
)


def _fcstd(
    entries: list[tuple[str, bytes]],
    *,
    archive_comment: bytes = b"",
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = archive_comment
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _document(*, object_xml: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document SchemaVersion="4" ProgramVersion="1.1.3">'
        f"<ObjectData>{object_xml}</ObjectData>"
        "</Document>"
    ).encode("utf-8")


def _encrypted_flag(payload: bytes) -> bytes:
    value = bytearray(payload)
    local = value.index(b"PK\x03\x04")
    central = value.index(b"PK\x01\x02")
    local_flags = struct.unpack_from("<H", value, local + 6)[0]
    central_flags = struct.unpack_from("<H", value, central + 8)[0]
    struct.pack_into("<H", value, local + 6, local_flags | 1)
    struct.pack_into("<H", value, central + 8, central_flags | 1)
    return bytes(value)


def _set_local_u16(payload: bytes, offset: int, value: int) -> bytes:
    changed = bytearray(payload)
    local = changed.index(b"PK\x03\x04")
    struct.pack_into("<H", changed, local + offset, value)
    return bytes(changed)


def _set_local_u32(payload: bytes, offset: int, value: int) -> bytes:
    changed = bytearray(payload)
    local = changed.index(b"PK\x03\x04")
    struct.pack_into("<I", changed, local + offset, value)
    return bytes(changed)


def _set_both_flags(payload: bytes, flags: int) -> bytes:
    changed = bytearray(payload)
    local = changed.index(b"PK\x03\x04")
    central = changed.index(b"PK\x01\x02")
    struct.pack_into("<H", changed, local + 6, flags)
    struct.pack_into("<H", changed, central + 8, flags)
    return bytes(changed)


def _mismatched_local_name(payload: bytes) -> bytes:
    changed = bytearray(payload)
    local = changed.index(b"PK\x03\x04")
    name_start = local + 30
    changed[name_start] = ord("d")
    return bytes(changed)


def _overlapping_second_entry(payload: bytes) -> bytes:
    changed = bytearray(payload)
    first = changed.index(b"PK\x01\x02")
    second = changed.index(b"PK\x01\x02", first + 4)
    struct.pack_into("<I", changed, second + 42, 0)
    return bytes(changed)


def _symlink_entry(payload: bytes) -> bytes:
    changed = bytearray(payload)
    first = changed.index(b"PK\x01\x02")
    second = changed.index(b"PK\x01\x02", first + 4)
    struct.pack_into("<I", changed, second + 38, 0o120777 << 16)
    return bytes(changed)


def test_fcstd_static_inspection_accepts_a_minimal_non_scripted_document() -> None:
    payload = _fcstd([("Document.xml", _document()), ("GuiDocument.xml", b"<GuiDocument/>")])

    evidence = inspect_fcstd_bytes(payload)

    assert evidence.entry_count == 2
    assert evidence.document_schema_version == "4"


def test_fcstd_static_inspection_accepts_freecad_canonical_archive_comment() -> None:
    payload = _fcstd(
        [("Document.xml", _document()), ("GuiDocument.xml", b"<GuiDocument/>")],
        archive_comment=b"FreeCAD Document",
    )

    evidence = inspect_fcstd_bytes(payload)

    assert evidence.entry_count == 2
    assert evidence.document_schema_version == "4"


@pytest.mark.parametrize(
    "archive_comment",
    (
        b"FreeCAD document",
        b"FreeCAD Document\x00",
        b"arbitrary archive comment",
    ),
)
def test_fcstd_static_inspection_rejects_noncanonical_archive_comments(
    archive_comment: bytes,
) -> None:
    payload = _fcstd(
        [("Document.xml", _document())],
        archive_comment=archive_comment,
    )

    with pytest.raises(FcstdSecurityError) as captured:
        inspect_fcstd_bytes(payload)

    assert captured.value.code == "FCSTD_ARCHIVE_INVALID"


@pytest.mark.parametrize(
    "payload,code",
    (
        (b"not a zip", "FCSTD_ARCHIVE_INVALID"),
        (_fcstd([("GuiDocument.xml", b"<GuiDocument/>")]), "FCSTD_STRUCTURE_UNSUPPORTED"),
        (_fcstd([("../Document.xml", _document())]), "FCSTD_ARCHIVE_PATH_UNSAFE"),
        (_fcstd([("Document.xml", _document()), ("Macros/evil.py", b"raise SystemExit")]), "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED"),
        (
            _fcstd([("Document.xml", _document()), ("document.XML", _document())]),
            "FCSTD_ARCHIVE_NAME_COLLISION",
        ),
        (_encrypted_flag(_fcstd([("Document.xml", _document())])), "FCSTD_ARCHIVE_ENCRYPTED"),
        (_fcstd([("Document.xml", b"<Document>")]), "FCSTD_DOCUMENT_XML_INVALID"),
        (
            _fcstd(
                [
                    (
                        "Document.xml",
                        b'<!DOCTYPE Document [<!ENTITY x "boom">]><Document SchemaVersion="4">&x;</Document>',
                    )
                ]
            ),
            "FCSTD_DOCUMENT_XML_INVALID",
        ),
        (
            _fcstd(
                [
                    (
                        "Document.xml",
                        _document(object_xml='<Object type="App::FeaturePython" name="Unsafe"/>'),
                    )
                ]
            ),
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
        ),
        (
            _fcstd(
                [
                    (
                        "Document.xml",
                        _document(
                            object_xml='<Object type="Part::Feature"><Property name="Proxy" type="App::PropertyPythonObject"/></Object>'
                        ),
                    )
                ]
            ),
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
        ),
    ),
)
def test_fcstd_static_inspection_rejects_untrusted_archive_shapes(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(FcstdSecurityError) as captured:
        inspect_fcstd_bytes(payload)

    assert captured.value.code == code


def test_fcstd_static_inspection_rejects_a_compression_ratio_bomb() -> None:
    payload = _fcstd(
        [
            ("Document.xml", _document()),
            ("thumbnails/Thumbnail.png", b"0" * (2 * 1024 * 1024)),
        ]
    )

    with pytest.raises(FcstdSecurityError) as captured:
        inspect_fcstd_bytes(payload)

    assert captured.value.code == "FCSTD_ARCHIVE_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "payload,code",
    (
        (
            _fcstd([("Document.xml", '<?xml version="1.0" encoding="UTF-16"?><Document SchemaVersion="4"/>'.encode("utf-16"))]),
            "FCSTD_DOCUMENT_XML_INVALID",
        ),
        (
            _fcstd([("Document.xml", (b" " * 5000) + b'<!DOCTYPE Document><Document SchemaVersion="4"/>')]),
            "FCSTD_DOCUMENT_XML_INVALID",
        ),
        (
            _fcstd([("Document.xml", b'<Document xmlns="urn:unsafe" SchemaVersion="4"/>')]),
            "FCSTD_STRUCTURE_UNSUPPORTED",
        ),
        (
            _fcstd([("Document.xml", _document()), ("Macros/evil.py.", b"pass")]),
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
        ),
        (
            _fcstd([("Document.xml", _document()), ("shape.bin", b"a"), ("shape.bin.", b"b")]),
            "FCSTD_ARCHIVE_NAME_COLLISION",
        ),
        (
            _fcstd([("Document.xml", _document(object_xml='<Object Type="App::FeaturePython"/>'))]),
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
        ),
        (
            _fcstd([("Document.xml", _document(object_xml='<Property Name="Proxy" Type="App::PropertyString"/>'))]),
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
        ),
    ),
)
def test_fcstd_static_inspection_fails_closed_for_portable_xml_and_names(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(FcstdSecurityError) as captured:
        inspect_fcstd_bytes(payload)
    assert captured.value.code == code


@pytest.mark.parametrize(
    "auxiliary_xml",
    (
        b'<?xml version="1.0" encoding="ISO-8859-1"?><GuiDocument/>',
        b'<GuiDocument xmlns="urn:unsupported"/>',
        b'<GuiDocument>later<!DOCTYPE GuiDocument></GuiDocument>',
        b'<!DOCTYPE GuiDocument [<!ENTITY payload "x">]><GuiDocument/>',
    ),
)
def test_fcstd_static_inspection_rejects_unsafe_declarations_in_every_xml_entry(
    auxiliary_xml: bytes,
) -> None:
    payload = _fcstd(
        [("Document.xml", _document()), ("GuiDocument.xml", auxiliary_xml)]
    )

    with pytest.raises(FcstdSecurityError):
        inspect_fcstd_bytes(payload)


@pytest.mark.parametrize(
    "payload",
    (
        _set_local_u16(_fcstd([("Document.xml", _document())]), 8, zipfile.ZIP_STORED),
        _set_local_u16(_fcstd([("Document.xml", _document())]), 6, 0x08),
        _set_local_u32(_fcstd([("Document.xml", _document())]), 14, 0),
        _set_local_u32(_fcstd([("Document.xml", _document())]), 18, 1),
    ),
)
def test_fcstd_static_inspection_rejects_central_local_metadata_mismatch(
    payload: bytes,
) -> None:
    with pytest.raises(FcstdSecurityError) as captured:
        inspect_fcstd_bytes(payload)
    assert captured.value.code == "FCSTD_ARCHIVE_INVALID"


def test_agent_native_empty_document_archive_passes_its_scanner() -> None:
    """Equivalent to the archive shape emitted by the controlled native seed."""
    payload = _fcstd(
        [
            (
                "Document.xml",
                _document(
                    object_xml=(
                        '<Object type="Part::Feature" name="DesignAudit">'
                        '<Property name="KnowledgeContext" type="App::PropertyString"/>'
                        "</Object>"
                    )
                ),
            ),
            ("GuiDocument.xml", b'<GuiDocument SchemaVersion="1"/>'),
        ],
        archive_comment=b"FreeCAD Document",
    )

    assert inspect_fcstd_bytes(payload).document_schema_version == "4"


@pytest.mark.parametrize(
    "payload,code",
    (
        (_set_both_flags(_fcstd([("Document.xml", _document())]), 0x08), "FCSTD_ARCHIVE_INVALID"),
        (_set_both_flags(_fcstd([("Document.xml", _document())]), 0x40), "FCSTD_ARCHIVE_INVALID"),
        (_mismatched_local_name(_fcstd([("Document.xml", _document())])), "FCSTD_ARCHIVE_INVALID"),
        (
            _overlapping_second_entry(
                _fcstd([("Document.xml", _document()), ("GuiDocument.xml", b"<GuiDocument/>")])
            ),
            "FCSTD_ARCHIVE_INVALID",
        ),
        (
            _symlink_entry(
                _fcstd([("Document.xml", _document()), ("shape.bin", b"link target")])
            ),
            "FCSTD_ARCHIVE_PATH_UNSAFE",
        ),
        (
            _fcstd([("Document.xml", _document()), ("Macros/evil.PY ", b"pass")]),
            "FCSTD_SCRIPTED_OBJECT_UNSUPPORTED",
        ),
    ),
)
def test_fcstd_static_inspection_rejects_noncanonical_raw_zip_layout(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(FcstdSecurityError) as captured:
        inspect_fcstd_bytes(payload)
    assert captured.value.code == code
