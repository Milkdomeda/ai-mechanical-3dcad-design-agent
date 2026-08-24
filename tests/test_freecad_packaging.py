from __future__ import annotations

import hashlib
import getpass
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from mechanical_design_agent.freecad_discovery import CERTIFIED_FREECADCMD_VERSIONS
from mechanical_design_agent.package_resources import freecad_scripts_directory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FREECADCMD = os.environ.get("MECH_DESIGN_FREECADCMD", "").strip()
EXPECTED_FREECAD_VERSION = os.environ.get(
    "MECH_DESIGN_FREECADCMD_EXPECTED_VERSION", ""
).strip()
EXPECTED_FREECAD_SHA256 = os.environ.get(
    "MECH_DESIGN_FREECADCMD_SHA256", ""
).strip().lower()
EXPECTED_SCRIPTS = {
    "create_empty_working_copy.py": "934dae14266e0d3fb47080b7637c47f1b04642447a358bdf71a03d9489b60b61",
    "extract_model_manifest.py": "cc63c6d6a9281259bb238c5c8d118115f3fb99c03b6a3ea09863bbe0ecfb267d",
    "normalize_working_copy.py": "eb3fa4ff50a6f16903720f340b4bb3e8469504d7295e1821bccbaf4417ad9539",
    "validate_external_step.py": "f069b4c32b82c3a9016ba95e6dc59ceee4749c0b0501087c2992410d717ec7cd",
    "validate_fastener_interfaces.py": "1defe089214c6ac9a6b89893c05cfcfe6e2576a7b36ee7e737d98e0ababe099b",
    "validate_mechanical_interfaces.py": "a92fbc4f759d98ba5ad75ea721c6a9a52884eef56c9a3c36fce81ad771c247bf",
    "validate_working_copy.py": "683dcccc27d846eff47bb63ef0de4b1deffa4a0ffd69436f441729f53cd371b5",
}


class FreeCADPackageResourceTests(unittest.TestCase):
    def test_packaged_freecad_scripts_match_the_exact_baseline(self) -> None:
        with freecad_scripts_directory() as scripts:
            names = [path.name for path in sorted(scripts.glob("*.py"))]
            digests = {
                name: hashlib.sha256((scripts / name).read_bytes()).hexdigest()
                for name in names
            }

        self.assertEqual(names, list(EXPECTED_SCRIPTS))
        self.assertEqual(digests, EXPECTED_SCRIPTS)

    def test_new_design_seed_uses_only_native_non_scripted_objects(self) -> None:
        with freecad_scripts_directory() as scripts:
            source = (scripts / "create_empty_working_copy.py").read_text(
                encoding="utf-8"
            )

        self.assertIn('addObject("Part::Feature", "DesignAudit")', source)
        self.assertNotIn("FeaturePython", source)


