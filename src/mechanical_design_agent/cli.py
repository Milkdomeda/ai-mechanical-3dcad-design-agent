from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .bootstrap_diagnostics import CapabilityRequest, DiagnosticGateError, exit_code_for_status
from .bootstrap_runtime import BootstrapRuntime
from .config import Settings
from .database_bootstrap import bootstrap_databases
from .migrations import postgres_migrations_directory
from .jobs import JobFailure
from .job_errors import safe_job_error
from .repository import PostgresRepository
from .service import MechanicalDesignService
from .smoke import run_test_fixture
from .workspace_bootstrap import (
    BootstrapFailure,
    initialize_workspace,
    parse_selected_env_file,
    select_workspace,
)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _validate_smoke_source(
    parser: argparse.ArgumentParser,
    value: str,
) -> Path:
    """Return the canonical existing regular source file or terminate via parser.error."""
    try:
        source = Path(value).expanduser().resolve(strict=True)
    except OSError:
        parser.error("smoke-fixture --source must be an existing regular file")
    if not source.is_file():
        parser.error("smoke-fixture --source must be an existing regular file")
    return source


CLI_CAPABILITIES = {
    "migrate": "postgres_migration",
    "bootstrap": CapabilityRequest(
        "family_create_or_manage",
        additional_components=("product_family",),
    ),
    "register-library": "library_ingest",
    "scan": "library_ingest",
    "project": "projection",
    "rebuild-projection": "projection",
    "smoke-fixture": "model_validation",
    "job": "design_job_workspace",
    "family": "design_job_workspace",
}


def _add_bootstrap_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace")
    parser.add_argument("--env-file")
    parser.add_argument("--product-family")


def _add_workspace_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace")
    parser.add_argument("--env-file")


def _result_status(value: dict[str, object]) -> str:
    status = value.get("status")
    if isinstance(status, dict):
        overall = status.get("overall")
        if isinstance(overall, str):
            return overall
    if isinstance(status, str):
        return status
    return "blocked"


def _print_result(value: dict[str, object]) -> None:
    _print(value)
    exit_code = exit_code_for_status(_result_status(value))
    if exit_code:
        raise SystemExit(exit_code)


def _job_binding(args: argparse.Namespace) -> str:
    """Use only an explicit CLI binding or this process's environment binding."""
    explicit = getattr(args, "job", None)
    if explicit is not None:
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        raise JobFailure("JOB_INPUT_INVALID", "explicit --job must be a nonblank Job UUID or display ID")
    process_value = os.environ.get("MECH_DESIGN_JOB_ID", "").strip()
    if process_value:
        return process_value
    raise JobFailure(
        "JOB_ID_REQUIRED",
        "--job or process-scoped MECH_DESIGN_JOB_ID is required",
    )


def _job_error(error: Exception) -> dict[str, object]:
    return safe_job_error(error)


