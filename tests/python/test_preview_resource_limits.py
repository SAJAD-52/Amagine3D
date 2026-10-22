from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "a3d-public"


class PreviewResourceLimitTests(unittest.TestCase):
    def test_os_boundary_blocks_growth_beyond_a_small_budget(self):
        script = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from preview_resource_limits import install_preview_memory_limit
boundary = install_preview_memory_limit(64 * 1024 * 1024)
try:
    _allocation = bytearray(128 * 1024 * 1024)
except MemoryError:
    print(json.dumps({"blocked": True, "boundary": boundary}))
else:
    print(json.dumps({"blocked": False, "boundary": boundary}))
'''
        result = subprocess.run([sys.executable, "-c", script, str(SKILL)], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["blocked"], payload)
        boundary = payload["boundary"]
        self.assertIn(boundary["kind"], {"address-space", "job-process-commit"})
        self.assertEqual(boundary["budgetBytes"], 64 * 1024 * 1024)
        self.assertGreater(boundary["limitBytes"], boundary["baselineBytes"])
        self.assertLessEqual(boundary["limitBytes"], boundary["baselineBytes"] + boundary["budgetBytes"])
        self.assertIn("brep_tessellation", boundary["baselineModules"])


if __name__ == "__main__":
    unittest.main()
