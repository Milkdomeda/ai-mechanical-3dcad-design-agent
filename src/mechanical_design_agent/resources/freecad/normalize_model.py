"""Create a normalized FCStd from STEP or FCStd without altering the source."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def normalize(source_path, output_path):
    import FreeCAD as App
    import Import

    source = Path(source_path).expanduser().resolve(strict=True)
    output = Path(output_path).expanduser().resolve()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    if source.suffix.lower() == ".fcstd":
        document = App.openDocument(str(source), hidden=True)
    else:
        document = App.newDocument("MechanicalDesignModel")
        Import.insert(str(source), document.Name)
        document.recompute()
    try:
        document.saveAs(str(output))
    finally:
        App.closeDocument(document.Name)
    after = hashlib.sha256(source.read_bytes()).hexdigest()
    if after != before:
        raise RuntimeError("source CAD changed while creating the normalized model")
    return before


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: normalize_model.py SOURCE OUTPUT")
    normalize(sys.argv[-2], sys.argv[-1])
