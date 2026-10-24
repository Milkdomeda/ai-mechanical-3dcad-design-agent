# SPDX-License-Identifier: LGPL-2.0-or-later
"""FreeCAD unittest-style regression candidate; run in the native interpreter."""
import unittest
import math
from types import SimpleNamespace
from unittest.mock import patch
import FreeCAD
from femmesh import meshtools


class TestFaceForceR1(unittest.TestCase):
    def areas(self,points):
        nodes={i:FreeCAD.Vector(*p) for i,p in enumerate(points,1)}
        return [v for _,v in meshtools.get_ref_facenodes_areas(nodes,{1:tuple(nodes)})]

    def test_straight_t6(self):
        actual=self.areas(((0,0,0),(1,0,0),(0,1,0),(.5,0,0),(.5,.5,0),(0,.5,0)))
        for a,b in zip(actual,(0,0,0,1/6,1/6,1/6)):self.assertAlmostEqual(a,b,delta=2e-13)

    def test_curved_t6_independent_golden(self):
        actual=self.areas(((0,0,0),(1,0,.25),(0,1,0),(.5,0,.0625),(.5,.5,.0625),(0,.5,0)))
        # Independent analytic s integration + adaptive 1-D Simpson reference.
        golden=(-.0006769129788454595,.0019976474249444806,-.0006769129788454595,.17074533398872924,.17074533398872924,.1680376820733474)
        for a,b in zip(actual,golden):self.assertAlmostEqual(a,b,delta=2e-13)

    def test_legacy_faces(self):
        square=((0,0,0),(1,0,0),(1,1,0),(0,1,0))
        for points,want in ((square[:3],(1/6,)*3),(square,(.25,)*4),(square+((.5,0,0),(1,.5,0),(.5,1,0),(0,.5,0)),(-1/12,)*4+(1/3,)*4)):
            for a,b in zip(self.areas(points),want):self.assertAlmostEqual(a,b,delta=2e-13)

    def test_folded_face_rejected(self):
        with self.assertRaises(ValueError):self.areas(((0,0,0),(1,0,0),(0,1,0),(2,2,0),(.5,.5,0),(0,.5,0)))

    def test_extreme_area_uses_real_native_quantity(self):
        # Only reference lookup is doubled; native integrator, global force
        # table and FreeCAD's C++ Quantity execute together.
        points=((0,0,0),(1,0,0),(0,1,0),(.5,0,0),(.5,.5,0),(0,.5,0))
        obj=SimpleNamespace(Name='Fixture')
        load=SimpleNamespace(Force=FreeCAD.Units.Quantity('40 N'),References=[(obj,('Face1',))])
        for scale in (1e-154,1e154):
            nodes={i:FreeCAD.Vector(*(v*scale for v in p)) for i,p in enumerate(points,1)}
            with patch.object(meshtools,'sub_shape_at_global_placement',return_value=SimpleNamespace(ShapeType='Face',Area=1.)),patch.object(meshtools,'get_ref_facenodes_table',return_value={1:tuple(nodes)}):
                result=meshtools.get_force_obj_face_nodeload_table(None,{},nodes,load)
            # Native meshtools intentionally returns scalar quantities already
            # expressed in N, matching its existing writer-facing contract.
            expected_unit=FreeCAD.Units.Quantity(load.Force.getValueAs('N')).Unit
            self.assertTrue(all(v.Unit==expected_unit for _,table in result for v in table.values()))
            values=[float(v.Value) for _,table in result for v in table.values()]
            self.assertTrue(all(math.isfinite(v) for v in values))
            self.assertAlmostEqual(math.fsum(values),40.,delta=1e-12)
            for actual,want in zip(values,(0,0,0,40/3,40/3,40/3)):
                self.assertAlmostEqual(actual,want,delta=1e-12)
