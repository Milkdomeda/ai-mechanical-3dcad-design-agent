from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterator


@contextmanager
def _resource_directory(name: str) -> Iterator[Path]:
    resource = files("mechanical_design_agent").joinpath("resources", name)
    with as_file(resource) as root:
        yield root


@contextmanager
def schemas_directory() -> Iterator[Path]:
    with _resource_directory("schemas") as root:
        yield root


@contextmanager
def validation_resources_directory() -> Iterator[Path]:
    with _resource_directory("validation") as root:
        yield root


@contextmanager
def freecad_scripts_directory() -> Iterator[Path]:
    with _resource_directory("freecad") as root:
        yield root


@contextmanager
def standard_part_provider_config() -> Iterator[Path]:
    with _resource_directory("config") as root:
        yield root / "standard_part_providers.json"
