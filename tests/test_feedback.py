"""Copied-buffer feedback tests; no Qt/Krita required."""
import importlib.util
from pathlib import Path
import unittest

module=importlib.util.spec_from_file_location('feedback',Path(__file__).resolve().parents[1]/'pykrita/krita_mcp/feedback.py')
f=importlib.util.module_from_spec(module);module.loader.exec_module(f)


class FeedbackTests(unittest.TestCase):
    def test_linear_alpha_weighted_mean_and_empty(self):
        s=f.statistics(bytes([0,0,0,255,255,255,255,255,0,0,255,0]))
        self.assertEqual(s['linear_light_mean_rgb'],[188]*3)
        self.assertEqual(s['visible_count'],2)
        self.assertEqual(s['pixel_count'],3)
        self.assertTrue(f.statistics(bytes([1,2,3,0]))['empty'])
        self.assertIsNone(f.statistics(bytes([1,2,3,0]))['linear_light_mean_rgb'])

    def test_palette_ties_are_deterministic(self):
        self.assertEqual(f.statistics(bytes([0,0,255,255,255,0,0,255]))['palette'][0]['rgb'],[8,8,248])

    def test_changed_and_protected_bytes(self):
        a=bytes(16);z=bytes([1,0,0,0,0,0,0,0,0,0,255,255,0,0,0,0])
        result,_,v=f.measurements(a,z,2,2,bytes([0,255,255,0]))
        self.assertEqual(result['changed_pixels'],2)
        self.assertEqual(result['outside_allowed_pixels'],1)
        self.assertEqual(result['changed_bounds'],{'x':0,'y':0,'width':1,'height':2})
        self.assertEqual(v[3],255)

    def test_overlap_boundary_empty_semantics(self):
        a=bytes(16);t=bytes([0]*4)
        r,_,_=f.measurements(a,a,2,2,target=t)
        self.assertEqual(r['target_iou'],1);self.assertEqual(r['symmetric_boundary_distance'],0)
        z=bytes([0,0,255,255])+bytes(12)
        r,_,_=f.measurements(a,z,2,2,target=bytes([0,255,0,0]))
        self.assertEqual(r['target_iou'],0);self.assertEqual(r['target_coverage'],0)
        self.assertEqual(r['symmetric_boundary_distance'],1)

    def test_visible_changes_ignore_colour_under_transparency(self):
        a=bytes([0,0,0,0, 9,9,9,255, 0,0,0,0, 0,0,0,0])
        z=bytes([5,5,5,0, 9,9,9,255, 1,1,1,255, 0,0,0,0])
        r,_,_=f.measurements(a,z,2,2)
        self.assertEqual((r['changed_pixels'],r['changed_visible_pixels']),(2,1))
        self.assertEqual(r['alpha_iou'],0.5)
        self.assertEqual(f.measurements(a,a,2,2)[0]['alpha_iou'],1)


if __name__=='__main__':unittest.main(verbosity=2)
