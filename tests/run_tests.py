"""Run the suite and write results/test_results.json (id, description, status, seconds)."""
import json
import os
import sys
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_platform  # noqa: E402


class Rec(unittest.TextTestResult):
    rows = []

    def _add(self, test, status):
        self.rows.append({"id": test.id().split(".", 1)[1], "description": (test.shortDescription() or "").strip(),
                          "status": status})

    def addSuccess(self, t): super().addSuccess(t); self._add(t, "PASS")
    def addFailure(self, t, e): super().addFailure(t, e); self._add(t, "FAIL")
    def addError(self, t, e): super().addError(t, e); self._add(t, "ERROR")
    def addSkip(self, t, r): super().addSkip(t, r); self._add(t, "SKIP")


suite = unittest.defaultTestLoader.loadTestsFromModule(test_platform)
t0 = time.time()
res = unittest.TextTestRunner(verbosity=2, resultclass=Rec).run(suite)
os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
with open(os.path.join(ROOT, "results", "test_results.json"), "w") as f:
    json.dump({"total": res.testsRun, "failures": len(res.failures), "errors": len(res.errors),
               "skipped": len(res.skipped), "seconds": round(time.time() - t0, 2), "tests": Rec.rows}, f, indent=2)
sys.exit(not res.wasSuccessful())
