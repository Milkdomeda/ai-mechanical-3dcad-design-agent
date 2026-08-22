import json
import tempfile
import unittest
from pathlib import Path

from mechanical_design_agent.assembly import validate_assembly_completeness


def _manifest(report_path: str):
    return {
        "schema_version": "AssemblyCompleteness/v2",
        "working_copy_id": "00000000-0000-0000-0000-000000000011",
        "working_sha256": "a" * 64,
        "ground_component_id": "base",
        "components": [
            {"id": "base", "bom_item_id": "B1", "role": "ground", "interface_level": "detailed"},
            {"id": "slide", "bom_item_id": "B2", "role": "moving", "interface_level": "detailed"},
        ],
        "bom": [
            {"id": "B1", "quantity": 1},
            {"id": "B2", "quantity": 1},
            {"id": "F1", "quantity": 3},
        ],
        "joints": [{
            "id": "J1",
            "a": "base",
            "b": "slide",
            "kind": "fastened",
            "interface_a": "rail",
            "interface_b": "carriage",
            "fastener_bom_item_ids": ["F1"],
            "fastener_quantity": 3,
            "fastener_object_ids": ["Bolt_001", "Washer_001", "Nut_001"],
        }],
        "functional_load_paths": [{"id": "P1", "components": ["slide", "base"]}],
        "motion_checks": [{"component_id": "slide", "status": "passed", "evidence": "sweep"}],
        "fastener_geometry_checks": [{
            "id": "FG1",
            "status": "passed",
            "validator": "freecad-model-validation",
            "working_sha256": "a" * 64,
            "report_path": report_path,
        }],
        "design_boundary": [{"id": "drive", "interface": "shaft", "owner": "customer", "status": "open"}],
    }


class AssemblyCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.report_path = Path(self.temp_directory.name) / "report.json"
        self.report_path.write_text(json.dumps({
            "status": "passed",
            "validator": "freecad-model-validation",
            "kind": "FCStd",
            "working_sha256": "a" * 64,
            "summary": {"fasteners_detected": 3},
            "fastener_inventory": [
                {"object_id": "Bolt_001", "contract_check_id": "fastener.Bolt_001.contract"},
                {"object_id": "Washer_001", "contract_check_id": "fastener.Washer_001.contract"},
                {"object_id": "Nut_001", "contract_check_id": "fastener.Nut_001.contract"},
            ],
            "checks": [
                {"id": "fastener.coverage", "validator": "freecad-model-validation", "mandatory": True, "status": "passed", "actual": 3},
                {"id": "fastener.Bolt_001.contract", "validator": "freecad-model-validation", "mandatory": True, "status": "passed"},
                {"id": "fastener.Washer_001.contract", "validator": "freecad-model-validation", "mandatory": True, "status": "passed"},
                {"id": "fastener.Nut_001.contract", "validator": "freecad-model-validation", "mandatory": True, "status": "passed"},
                {"id": "fastener-set.J1-01.assembly", "validator": "freecad-model-validation", "mandatory": True, "status": "passed"},
            ],
        }), encoding="utf-8")

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_complete_assembly_passes(self):
        self.assertEqual(validate_assembly_completeness(_manifest(str(self.report_path)))["status"], "passed")

    def test_v1_manifest_is_rejected(self):
        manifest = _manifest(str(self.report_path))
        manifest["schema_version"] = "AssemblyCompleteness/v1"
        result = validate_assembly_completeness(manifest)
        self.assertEqual(result["status"], "failed")
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("manifest.schema", failed)

    def test_orphan_and_envelope_placeholder_fail(self):
        manifest = _manifest(str(self.report_path))
        manifest["components"].append({"id": "orphan", "bom_item_id": "B3", "role": "fixed", "interface_level": "envelope_only", "placeholder": True})
        manifest["bom"].append({"id": "B3", "quantity": 1})
        result = validate_assembly_completeness(manifest)
        self.assertEqual(result["status"], "failed")
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertTrue({"assembly.connected", "placeholder.no-envelope-only", "placeholder.interface-contract"} <= failed)

    def test_fastened_joint_without_geometry_evidence_fails(self):
        manifest = _manifest(str(self.report_path))
        manifest["fastener_geometry_checks"] = []
        result = validate_assembly_completeness(manifest)
        self.assertEqual(result["status"], "failed")
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fastener-model-evidence", failed)

    def test_one_detected_fastener_omitted_from_joint_fails(self):
        manifest = _manifest(str(self.report_path))
        manifest["joints"][0]["fastener_object_ids"].remove("Washer_001")
        manifest["joints"][0]["fastener_quantity"] = 2
        result = validate_assembly_completeness(manifest)
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fastener-occurrence-coverage", failed)

    def test_fastener_assigned_to_two_joints_fails(self):
        manifest = _manifest(str(self.report_path))
        duplicate = dict(manifest["joints"][0])
        duplicate.update({
            "id": "J2",
            "fastener_quantity": 1,
            "fastener_object_ids": ["Washer_001"],
        })
        manifest["joints"].append(duplicate)
        result = validate_assembly_completeness(manifest)
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fastener-occurrence-coverage", failed)

    def test_unknown_fastener_object_fails(self):
        manifest = _manifest(str(self.report_path))
        manifest["joints"][0]["fastener_object_ids"].append("ManualBolt_999")
        manifest["joints"][0]["fastener_quantity"] = 4
        result = validate_assembly_completeness(manifest)
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fastener-occurrence-coverage", failed)

    def test_stale_model_validation_report_fails(self):
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        report["working_sha256"] = "b" * 64
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        result = validate_assembly_completeness(_manifest(str(self.report_path)))
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fastener-model-evidence", failed)

    def test_failed_fastener_contract_in_report_fails(self):
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        report["status"] = "failed"
        report["checks"][1]["status"] = "failed"
        self.report_path.write_text(json.dumps(report), encoding="utf-8")
        result = validate_assembly_completeness(_manifest(str(self.report_path)))
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fastener-model-evidence", failed)

    def test_fastener_quantity_must_equal_assigned_occurrences(self):
        manifest = _manifest(str(self.report_path))
        manifest["joints"][0]["fastener_quantity"] = 4
        result = validate_assembly_completeness(manifest)
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("joints.fasteners", failed)

    def test_declared_critical_interface_requires_same_revision_report(self):
        manifest = _manifest(str(self.report_path))
        manifest["critical_interfaces"] = [{"id": "support-axis", "kind": "axis_concentricity"}]
        result = validate_assembly_completeness(manifest)
        self.assertEqual(result["status"], "failed")
        failed = {item["id"] for item in result["checks"] if item["status"] == "failed"}
        self.assertIn("interfaces.mechanical-geometry-evidence", failed)


if __name__ == "__main__":
    unittest.main()
