import sys
import types
import unittest

# backoffice imports pymssql (absent on the host) and db at module load; stub both.
sys.modules.setdefault("pymssql", types.SimpleNamespace(connect=lambda **k: None))
sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

import backoffice  # noqa: E402


class CleanTrackingTest(unittest.TestCase):
    def test_zero_and_blank_placeholders_mean_no_tracking(self):
        cases = {None: "", "": "", "   ": "", "0": "", " 0 ": ""}
        self.assertEqual({k: backoffice._clean_tracking(k) for k in cases}, cases)

    def test_real_numbers_pass_through_trimmed(self):
        self.assertEqual(backoffice._clean_tracking(" 1Z999AA10123456784 "), "1Z999AA10123456784")

    def test_zero_prefixed_numbers_are_not_placeholders(self):
        self.assertEqual(backoffice._clean_tracking("0123456789"), "0123456789")


if __name__ == "__main__":
    unittest.main()
