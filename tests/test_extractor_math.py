from __future__ import annotations

import importlib.util
import math
import unittest

from mechanical_design_agent.package_resources import freecad_scripts_directory


with freecad_scripts_directory() as scripts:
    script = scripts / "extract_model_manifest.py"
    spec = importlib.util.spec_from_file_location("extract_model_manifest", script)
    assert spec and spec.loader
    EXTRACTOR = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(EXTRACTOR)


class ExtractorMathTests(unittest.TestCase):
    def test_principal_inertia_preserves_eigenvalues_and_orthogonal_axes(self) -> None:
        result = EXTRACTOR._principal_inertia(
            [
                [2.0, 1.0, 0.0],
                [1.0, 2.0, 0.0],
                [0.0, 0.0, 4.0],
            ]
        )
        self.assertEqual([round(value, 9) for value in result["principal_moments"]], [1.0, 3.0, 4.0])
        axes = result["principal_axes"]
        for axis in axes:
            self.assertAlmostEqual(math.sqrt(sum(value * value for value in axis)), 1.0, places=8)
        for left in range(3):
            for right in range(left + 1, 3):
                self.assertAlmostEqual(sum(axes[left][i] * axes[right][i] for i in range(3)), 0.0, places=8)

    def test_closed_mesh_mass_properties_match_unit_cube(self) -> None:
        vertices = [
            [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
            [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
        ]
        triangles = [
            [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4], [3, 7, 6], [3, 6, 2],
            [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5],
        ]
        result = EXTRACTOR._mesh_mass_properties(vertices, triangles)
        self.assertAlmostEqual(result["volume_mm3"], 1.0, places=9)
        for coordinate in result["center_of_mass_mm"]:
            self.assertAlmostEqual(coordinate, 0.5, places=9)
        for index in range(3):
            self.assertAlmostEqual(result["matrix"][index][index], 1.0 / 6.0, places=9)
        for row, column in ((0, 1), (0, 2), (1, 2)):
            self.assertAlmostEqual(result["matrix"][row][column], 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
