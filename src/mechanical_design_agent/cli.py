from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import __version__
from .bootstrap_runtime import BootstrapRuntime
from .database_bootstrap import bootstrap_knowledge_database
from .standard_part_configuration import (
    disable_standard_part_catalog,
    enable_standard_part_catalog,
    load_standard_part_provider_catalog,
    load_standard_part_sources,
)
from .workspace_bootstrap import (
    BootstrapFailure,
    initialize_workspace,
    read_workspace_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mechanical-design")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize a design workspace")
    init.add_argument("--workspace", type=Path, required=True)
    init.add_argument("--actor")
    init.add_argument("--organization")
    init.add_argument("--design-group")
    init.add_argument("--dry-run", action="store_true")

    status = commands.add_parser("status", help="inspect runtime readiness")
    status.add_argument("--workspace", type=Path)

    knowledge = commands.add_parser("knowledge", help="manage knowledge infrastructure")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_bootstrap = knowledge_commands.add_parser("bootstrap")
    knowledge_bootstrap.add_argument("--workspace", type=Path)

    standard = commands.add_parser("standard-parts")
    standard_commands = standard.add_subparsers(dest="standard_command", required=True)
    providers = standard_commands.add_parser("providers")
    providers.add_argument("--category", default="")
    sources = standard_commands.add_parser("status")
    sources.add_argument("--workspace", type=Path)
    catalog = standard_commands.add_parser("catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    enable = catalog_commands.add_parser("enable")
    enable.add_argument("--root", type=Path, required=True)
    enable.add_argument("--workspace", type=Path, required=True)
    disable = catalog_commands.add_parser("disable")
    disable.add_argument("--workspace", type=Path, required=True)
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _exit_code(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    status = value.get("status")
    if isinstance(status, dict):
        status = status.get("overall")
    return {
        "ok": 0,
        "ready": 0,
        "warning": 1,
        "setup_required": 2,
        "blocked": 3,
    }.get(str(status), 0)


def _runtime(workspace: Path | None) -> BootstrapRuntime:
    return BootstrapRuntime.from_process(
        cwd=Path.cwd(),
        environ=os.environ,
        workspace=workspace,
    )


def main() -> None:
    arguments = _parser().parse_args()
    try:
        result: Any
        if arguments.command == "init":
            result = initialize_workspace(
                workspace=arguments.workspace,
                actor_id=arguments.actor,
                organization_id=arguments.organization,
                design_group_id=arguments.design_group,
                dry_run=arguments.dry_run,
            ).as_dict()
        elif arguments.command == "status":
            result = _runtime(arguments.workspace).status()
        elif arguments.command == "knowledge":
            result = bootstrap_knowledge_database(
                _runtime(arguments.workspace).knowledge_settings()
            )
        elif arguments.standard_command == "providers":
            result = load_standard_part_provider_catalog().as_dict(arguments.category)
        elif arguments.standard_command == "status":
            result = _runtime(arguments.workspace).standard_part_sources_status()
        elif arguments.catalog_command == "enable":
            manifest = read_workspace_manifest(arguments.workspace)
            result = enable_standard_part_catalog(
                manifest=manifest, root_path=arguments.root
            )
        else:
            manifest = read_workspace_manifest(arguments.workspace)
            result = disable_standard_part_catalog(manifest=manifest)
        _print(result)
        raise SystemExit(_exit_code(result))
    except BootstrapFailure as exc:
        _print(exc.as_dict())
        raise SystemExit(_exit_code(exc.as_dict())) from None
    except Exception as exc:
        value = {
            "schema_version": "MechanicalDesignCommandError/v1",
            "status": "blocked",
            "code": type(exc).__name__,
            "message": str(exc),
        }
        _print(value)
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
