"""Select household sensors, daily tariff windows and export remuneration."""
from __future__ import annotations

import probatio

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .config import DEFAULTS, normalize
from .const import DOMAIN, ENTRY_MINOR_VERSION, NAME
from .model import build_plan, stamp


def _entity(multiple=False):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", multiple=multiple))


def _number(unit, minimum=0, maximum=None, step="any"):
    kwargs = {"min": minimum, "step": step, "mode": selector.NumberSelectorMode.BOX,
              "unit_of_measurement": unit}
    if maximum is not None:
        kwargs["max"] = maximum
    return selector.NumberSelector(selector.NumberSelectorConfig(**kwargs))


def sensors_schema(values, *, options=False, advanced=False):
    fields = {}
    for key in ("consumption_sensor", "grid_sensor"):
        marker = probatio.Required(key, default=values[key]) if values.get(key) else probatio.Required(key)
        fields[marker] = _entity()
    fields[probatio.Optional("additional_consumption", default=[])] = _entity(True)
    for key in ("export_sensor", "source_timestamp"):
        fields[probatio.Optional(key)] = _entity()
    if not options:
        fields[probatio.Required("currency", default=values.get("currency", "EUR"))] = selector.TextSelector()
    if advanced:
        fields[probatio.Optional("invert_grid", default=values.get("invert_grid", False))] = selector.BooleanSelector()
        fields[probatio.Optional("max_age_seconds", default=values.get("max_age_seconds", 900))] = _number("s", 0, 86400, 1)
        fields[probatio.Optional("max_gap_seconds", default=values.get("max_gap_seconds", 900))] = _number("s", 60, 3600, 1)
    return probatio.Schema(fields)


def tariffs_schema(values):
    rate = _number(f"{values.get('currency', 'EUR')}/kWh")
    base = probatio.Required("base_rate", default=values["base_rate"]) if "base_rate" in values else probatio.Required("base_rate")
    fields = {base: rate}
    for name in ("off_peak", "super_off_peak"):
        fields[probatio.Required(f"{name}_enabled", default=values.get(f"{name}_enabled", False))] = selector.BooleanSelector()
        fields[probatio.Optional(f"{name}_rate", default=values.get(f"{name}_rate", 0.))] = rate
        fields[probatio.Optional(f"{name}_windows", default=values.get(f"{name}_windows", ""))] = selector.TextSelector()
    return probatio.Schema(fields)


def export_schema(values):
    return probatio.Schema({
        probatio.Required("export_mode", default=values.get("export_mode", "none")): selector.SelectSelector(
            selector.SelectSelectorConfig(options=["none", "unpaid", "paid"], translation_key="export_mode")),
        probatio.Optional("export_rate", default=values.get("export_rate", 0.)): _number(f"{values.get('currency', 'EUR')}/kWh"),
    })


class _Steps:
    _values: dict
    _is_options = False

    async def async_step_sensors(self, user_input=None):
        errors = {}
        if user_input is not None:
            # An optional selector omitted after clearing it must not retain its
            # previous value from an options flow.
            cleaned = {"additional_consumption": [], "export_sensor": "", "source_timestamp": "", **user_input}
            for key in ("export_sensor", "source_timestamp"):
                cleaned[key] = cleaned.get(key) or ""
            candidate = {**self._values, **cleaned}
            try:
                normalize({"base_rate": 0., **candidate})
                errors.update(self._sensor_errors(candidate))
                if not errors:
                    self._values = candidate
                    return await self.async_step_tariffs()
            except (ValueError, KeyError, TypeError) as exc:
                errors["base"] = str(exc) if str(exc) in ("duplicate_source", "invalid_currency", "invalid_interval") else "invalid_configuration"
        values = candidate if user_input is not None else self._values
        schema = sensors_schema(values, options=self._is_options,
                                advanced=getattr(self, "show_advanced_options", False))
        suggested = {key: values[key] for key in (
            "additional_consumption", "export_sensor", "source_timestamp") if values.get(key)}
        return self.async_show_form(step_id="sensors", data_schema=self.add_suggested_values_to_schema(schema, suggested),
                                    errors=errors)

    def _sensor_errors(self, values):
        errors = {}
        registry = er.async_get(self.hass)
        for field in ("consumption_sensor", "grid_sensor", "additional_consumption", "export_sensor"):
            entities = values[field] if field == "additional_consumption" else [values[field]]
            for entity in filter(None, entities):
                state = self.hass.states.get(entity)
                entry = registry.async_get(entity)
                unit = state.attributes.get("unit_of_measurement") if state else None
                if unit is None and entry:
                    unit = entry.unit_of_measurement
                if unit is not None and unit not in ("W", "kW"):
                    errors[field] = "invalid_power_unit"
                elif unit is None and state and state.state not in ("unknown", "unavailable"):
                    errors[field] = "invalid_power_unit"
        if values["source_timestamp"]:
            state = self.hass.states.get(values["source_timestamp"])
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    stamp(state.state)
                except (ValueError, OverflowError):
                    errors["source_timestamp"] = "invalid_timestamp"
        return errors

    async def async_step_tariffs(self, user_input=None):
        errors = {}
        if user_input is not None:
            candidate = {**self._values, **user_input}
            try:
                build_plan(candidate)
                self._values = candidate
                return await self.async_step_export()
            except (ValueError, KeyError, TypeError) as exc:
                errors["base"] = "invalid_windows" if str(exc) == "invalid_windows" else "invalid_rate"
        return self.async_show_form(step_id="tariffs", data_schema=tariffs_schema(self._values), errors=errors)

    async def async_step_export(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                config = normalize({**self._values, **user_input})
                return self.async_create_entry(title="" if self._is_options else NAME, data=config)
            except (ValueError, KeyError, TypeError):
                errors["base"] = "invalid_rate"
        return self.async_show_form(step_id="export", data_schema=export_schema(self._values), errors=errors)


class SavingsConfigFlow(_Steps, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = ENTRY_MINOR_VERSION

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        self._values = {**DEFAULTS,
            "currency": getattr(self.hass.config, "currency", "EUR") or "EUR",
            "timezone": self.hass.config.time_zone}
        return await self.async_step_sensors(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SavingsOptionsFlow()


class SavingsOptionsFlow(_Steps, config_entries.OptionsFlow):
    _is_options = True

    async def async_step_init(self, user_input=None):
        self._values = {**self.config_entry.data, **self.config_entry.options}
        return await self.async_step_sensors(user_input)
