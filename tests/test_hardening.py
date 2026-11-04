import sys
import unittest

from vpush.hardening import harden


class HardeningTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_sets_no_new_privs_and_reports(self):
        with self.assertLogs("vpush.security", level="INFO") as logs:
            status = harden()
        self.assertTrue(status["no_new_privs"])
        self.assertIsInstance(status["uid"], int)
        self.assertIn("no_new_privs=True", "\n".join(logs.output))
        # Calling it again is harmless (the flag can't be unset anyway).
        self.assertTrue(harden()["no_new_privs"])

    @unittest.skipIf(sys.platform.startswith("linux"), "non-Linux only")
    def test_noop_elsewhere(self):
        self.assertIsNone(harden()["no_new_privs"])


if __name__ == "__main__":
    unittest.main()
