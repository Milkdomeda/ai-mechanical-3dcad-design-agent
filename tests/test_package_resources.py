from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from mechanical_design_agent.package_resources import (
    schemas_directory,
    validation_resources_directory,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_NAME = "design-lesson-package-v1.schema.json"
SCHEMA_SHA256 = "67251e2d3411ec7ea668f4d16401eb813ebfb7c1008f6aded52564f04ff56838"
SCREENING_SCHEMA_NAME = "design-lesson-screening-package-v1.schema.json"
SCREENING_SCHEMA_SHA256 = (
    "43605b2110020ecfc0aca3b79eacb51ef5733183f0eae7030b16a673c69d18f8"
)
VALIDATION_NAME = "step_component.json"
VALIDATION_SHA256 = "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"


class PackageResourceTests(unittest.TestCase):
    def test_packaged_resources_match_the_exact_json_baseline(self) -> None:
        with schemas_directory() as schemas:
            self.assertEqual(
                [path.name for path in sorted(schemas.glob("*.json"))],
                [SCHEMA_NAME, SCREENING_SCHEMA_NAME],
            )
            schema_bytes = (schemas / SCHEMA_NAME).read_bytes()
            screening_schema_bytes = (schemas / SCREENING_SCHEMA_NAME).read_bytes()
        with validation_resources_directory() as validation:
            self.assertEqual(
                [path.name for path in sorted(validation.glob("*.json"))],
                [VALIDATION_NAME],
            )
            validation_bytes = (validation / VALIDATION_NAME).read_bytes()

        self.assertEqual(hashlib.sha256(schema_bytes).hexdigest(), SCHEMA_SHA256)
        self.assertEqual(
            hashlib.sha256(screening_schema_bytes).hexdigest(),
            SCREENING_SCHEMA_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(validation_bytes).hexdigest(), VALIDATION_SHA256
        )
        self.assertEqual(json.loads(schema_bytes)["$id"], SCHEMA_NAME)
        self.assertEqual(
            json.loads(screening_schema_bytes)["$id"], SCREENING_SCHEMA_NAME
        )
        self.assertEqual(json.loads(validation_bytes), {})


class InstalledWheelPackageResourceTests(unittest.TestCase):
    def test_clean_installed_wheel_locates_schema_and_validation_resources(self) -> None:
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "uv is required to build the release wheel")

        with tempfile.TemporaryDirectory(prefix="packaged-static-resources-") as temporary:
            root = Path(temporary)
            dist = root / "dist"
            venv = root / "venv"
            environment = dict(os.environ)
            environment.setdefault("UV_CACHE_DIR", str(root / "uv-cache"))
            environment.pop("PYTHONPATH", None)
            subprocess.run(
                [str(uv), "build", "--wheel", "--out-dir", str(dist)],
                cwd=PROJECT_ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(dist.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                packaged = sorted(
                    name
                    for name in archive.namelist()
                    if "/resources/schemas/" in name
                    or "/resources/validation/" in name
                )
            self.assertEqual(
                packaged,
                [
                    f"mechanical_design_agent/resources/schemas/{SCHEMA_NAME}",
                    "mechanical_design_agent/resources/schemas/"
                    f"{SCREENING_SCHEMA_NAME}",
                    f"mechanical_design_agent/resources/validation/{VALIDATION_NAME}",
                ],
            )

            subprocess.run(
                [str(uv), "venv", "--python", sys.executable, str(venv)],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run(
                [str(uv), "pip", "install", "--python", str(python), str(wheel)],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            module_path = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import mechanical_design_agent as package; print(package.__file__)",
                ],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertTrue(Path(module_path).is_relative_to(venv))

            script = (
                "import hashlib, json\n"
                "from mechanical_design_agent.package_resources import "
                "schemas_directory, validation_resources_directory\n"
                f"schema_name = {SCHEMA_NAME!r}\n"
                f"screening_schema_name = {SCREENING_SCHEMA_NAME!r}\n"
                f"validation_name = {VALIDATION_NAME!r}\n"
                "with schemas_directory() as root:\n"
                "    schema_bytes = (root / schema_name).read_bytes()\n"
                "    screening_schema_bytes = "
                "(root / screening_schema_name).read_bytes()\n"
                "with validation_resources_directory() as root:\n"
                "    validation_bytes = (root / validation_name).read_bytes()\n"
                "print(json.dumps({"
                "'schema_id': json.loads(schema_bytes)['$id'], "
                "'schema_sha256': hashlib.sha256(schema_bytes).hexdigest(), "
                "'screening_schema_id': "
                "json.loads(screening_schema_bytes)['$id'], "
                "'screening_schema_sha256': "
                "hashlib.sha256(screening_schema_bytes).hexdigest(), "
                "'validation': json.loads(validation_bytes), "
                "'validation_sha256': hashlib.sha256(validation_bytes).hexdigest()}))\n"
            )
            result = subprocess.run(
                [str(python), "-c", script],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = json.loads(result.stdout)
            self.assertEqual(
                installed,
                {
                    "schema_id": SCHEMA_NAME,
                    "schema_sha256": SCHEMA_SHA256,
                    "screening_schema_id": SCREENING_SCHEMA_NAME,
                    "screening_schema_sha256": SCREENING_SCHEMA_SHA256,
                    "validation": {},
                    "validation_sha256": VALIDATION_SHA256,
                },
            )
