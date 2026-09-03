"""Endicia as its own provider, implemented on top of ShipStation.

Endicia (an Auctane brand, now merged into Stamps.com) steers new integrations
to the ShipStation API, and an Endicia account can be connected as a carrier
inside a ShipStation account. So rather than a second API client, this
platform is the ShipStation adapter locked to ONE connected carrier — the one
the admin picks in Settings:
  * rating and the services list cover only that carrier, so packers see
    Endicia's USPS services alone under the instance's alias;
  * rates and labels carry the alias as their carrier name instead of the
    ShipStation-side nickname ("Stamps.com");
  * label purchase, idempotency, void, manifests and test labels are inherited
    untouched.

Only the instance hooks are overridden; module helpers stay in shipstation.py
so its tests keep patching them in one place.
"""
from providers.base import ProviderError, origin_descriptor
from providers.shipstation import ShipStationProvider, _split_service_id

NOT_SELECTED = "Endicia carrier is not selected — pick it in Settings"
NOT_CONNECTED = "The selected carrier is no longer connected to the ShipStation account"


class EndiciaProvider(ShipStationProvider):
    platform = "endicia"
    label = "Endicia (via ShipStation)"

    def _selected_carrier_id(self):
        return (self.setting("carrier_id") or "").strip()

    def _selected_carrier(self, carriers):
        selected = self._selected_carrier_id()
        if not selected:
            raise ProviderError(NOT_SELECTED)
        for c in carriers:
            if c.get("carrier_id") == selected:
                return c
        raise ProviderError(NOT_CONNECTED)

    # ---- carrier hooks ----
    def rating_carriers(self, carriers):
        return [self._selected_carrier(carriers)]

    def carrier_names(self, carriers):
        # Every id maps to the alias: only the selected carrier is ever bought
        # under this instance, and labels bought before a later re-selection
        # must keep reading as this instance rather than as "stamps_com".
        names = {c.get("carrier_id"): self.label for c in carriers if c.get("carrier_id")}
        selected = self._selected_carrier_id()
        if selected:
            names[selected] = self.label
        return names

    def buy_labels(self, provider_shipment_ids, service_id):
        carrier_id, _ = _split_service_id(service_id)
        if carrier_id != self._selected_carrier_id():
            raise ProviderError("That service belongs to a different carrier than this Endicia integration")
        return super().buy_labels(provider_shipment_ids, service_id)

    def _connection_summary(self, carriers):
        carrier = self._selected_carrier(carriers)
        name = carrier.get("friendly_name") or carrier.get("carrier_code") or carrier.get("carrier_id")
        nickname = (carrier.get("nickname") or "").strip()
        if nickname and nickname != name:
            name = f"{name} · {nickname}"
        count = len(carrier.get("services") or [])
        return f"{self.label} — {name}, {count} service(s)" + self._test_labels_suffix()

    # ---- settings surface ----
    def descriptor(self):
        return {
            "name": self.name,
            "key": self.name,
            "platform": self.platform,
            "platform_label": self.platform_label,
            "label": self.label,
            "enabled": self.setting("enabled") == "true",
            "enabled_key": self.setting_key("enabled"),
            "modes": [],
            "fields": [
                {"key": self.setting_key("api_key"), "label": "ShipStation API key (v2)", "type": "secret",
                 "hint": "The same v2 key as your ShipStation integration (ShipStation → Settings → Account → "
                         "API Settings). Connect your Endicia account as a carrier in ShipStation first."},
                {"key": self.setting_key("carrier_id"), "label": "Endicia carrier", "type": "select",
                 "options_endpoint": f"/api/providers/{self.name}/carriers",
                 "hint": "Save the API key first, then pick the carrier connected to your Endicia account."},
                *self._label_fields(),
            ],
            "test_endpoint": f"/api/providers/{self.name}/test",
            "supports": {"service_exclusions": True},
            "services_endpoint": f"/api/providers/{self.name}/services",
            "excluded_endpoint": f"/api/providers/{self.name}/excluded-services",
            **origin_descriptor(self.name),
        }
