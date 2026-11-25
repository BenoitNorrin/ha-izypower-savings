"""Estimated benefit for current periods, with separate savings and resale."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, NAME, SIGNAL, PERIODS


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([SavingsSensor(entry.runtime_data, key) for key in (*PERIODS, "status")])


class SavingsSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator, key):
        self.coordinator = coordinator
        self.key = key
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self.entity_id = f"sensor.{DOMAIN}_{key}"
        self._attr_translation_key = key
        if key != "status":
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = coordinator.config["currency"]
            self._attr_suggested_display_precision = 2
        else:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["waiting", "collecting", "source_error"]
        self._attr_device_info = {"identifiers": {(DOMAIN, "household")}, "name": NAME,
                                  "manufacturer": "Communauté", "model": "Estimation locale"}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.async_write_ha_state))

    @property
    def available(self):
        return self.key == "status" or (self.coordinator.ready and self.snapshot()["benefit"] is not None)

    def snapshot(self):
        engine = self.coordinator.journal.engine
        summary = engine.summary(self.key if self.key != "status" else "jour", self.coordinator.now())
        summary.update(error=self.coordinator.error,
                       last_sample=engine.last["measured_at"] if engine.last else None,
                       export_mode=engine.plans[-1][1]["export_mode"] if engine.plans else "none",
                       method="Puissances interpolées et tarifs historisés", excludes_subscription=True)
        return summary

    @property
    def native_value(self):
        if self.key == "status":
            if self.coordinator.error:
                return "source_error"
            return "collecting" if self.coordinator.journal.engine.has_interval else "waiting"
        value = self.snapshot()["benefit"]
        return round(value, 6) if value is not None else None

    @property
    def extra_state_attributes(self):
        return self.snapshot()
