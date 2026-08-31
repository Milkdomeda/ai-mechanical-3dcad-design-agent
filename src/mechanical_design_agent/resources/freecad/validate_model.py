"""Reopen and validate a host-inspected FCStd through nonce-bound stdout."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


_PREFIX = "MECHANICAL_DESIGN_FCSTD_VALIDATION_V1 "


def validate(input_path, nonce):
    import FreeCAD as App

    source = Path(input_path).expanduser().resolve(strict=True)
    if source.suffix.casefold() != ".fcstd" or source.stat().st_size <= 0:
        raise ValueError("model input must be a nonempty FCStd")
    if not isinstance(nonce, str) or len(nonce) < 32:
        raise ValueError("Agent validation nonce is invalid")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    before_size = source.stat().st_size
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
            raise ValueError("model document contains invalid objects")
        after = hashlib.sha256(source.read_bytes()).hexdigest()
        if after != before or source.stat().st_size != before_size:
            raise RuntimeError("FCStd changed during validation")
        payload = {
            "schema_version": "MechanicalDesignModelValidation/v1",
            "status": "valid",
            "nonce": nonce,
            "sha256": before,
            "size_bytes": before_size,
            "document_name": str(document.Name),
            "object_count": len(document.Objects),
            "recomputed": recompute_result is not False,
        }
        print(
            _PREFIX
            + json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return payload
    finally:
        App.closeDocument(document.Name)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: validate_model.py INPUT AGENT_NONCE")
    validate(sys.argv[-2], sys.argv[-1])
