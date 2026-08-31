import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")

sys.modules.setdefault("db", types.SimpleNamespace(
    get_setting=lambda *a, **k: None, set_setting=lambda *a, **k: None,
    query=lambda *a, **k: None, execute=lambda *a, **k: None))

from providers.easypost import EasyPostProvider  # noqa: E402
from providers.easyship import EasyshipProvider  # noqa: E402
from providers.shippo import ShippoProvider  # noqa: E402
from providers.shipstation import ShipStationProvider  # noqa: E402

DB = sys.modules["db"]
PROVIDER_CLASSES = (EasyshipProvider, ShippoProvider, EasyPostProvider, ShipStationProvider)

DESTINATION = {"company": "Acme Corp", "contact": "Jane", "address1": "1 Main St",
               "city": "Austin", "state": "TX", "zip": "78701"}


class PrepareDestinationTest(unittest.TestCase):
    def test_checked_blanks_the_company_without_mutating_the_input(self):
        for cls in PROVIDER_CLASSES:
            with self.subTest(platform=cls.platform), \
                 patch.object(DB, "get_setting",
                              lambda key, default=None: "true" if key.endswith("_no_company") else default):
                out = cls().prepare_destination(DESTINATION)
                self.assertEqual(out, {**DESTINATION, "company": ""})
                self.assertEqual(DESTINATION["company"], "Acme Corp")

    def test_unset_setting_leaves_the_destination_untouched(self):
        for cls in PROVIDER_CLASSES:
            with self.subTest(platform=cls.platform):
                self.assertIs(cls().prepare_destination(DESTINATION), DESTINATION)


class DescriptorFieldTest(unittest.TestCase):
    def test_every_platform_exposes_the_no_company_checkbox(self):
        """The descriptor entry is also what whitelists the setting key in
        settings_api._provider_setting_keys, so its shape matters."""
        for cls in PROVIDER_CLASSES:
            with self.subTest(platform=cls.platform):
                provider = cls("inst-1", "Alias")
                fields = provider.descriptor()["fields"]
                match = [f for f in fields if f["key"] == "inst-1_no_company"]
                self.assertEqual(match, [{"key": "inst-1_no_company", "label": "No Company",
                                          "type": "checkbox", "hint": match[0]["hint"] if match else None}])


if __name__ == "__main__":
    unittest.main()
