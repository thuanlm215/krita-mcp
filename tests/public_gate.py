"""Normal-install rejection and retained integration coverage in one instance."""
import unittest
import tests.test_mcp
from tests.test_removed_execution import RemovedExecutionTests
from tests.test_frame_repair import FrameRepairTests

if __name__ == '__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(RemovedExecutionTests)
    suite.addTest(FrameRepairTests('test_p5_fractional_alpha_png_atlas_exact'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(tests.test_mcp.main() if result.wasSuccessful() else 1)
