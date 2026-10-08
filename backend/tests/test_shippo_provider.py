import sys
import types
import unittest

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers import shippo as sp  # noqa: E402
from providers.base import ProviderError  # noqa: E402


class BuildParcelTest(unittest.TestCase):
    def test_parcel_uses_inches_and_pounds(self):
        self.assertEqual(sp._build_parcel({"weight": "2.5", "length": "10", "width": "6", "height": "4"}), {
            "length": "10.0", "width": "6.0", "height": "4.0", "distance_unit": "in",
            "weight": "2.5", "mass_unit": "lb",
        })

    def test_parcel_refuses_missing_or_zero_dimensions(self):
        # Shippo requires dimensions; inventing a 1-inch side would make USPS
        # quote a cubic tier the real box is then not billed at.
        for parcel in ({"weight": "21.3", "length": "0", "width": "0", "height": "0"},
                       {"weight": "21.3"},
                       {"weight": "21.3", "length": "10", "width": "", "height": "abc"}):
            with self.subTest(parcel=parcel):
                with self.assertRaises(ProviderError):
                    sp._build_parcel(parcel)


if __name__ == "__main__":
    unittest.main()
