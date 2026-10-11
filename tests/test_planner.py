"""Geometry tests run offline without importing Krita or Qt."""
import copy
import importlib.util
from pathlib import Path
import unittest
from tests.stroke_spec import stroke_spec

module = importlib.util.spec_from_file_location('planner',Path(__file__).resolve().parents[1]/'pykrita/krita_mcp/stroke_planner.py')
planner = importlib.util.module_from_spec(module)
module.loader.exec_module(planner)


class PlannerTests(unittest.TestCase):
    def spec(self,geometry=None):
        return stroke_spec({'document':'session:example','layer':'uuid:example','scope':{'kind':'static'}},geometry)

    def test_endpoints_corners_and_determinism(self):
        s=self.spec({'kind':'polyline','points':[[10,10],[10,10],[50,10],[50,70]]})
        a=planner.plan(s)
        self.assertEqual(a,planner.plan(s))
        pts=[(p['x'],p['y']) for p in a['points']]
        self.assertEqual(pts[0],(10,10));self.assertEqual(pts[-1],(50,70));self.assertIn((50,10),pts)

    def test_bezier_tolerance_curvature_and_cusp(self):
        s=self.spec({'kind':'cubic','segments':[[[10,80],[10,10],[100,10],[110,80]]]})
        a=planner.plan(s)
        self.assertEqual((a['points'][0]['x'],a['points'][0]['y']),(10,80))
        self.assertLess(min(p['y'] for p in a['points']),30)
        self.assertTrue(all(planner.distance((p['x'],p['y']),(q['x'],q['y']))<=4.00001 for p,q in zip(a['points'],a['points'][1:])))
        s=self.spec({'kind':'cubic','segments':[[[10,50],[100,50],[100,50],[10,50]]]})
        self.assertGreater(max(p['x'] for p in planner.plan(s)['points']),70)

    def test_pressure_and_time_arclength(self):
        s=self.spec({'kind':'polyline','points':[[10,10],[50,10],[50,70]]})
        s['pressure_profile']['samples']=[{'at':0,'value':0},{'at':1,'value':1}]
        p=planner.plan(s)
        corner=next(x for x in p['points'] if x['x']==50 and x['y']==10)
        self.assertAlmostEqual(corner['pressure'],.4)
        self.assertAlmostEqual(corner['timestamp_ms'],200)
        self.assertEqual(p['points'][-1]['timestamp_ms'],500)

    def test_invalid_and_budget_are_explicit(self):
        s=self.spec()
        for key,value in [('version',True),('size',float('nan')),('opacity',2),('erase',1),('tilt',{'x':2,'y':0})]:
            with self.assertRaises(planner.PlanError):planner.plan(dict(s,**{key:value}))
        for points in ([[10,10],[10,10]],[[True,10],[11,10]],[[float('inf'),10],[11,10]]):
            with self.assertRaises(planner.PlanError):planner.plan(self.spec({'kind':'polyline','points':points}))
        s['limits']['spacing']=.1
        with self.assertRaises(planner.PlanError):planner.plan(s)
        s=self.spec();s['pressure_profile']['samples'][1]['at']=0
        with self.assertRaises(planner.PlanError):planner.plan(s)

    def test_tiny_path_still_has_two_endpoints(self):
        s=self.spec({'kind':'polyline','points':[[10,10],[10.000001,10]]})
        p=planner.plan(s)
        self.assertEqual(p['point_count'],2)
        self.assertGreater(p['points'][1]['timestamp_ms'],p['points'][0]['timestamp_ms'])


if __name__=='__main__':unittest.main(verbosity=2)