@unittest.skipUnless(
    FREECADCMD,
    "MECH_DESIGN_FREECADCMD is not configured; installed-wheel FreeCAD test skipped",
)
class InstalledWheelFreeCADE2ETests(unittest.TestCase):
    def test_installed_wheel_executes_core_scripts_and_loads_validators(self) -> None:
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "uv is required to build the release wheel")
        freecadcmd = Path(FREECADCMD).expanduser().resolve(strict=True)
        self.assertRegex(
            EXPECTED_FREECAD_SHA256,
            r"^[0-9a-f]{64}$",
            "MECH_DESIGN_FREECADCMD_SHA256 must hold the reviewed official 1.1.3 digest",
        )
        version = subprocess.run(
            [str(freecadcmd), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(version.returncode, 0, version.stdout)
        match = re.search(
            r"\bFreeCAD(?:Cmd)?\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)\b",
            version.stdout,
            re.IGNORECASE,
        )
        self.assertIsNotNone(match, version.stdout)
        actual_version = match.group(1)
        self.assertIn(actual_version, CERTIFIED_FREECADCMD_VERSIONS)
        if EXPECTED_FREECAD_VERSION:
            self.assertEqual(actual_version, EXPECTED_FREECAD_VERSION)

        with tempfile.TemporaryDirectory(prefix="packaged-freecad-scripts-") as temporary:
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
                    Path(name).name
                    for name in archive.namelist()
                    if "/resources/freecad/" in name and name.endswith(".py")
                )
            self.assertEqual(packaged, list(EXPECTED_SCRIPTS))

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

            installed_environment = {
                **environment,
                "PACKAGING_FREECADCMD": str(freecadcmd),
                "PACKAGING_FREECADCMD_SHA256": EXPECTED_FREECAD_SHA256,
                "PACKAGING_WORKSPACE": str(root / "执行 workspace with spaces"),
            }
            script = (
                "import hashlib, json, os\n"
                "from pathlib import Path\n"
                "from mechanical_design_agent.freecad_runner import run_freecad_script\n"
                "from mechanical_design_agent.package_resources import freecad_scripts_directory\n"
                "from mechanical_design_agent.secure_fs import read_managed_file\n"
                "freecadcmd = Path(os.environ['PACKAGING_FREECADCMD'])\n"
                "expected_freecad_sha256 = os.environ['PACKAGING_FREECADCMD_SHA256']\n"
                "workspace = Path(os.environ['PACKAGING_WORKSPACE'])\n"
                "workspace.mkdir(parents=True)\n"
                "freecad_pin = read_managed_file(freecadcmd)\n"
                "if freecad_pin.sha256 != expected_freecad_sha256:\n"
                "    raise RuntimeError('reviewed FreeCAD executable SHA-256 mismatch')\n"
                "source = workspace / '源 model with spaces.FCStd'\n"
                "normalized = workspace / 'normalized 模型.FCStd'\n"
                "source_manifest = workspace / 'source manifest.json'\n"
                "normalized_manifest = workspace / 'normalized manifest.json'\n"
                "loader = workspace / 'load installed resource.py'\n"
                "loader.write_text(\"import runpy, sys\\nrunpy.run_path(sys.argv[1], "
                "run_name='installed_validation_resource')\\n\", encoding='utf-8')\n"
                "with freecad_scripts_directory() as scripts:\n"
                "    hashes_before = {name: hashlib.sha256((scripts / name).read_bytes()).hexdigest() "
                "for name in sorted(" + repr(tuple(EXPECTED_SCRIPTS)) + ")}\n"
                "def execute(name, arguments, timeout):\n"
                "    with freecad_scripts_directory() as scripts:\n"
                "        result = run_freecad_script(freecadcmd, scripts / name, arguments, "
                "timeout_seconds=timeout, expected_sha256=expected_freecad_sha256, "
                "expected_identity=freecad_pin.identity, controlled_directory=workspace)\n"
                "    if result.returncode != 0:\n"
                "        raise RuntimeError(name + ': ' + result.stderr[-4000:] + result.stdout[-4000:])\n"
                "    return result\n"
                "def validate(path, nonce):\n"
                "    result = execute('validate_working_copy.py', [path, nonce], 900)\n"
                "    prefix = 'MECHANICAL_DESIGN_FCSTD_VALIDATION_V1 '\n"
                "    if result.stderr or not result.stdout.startswith(prefix) or result.stdout.count('\\n') != 1:\n"
                "        raise RuntimeError('unexpected validation process output')\n"
                "    payload = json.loads(result.stdout[len(prefix):-1])\n"
                "    if payload['nonce'] != nonce:\n"
                "        raise RuntimeError('validation nonce mismatch')\n"
                "    return payload\n"
                "execute('create_empty_working_copy.py', [source], 120)\n"
                "source_before = hashlib.sha256(source.read_bytes()).hexdigest()\n"
                "execute('normalize_working_copy.py', [source, normalized], 120)\n"
                "source_after = hashlib.sha256(source.read_bytes()).hexdigest()\n"
                "execute('extract_model_manifest.py', [source, source_manifest], 900)\n"
                "execute('extract_model_manifest.py', [normalized, normalized_manifest], 900)\n"
                "source_validation = validate(source, 'source-agent-nonce-0000000000000001')\n"
                "normalized_validation = validate(normalized, 'normalized-agent-nonce-000000001')\n"
                "loaded = []\n"
                "for name in ['validate_external_step.py', 'validate_fastener_interfaces.py', "
                "'validate_mechanical_interfaces.py']:\n"
                "    with freecad_scripts_directory() as scripts:\n"
                "        result = run_freecad_script(freecadcmd, loader, [scripts / name], "
                "timeout_seconds=120, expected_sha256=expected_freecad_sha256, "
                "expected_identity=freecad_pin.identity, controlled_directory=workspace)\n"
                "    if result.returncode != 0:\n"
                "        raise RuntimeError(name + ': ' + result.stderr[-4000:] + result.stdout[-4000:])\n"
                "    loaded.append(name)\n"
                "with freecad_scripts_directory() as scripts:\n"
                "    hashes_after = {name: hashlib.sha256((scripts / name).read_bytes()).hexdigest() "
                "for name in sorted(" + repr(tuple(EXPECTED_SCRIPTS)) + ")}\n"
                "print(json.dumps({'source_exists': source.is_file(), "
                "'normalized_exists': normalized.is_file(), "
                "'source_unchanged': source_before == source_after, "
                "'source_manifest': json.loads(source_manifest.read_text(encoding='utf-8'))['schema_version'], "
                "'normalized_manifest': json.loads(normalized_manifest.read_text(encoding='utf-8'))['schema_version'], "
                "'source_validation': source_validation['schema_version'], "
                "'normalized_validation': normalized_validation['schema_version'], "
                "'hashes_before': hashes_before, 'hashes_after': hashes_after, "
                "'loaded': loaded}))\n"
            )
            result = subprocess.run(
                [str(python), "-c", script],
                cwd=root,
                env=installed_environment,
                capture_output=True,
                text=True,
                timeout=1200,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["hashes_before"], EXPECTED_SCRIPTS)
            self.assertEqual(payload["hashes_after"], EXPECTED_SCRIPTS)
            del payload["hashes_before"]
            del payload["hashes_after"]
            self.assertEqual(
                payload,
                {
                    "source_exists": True,
                    "normalized_exists": True,
                    "source_unchanged": True,
                    "source_manifest": "ModelManifest/v2",
                    "normalized_manifest": "ModelManifest/v2",
                    "source_validation": "MechanicalDesignWorkingCopyValidation/v2",
                    "normalized_validation": "MechanicalDesignWorkingCopyValidation/v2",
                    "loaded": [
                        "validate_external_step.py",
                        "validate_fastener_interfaces.py",
                        "validate_mechanical_interfaces.py",
                    ],
                },
            )
            for private_value in (
                str(PROJECT_ROOT),
                str(Path.home()),
                getpass.getuser(),
                platform.node(),
            ):
                if private_value:
                    self.assertNotIn(private_value, result.stdout)
                    self.assertNotIn(private_value, result.stderr)
