import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import easypost as ep  # noqa: E402


class BuildParcelTest(unittest.TestCase):
    def test_parcel_converts_pounds_to_ounces_with_inch_dimensions(self):
        self.assertEqual(ep._build_parcel({"weight": "2.5", "length": "10", "width": "6", "height": "4"}),
                         {"length": 10.0, "width": 6.0, "height": 4.0, "weight": 40.0})

    def test_parcel_omits_dimensions_when_any_is_missing_or_zero(self):
        for parcel in ({"weight": "21.3", "length": "0", "width": "0", "height": "0"},
                       {"weight": "21.3"},
                       {"weight": "21.3", "length": "10", "width": "", "height": "abc"}):
            with self.subTest(parcel=parcel):
                self.assertEqual(ep._build_parcel(parcel), {"weight": 340.8})


if __name__ == "__main__":
    unittest.main()
