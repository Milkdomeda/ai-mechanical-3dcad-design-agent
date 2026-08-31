from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE = "mechanical_design_agent/resources/config/standard_part_providers.json"
EXPECTED_SHA256 = "089afe6cbbdae68d72aea60497bf285fc01b1d589dde7379a2a124e346c35464"
EXPECTED_PROVIDER_IDS = [
    "freecad-fasteners",
    "freecad-gears",
    "step-parts",
    "verified-local",
    "manufacturer-official",
    "3dfindit-cadenas",
    "misumi",
    "traceparts",
]


def test_wheel_contains_the_verified_standard_part_provider_catalog() -> None:
    uv = shutil.which("uv")
    assert uv is not None
    with tempfile.TemporaryDirectory(prefix="standard-part-wheel-") as temporary:
        dist = Path(temporary) / "dist"
        environment = dict(os.environ)
        environment.setdefault("UV_CACHE_DIR", str(Path(temporary) / "uv-cache"))
        result = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(dist)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
        wheel = next(dist.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            content = archive.read(RESOURCE)

    assert hashlib.sha256(content).hexdigest() == EXPECTED_SHA256
    value = json.loads(content)
    assert [item["id"] for item in value["providers"]] == EXPECTED_PROVIDER_IDS
