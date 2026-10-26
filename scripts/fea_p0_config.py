"""Pin tools in a separately verified official portable distribution."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from fea_native_probe import select_bundled_tool
    from fea_helpers import read_version
    tools = {}
    for name in ("freecadcmd", "gmsh", "ccx"):
        path = select_bundled_tool(args.root, name)
        flag = "-v" if name == "ccx" else "--version"
        result = subprocess.run([str(path), flag], capture_output=True, text=True, timeout=30)
        version = read_version(name, result)
        if name == "freecadcmd" and version != "1.1.3":
            raise RuntimeError("FreeCAD 1.1.3 required")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        tools[name] = {"path": str(path.absolute()), "version": version, "sha256": digest}
        print(name, version, digest)
    args.output.write_text(json.dumps({"schema_version": "FEAP0Configuration/v1", "tools": tools, "timeout_seconds": 300}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
