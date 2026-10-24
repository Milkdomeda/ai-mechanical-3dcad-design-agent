# SPDX-License-Identifier: LGPL-2.0-or-later
"""FreeCAD unittest-style regression candidate; run in the native interpreter."""
import unittest
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
