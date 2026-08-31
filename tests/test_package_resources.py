from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mechanical_design_agent.package_resources import validation_resources_directory


VALIDATION_NAME = "step_component.json"
VALIDATION_SHA256 = "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"


def test_packaged_validation_resource_matches_exact_baseline() -> None:
    with validation_resources_directory() as validation:
        names = [path.name for path in sorted(validation.glob("*.json"))]
        content = (validation / VALIDATION_NAME).read_bytes()

    assert names == [VALIDATION_NAME]
    assert hashlib.sha256(content).hexdigest() == VALIDATION_SHA256
    assert json.loads(content) == {}


def test_removed_schema_directory_is_not_packaged() -> None:
    package_root = Path(__import__("mechanical_design_agent").__file__).parent
    assert not list((package_root / "resources" / "schemas").glob("*.json"))
