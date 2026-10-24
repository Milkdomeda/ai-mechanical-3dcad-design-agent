"""Run P0 probes against explicitly configured local tools, never user CAD."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=["p0", "full"])
    parser.add_argument("--automated-only", action="store_true", help="Return automated subset status; report still records unmet P0 gates.")
    args = parser.parse_args(argv)
    if args.phase != "p0":
        parser.error("full acceptance is not implemented in P0")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from fea_native_probe import ProbeError, parse_config, probe_native
    try:
        report = probe_native(parse_config(args.config), args.workspace)
    except ProbeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 3
    print(json.dumps({"status": report["status"], "automated_status": report["automated_status"], "report": str(args.workspace / "probe.json")}))
    return 0 if report["status"] == "passed" or (args.automated_only and report["automated_status"] == "passed") else 3


if __name__ == "__main__":
    raise SystemExit(main())
