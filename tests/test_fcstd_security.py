from __future__ import annotations

from io import BytesIO
import struct
import zipfile

import pytest

from mechanical_design_agent.fcstd_security import (
    FcstdSecurityError,
    inspect_fcstd_bytes,
)


def _fcstd(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
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


def test_fcstd_static_inspection_accepts_a_minimal_non_scripted_document() -> None:
    payload = _fcstd([("Document.xml", _document()), ("GuiDocument.xml", b"<GuiDocument/>")])

    evidence = inspect_fcstd_bytes(payload)

    assert evidence.entry_count == 2
    assert evidence.document_schema_version == "4"


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
