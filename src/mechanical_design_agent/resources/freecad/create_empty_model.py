"""Create a neutral empty FCStd for a new design; no product knowledge is applied."""

from __future__ import annotations

import sys
from pathlib import Path


def create(output_path):
    import FreeCAD as App

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    document = App.newDocument("MechanicalDesignModel")
    try:
        # Native inert geometry metadata cannot persist executable Python proxies.
        audit = document.addObject("Part::Feature", "DesignAudit")
        audit.addProperty("App::PropertyString", "KnowledgeContext", "DesignAudit")
        audit.KnowledgeContext = "DesignContext/v2: no specialized knowledge unless explicitly authorized"
        document.saveAs(str(output))
    finally:
        App.closeDocument(document.Name)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: create_empty_model.py OUTPUT")
    create(sys.argv[-1])
