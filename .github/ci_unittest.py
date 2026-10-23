"""unittest discover that prints each failure's traceback as it happens (CI, E2.T2), so a run that
dies or hangs part way still shows what failed. Same arguments as `-m unittest discover`."""
import os
import sys
import unittest

sys.path[0] = os.getcwd()   # as `python -m unittest` run from the checkout


class Immediate(unittest.TextTestResult):
    def _now(self, test, err):
        self.stream.writeln("\n" + "=" * 70 + "\n%s\n%s" % (test, self._exc_info_to_string(err, test)))
        self.stream.flush()

    def addError(self, test, err):
        super().addError(test, err)
        self._now(test, err)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._now(test, err)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._now(subtest, err)


if __name__ == "__main__":
    unittest.main(module=None, argv=["unittest", "discover", *sys.argv[1:]],
                  testRunner=unittest.TextTestRunner(resultclass=Immediate, verbosity=2))