def main() -> None:
    parser = argparse.ArgumentParser(prog="mechanical-design")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--workspace")
    init.add_argument("--env-file")
    init.add_argument("--actor-id")
    init.add_argument("--dry-run", action="store_true")
    migrate = sub.add_parser("migrate")
    _add_bootstrap_args(migrate)
    database = sub.add_parser("database")
    database_sub = database.add_subparsers(dest="database_command", required=True)
    database_bootstrap = database_sub.add_parser("bootstrap")
    _add_workspace_args(database_bootstrap)
    status = sub.add_parser("status")
    _add_bootstrap_args(status)
    doctor = sub.add_parser("doctor")
    _add_bootstrap_args(doctor)
    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show")
    _add_bootstrap_args(config_show)
    family = sub.add_parser("family")
    family_sub = family.add_subparsers(dest="family_command", required=True)
    family_list = family_sub.add_parser("list")
    _add_bootstrap_args(family_list)
    family_create = family_sub.add_parser("create")
    _add_bootstrap_args(family_create)
    family_create.add_argument("--organization-id", required=True)
    family_create.add_argument("--organization-name", required=True)
    family_create.add_argument("--design-group-id", required=True)
    family_create.add_argument("--design-group-name", required=True)
    family_create.add_argument("--family-id", required=True)
    family_create.add_argument("--family-name", required=True)
    family_create.add_argument("--alias", action="append", default=[])
    family_create.add_argument("--set-default", action="store_true")
    family_default = family_sub.add_parser("set-default")
    _add_bootstrap_args(family_default)
    family_default.add_argument("family_id")
    family_active = family_sub.add_parser("active")
    _add_bootstrap_args(family_active)
    family_onboard = family_sub.add_parser("onboard")
    family_onboard_sub = family_onboard.add_subparsers(
        dest="family_onboard_command", required=True
    )
    family_onboard_start = family_onboard_sub.add_parser("start")
    _add_bootstrap_args(family_onboard_start)
    family_onboard_start.add_argument("--job", default=None)
    family_onboard_start.add_argument("--expected-revision", type=int, required=True)
    family_onboard_start.add_argument("--family-id", required=True)
    family_onboard_start.add_argument("--source", action="append", required=True)
    family_onboard_analyze = family_onboard_sub.add_parser("analyze")
    _add_bootstrap_args(family_onboard_analyze)
    family_onboard_analyze.add_argument("--job", default=None)
    family_onboard_analyze.add_argument("--expected-revision", type=int, required=True)
    family_onboard_analyze.add_argument("--family-id", required=True)
    family_onboard_analyze.add_argument("--analysis-file", required=True)
    family_onboard_analyze.add_argument("--candidate-file", required=True)
    family_onboard_review = family_onboard_sub.add_parser("review")
    _add_bootstrap_args(family_onboard_review)
    family_onboard_review.add_argument("--job", default=None)
    family_onboard_review.add_argument("--expected-revision", type=int, required=True)
    family_onboard_review.add_argument("--family-id", required=True)
    family_onboard_review.add_argument("--package-sha256", required=True)
    family_onboard_review.add_argument("--decision", choices=("approve", "reject"), required=True)
    family_onboard_review.add_argument("--reviewer-text", required=True)
    family_onboard_review.add_argument("--confirmation", required=True)
    family_onboard_publish = family_onboard_sub.add_parser("publish")
    _add_bootstrap_args(family_onboard_publish)
    family_onboard_publish.add_argument("--job", default=None)
    family_onboard_publish.add_argument("--expected-revision", type=int, required=True)
    family_onboard_publish.add_argument("--family-id", required=True)
    family_onboard_publish.add_argument("--package-sha256", required=True)
    family_onboard_publish.add_argument("--review-identity", required=True)
    family_onboard_publish.add_argument("--confirmation", required=True)
    family_onboard_status = family_onboard_sub.add_parser("status")
    _add_bootstrap_args(family_onboard_status)
    family_onboard_status.add_argument("--job", default=None)
    standard_parts = sub.add_parser("standard-parts")
    standard_parts_sub = standard_parts.add_subparsers(
        dest="standard_parts_command",
        required=True,
    )
    providers = standard_parts_sub.add_parser("providers")
    providers.add_argument("--category", default="")
    sources_status = standard_parts_sub.add_parser("status")
    _add_workspace_args(sources_status)
    catalog = standard_parts_sub.add_parser("catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_enable = catalog_sub.add_parser("enable")
    _add_workspace_args(catalog_enable)
    catalog_enable.add_argument("--root", required=True)
    catalog_disable = catalog_sub.add_parser("disable")
    _add_workspace_args(catalog_disable)
    bootstrap = sub.add_parser("bootstrap")
    _add_bootstrap_args(bootstrap)
    register = sub.add_parser("register-library")
    _add_bootstrap_args(register)
    register.add_argument("root_path")
    scan = sub.add_parser("scan")
    _add_bootstrap_args(scan)
    scan.add_argument("--library-id", default="")
    project = sub.add_parser("project")
    _add_bootstrap_args(project)
    project.add_argument("--limit", type=int, default=100)
    rebuild = sub.add_parser("rebuild-projection")
    _add_bootstrap_args(rebuild)
    rebuild.add_argument("--confirmation", required=True)
    smoke = sub.add_parser("smoke-fixture")
    _add_bootstrap_args(smoke)
    smoke.add_argument("--source", required=True)
    job = sub.add_parser("job")
    job_sub = job.add_subparsers(dest="job_command", required=True)

    job_create = job_sub.add_parser("create")
    _add_bootstrap_args(job_create)
    job_create.add_argument("--job-type", required=True)
    job_create.add_argument("--title", required=True)
    job_create.add_argument("--organization-id", required=True)
    job_create.add_argument("--design-group-id", required=True)
    job_create.add_argument("--family-id", default="")
    job_create.add_argument("--idempotency-token", required=True)
    job_create.add_argument("--source-file", dest="source_files", action="append", default=[])

    job_list = job_sub.add_parser("list")
    _add_bootstrap_args(job_list)
    job_list.add_argument("--status", default="")
    job_list.add_argument("--job-type", default="")
    job_list.add_argument("--family-id", default="")

    job_status = job_sub.add_parser("status")
    _add_bootstrap_args(job_status)
    job_status.add_argument("--job", default=None)

    job_resolve = job_sub.add_parser("resolve")
    _add_bootstrap_args(job_resolve)
    job_resolve.add_argument("--query", required=True)
    job_resolve.add_argument("--job-type", default="")
    job_resolve.add_argument("--family-id", default="")
    job_resolve.add_argument("--status", dest="job_statuses", action="append", default=[])

    job_close = job_sub.add_parser("close")
    _add_bootstrap_args(job_close)
    job_close.add_argument("--job", default=None)
    job_close.add_argument("--expected-revision", type=int, required=True)
    job_close.add_argument("--status", required=True)
    job_close.add_argument("--phase", required=True)
    job_close.add_argument("--reason", required=True)
    job_close.add_argument("--confirmation", required=True)

    job_reopen = job_sub.add_parser("reopen")
    _add_bootstrap_args(job_reopen)
    job_reopen.add_argument("--job", default=None)
    job_reopen.add_argument("--expected-revision", type=int, required=True)
    job_reopen.add_argument("--phase", required=True)
    job_reopen.add_argument("--reason", required=True)
    job_reopen.add_argument("--confirmation", required=True)

    job_doctor = job_sub.add_parser("doctor")
    _add_bootstrap_args(job_doctor)
    job_doctor.add_argument("--job", default=None)

    job_repair = job_sub.add_parser("repair")
    _add_bootstrap_args(job_repair)
    job_repair.add_argument("--job", default=None)
    job_repair.add_argument("--expected-revision", type=int, required=True)
    job_repair.add_argument("--doctor-receipt-sha256", required=True)
    job_repair.add_argument("--reason", required=True)
    job_repair.add_argument("--confirmation", required=True)

    job_migrate = job_sub.add_parser("migrate-legacy")
    _add_bootstrap_args(job_migrate)
    job_migrate_mode = job_migrate.add_mutually_exclusive_group()
    job_migrate_mode.add_argument("--dry-run", action="store_true")
    job_migrate_mode.add_argument("--apply", action="store_true")
    job_migrate.add_argument("--plan-file")
    job_migrate.add_argument("--receipt-sha256")
    job_migrate.add_argument("--confirmation")
    args = parser.parse_args()
    if args.command == "smoke-fixture":
        args.source = str(_validate_smoke_source(parser, args.source))
    if args.command == "init":
        try:
            cwd = Path.cwd()
            env_file = parse_selected_env_file(args.env_file, os.environ, cwd)
            workspace = select_workspace(
                runtime_workspace=args.workspace,
                environ=os.environ,
                env_file=env_file,
                cwd=cwd,
                require_manifest=False,
            )
            _print(
                initialize_workspace(
                    workspace=workspace.path,
                    actor_id=args.actor_id,
                    dry_run=args.dry_run,
                ).as_dict()
            )
        except BootstrapFailure as exc:
            _print(exc.as_dict())
            raise SystemExit(2 if exc.status == "setup_required" else 3) from None
        return
    if (
        args.command == "standard-parts"
        and args.standard_parts_command == "providers"
    ):
        result = BootstrapRuntime.standard_part_providers(args.category)
        if isinstance(result.get("status"), str):
            _print_result(result)
        else:
            _print(result)
        return
    runtime = BootstrapRuntime.from_process(
        cwd=Path.cwd(),
        environ=os.environ,
        workspace=args.workspace,
        env_file=args.env_file,
        product_family_id=getattr(args, "product_family", None),
    )
    if args.command == "standard-parts":
        if args.standard_parts_command == "status":
            result = runtime.standard_part_sources_status()
        elif args.catalog_command == "enable":
            result = runtime.standard_part_catalog_enable(args.root)
        else:
            result = runtime.standard_part_catalog_disable()
        _print_result(result)
        return
    if args.command == "status":
        _print_result(runtime.status())
        return
    if args.command == "doctor":
        _print_result(runtime.doctor())
        return
    if args.command == "config" and args.config_command == "show":
        _print_result(runtime.config_show())
        return
    if args.command == "family" and args.family_command != "onboard":
        try:
            if args.family_command == "list":
                result = runtime.list_product_families()
            elif args.family_command == "create":
                result = runtime.create_product_family(
                    organization_id=args.organization_id,
                    organization_name=args.organization_name,
                    design_group_id=args.design_group_id,
                    design_group_name=args.design_group_name,
                    family_id=args.family_id,
                    family_name=args.family_name,
                    aliases=args.alias,
                )
                if args.set_default:
                    result = {
                        **result,
                        "default_selection": runtime.set_default_product_family(
                            args.family_id
                        ),
                    }
            elif args.family_command == "set-default":
                result = runtime.set_default_product_family(args.family_id)
            else:
                result = runtime.active_product_family()
        except BootstrapFailure as exc:
            _print_result(exc.as_dict())
            return
        _print_result(result)
        return

    request = (
        "database_bootstrap"
        if args.command == "database"
        else CLI_CAPABILITIES[args.command]
    )
    capability = request.capability if isinstance(request, CapabilityRequest) else request
    try:
        runtime.require_initialized(capability)
        runtime.require_capability(request, probe=True)
    except DiagnosticGateError as exc:
        _print_result(exc.response)
        return
    if args.command == "database":
        secret_names = (
            "postgresql",
            "neo4j_uri",
            "neo4j_user",
            "neo4j_password",
        )
        secrets = {name: runtime.secret_value(name) for name in secret_names}
        if any(value is None for value in secrets.values()):
            _print_result(
                runtime.blocked_response(
                    capability="database_bootstrap",
                    code="DATABASE_CONFIGURATION_BLOCKED",
                    message="database bootstrap configuration is not ready",
                )
            )
            return
        _print_result(
            bootstrap_databases(
                database_url=str(secrets["postgresql"]),
                neo4j_uri=str(secrets["neo4j_uri"]),
                neo4j_user=str(secrets["neo4j_user"]),
                neo4j_password=str(secrets["neo4j_password"]),
            )
        )
        return
    if args.command == "migrate":
        with postgres_migrations_directory() as migrations:
            database_url = runtime.secret_value("postgresql")
            if database_url is None:
                raise RuntimeError("PostgreSQL capability passed without credentials")
            repository = PostgresRepository(database_url)
            _print(repository.apply_migrations(migrations))
        return
    try:
        settings = (
            runtime.job_operational_settings()
            if args.command == "job"
            or (args.command == "family" and args.family_command == "onboard")
            else runtime.operational_settings()
        )
    except DiagnosticGateError as exc:
        _print_result(exc.response)
        return
    except Exception as exc:
        _print_result(
            runtime.blocked_response(
                capability=capability,
                code="SERVICE_CONFIGURATION_BLOCKED",
                message=f"operational service configuration is not ready: {type(exc).__name__}",
            )
        )
        return
    if args.command == "smoke-fixture":
        _print(run_test_fixture(settings, args.source))
        return
    if args.command == "family" and args.family_command == "onboard":
        try:
            service = MechanicalDesignService(settings)
            job_id = _job_binding(args)
            if args.family_onboard_command == "start":
                result = service.product_family_onboarding_start(
                    job_id=job_id,
                    expected_job_revision=args.expected_revision,
                    family_id=args.family_id,
                    source_paths=args.source,
                )
            elif args.family_onboard_command == "analyze":
                analysis = json.loads(
                    Path(args.analysis_file).expanduser().resolve(strict=True).read_text(
                        encoding="utf-8"
                    )
                )
                candidates = json.loads(
                    Path(args.candidate_file).expanduser().resolve(strict=True).read_text(
                        encoding="utf-8"
                    )
                )
                if not isinstance(analysis, dict) or not isinstance(candidates, list):
                    raise JobFailure(
                        "JOB_ONBOARDING_INPUT_INVALID",
                        "analysis file must be an object and candidate file must be an array",
                    )
                result = service.product_family_onboarding_analyze(
                    job_id=job_id,
                    expected_job_revision=args.expected_revision,
                    family_id=args.family_id,
                    analysis=analysis,
                    candidate_knowledge=candidates,
                )
            elif args.family_onboard_command == "review":
                result = service.product_family_onboarding_review(
                    job_id=job_id,
                    expected_job_revision=args.expected_revision,
                    family_id=args.family_id,
                    package_sha256=args.package_sha256,
                    decision=args.decision,
                    reviewer_text=args.reviewer_text,
                    confirmation=args.confirmation,
                )
            elif args.family_onboard_command == "publish":
                result = service.product_family_onboarding_publish(
                    job_id=job_id,
                    expected_job_revision=args.expected_revision,
                    family_id=args.family_id,
                    package_sha256=args.package_sha256,
                    review_identity=args.review_identity,
                    confirmation=args.confirmation,
                )
            else:
                result = service.product_family_onboarding_status(job_id=job_id)
        except Exception as exc:
            _print_result(_job_error(exc))
            return
        _print(result)
        return
    if args.command == "job":
        try:
            service = MechanicalDesignService(settings)
            if args.job_command == "create":
                result = service.design_job_create(
                    job_type=args.job_type,
                    title=args.title,
                    organization_id=args.organization_id,
                    design_group_id=args.design_group_id,
                    family_id=args.family_id or None,
                    idempotency_token=args.idempotency_token,
                    source_files=args.source_files,
                )
            elif args.job_command == "migrate-legacy":
                if not args.apply:
                    result = service.design_job_migrate_legacy_dry_run()
                else:
                    if not all(
                        isinstance(value, str) and value.strip()
                        for value in (
                            args.plan_file,
                            args.receipt_sha256,
                            args.confirmation,
                        )
                    ):
                        raise JobFailure(
                            "JOB_MIGRATION_INPUT_REQUIRED",
                            "--apply requires --plan-file, --receipt-sha256, and --confirmation",
                        )
                    plan = json.loads(
                        Path(args.plan_file).expanduser().resolve(strict=True).read_text(
                            encoding="utf-8"
                        )
                    )
                    if not isinstance(plan, dict):
                        raise JobFailure(
                            "JOB_MIGRATION_PLAN_INVALID", "migration plan must be a JSON object"
                        )
                    result = service.design_job_migrate_legacy_apply(
                        plan=plan,
                        receipt_sha256=args.receipt_sha256,
                        confirmation=args.confirmation,
                    )
            elif args.job_command == "list":
                result = service.design_job_list(
                    status=args.status or None,
                    job_type=args.job_type or None,
                    family_id=args.family_id or None,
                )
            elif args.job_command == "resolve":
                result = service.design_job_resolve(
                    query=args.query,
                    job_type=args.job_type or None,
                    family_id=args.family_id or None,
                    statuses=tuple(args.job_statuses or ("active", "blocked")),
                )
            elif args.job_command not in {"migrate-legacy"}:
                job_id = _job_binding(args)
                if args.job_command == "status":
                    result = service.design_job_get(job_id=job_id)
                elif args.job_command == "close":
                    result = service.design_job_close(
                        job_id=job_id,
                        expected_revision=args.expected_revision,
                        status=args.status,
                        phase=args.phase,
                        reason=args.reason,
                        confirmation=args.confirmation,
                    )
                elif args.job_command == "reopen":
                    result = service.design_job_reopen(
                        job_id=job_id,
                        expected_revision=args.expected_revision,
                        phase=args.phase,
                        reason=args.reason,
                        confirmation=args.confirmation,
                    )
                elif args.job_command == "doctor":
                    result = service.design_job_doctor(job_id=job_id)
                else:
                    result = service.design_job_repair(
                        job_id=job_id,
                        expected_revision=args.expected_revision,
                        doctor_receipt_sha256=args.doctor_receipt_sha256,
                        reason=args.reason,
                        confirmation=args.confirmation,
                    )
        except Exception as exc:
            _print_result(_job_error(exc))
            return
        if args.job_command == "doctor":
            _print_result(result)
        else:
            _print(result)
        return
    service = MechanicalDesignService(settings)
    if args.command == "status":
        _print(service.system_status())
    elif args.command == "bootstrap":
        _print(service.family_bootstrap_get())
    elif args.command == "register-library":
        _print(service.library_register(args.root_path))
    elif args.command == "scan":
        _print(service.library_scan(args.library_id))
    elif args.command == "project":
        _print(service.projection_sync(args.limit))
    elif args.command == "rebuild-projection":
        _print(service.projection_rebuild(args.confirmation))


if __name__ == "__main__":
    main()
