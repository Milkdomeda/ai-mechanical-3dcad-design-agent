"""Reopen, recompute, and validate one controlled FCStd working copy."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def validate(input_path, receipt_path):
    import FreeCAD as App

    source = Path(input_path).expanduser().resolve(strict=True)
    receipt = Path(os.path.abspath(Path(receipt_path).expanduser()))
    if source.suffix.casefold() != ".fcstd":
        raise ValueError("working-copy input must be FCStd")
    if source.stat().st_size <= 0:
        raise ValueError("working-copy FCStd must not be empty")
    if receipt.exists():
        raise FileExistsError(receipt)
    if receipt.parent != source.parent:
        raise ValueError("validation receipt must share the controlled working directory")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    document = App.openDocument(str(source), hidden=True)
    try:
        recompute_result = document.recompute()
        invalid_objects = []
        for obj in document.Objects:
            states = {str(value).casefold() for value in getattr(obj, "State", ())}
            if "invalid" in states or "error" in states:
                invalid_objects.append(str(obj.Name))
            shape = getattr(obj, "Shape", None)
            if shape is not None and not shape.isNull() and not shape.isValid():
                invalid_objects.append(str(obj.Name))
        if invalid_objects:
            raise ValueError("working-copy document contains invalid objects")
        after = hashlib.sha256(source.read_bytes()).hexdigest()
        if after != before:
            raise RuntimeError("working-copy FCStd changed during validation")
        payload = {
            "schema_version": "MechanicalDesignWorkingCopyValidation/v1",
            "status": "valid",
            "sha256": before,
            "size_bytes": source.stat().st_size,
            "document_name": str(document.Name),
            "object_count": len(document.Objects),
            "recomputed": recompute_result is not False,
        }
        temporary = receipt.with_name(f".{receipt.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, receipt)
        return payload
    finally:
        App.closeDocument(document.Name)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: validate_working_copy.py INPUT RECEIPT")
    validate(sys.argv[-2], sys.argv[-1])
