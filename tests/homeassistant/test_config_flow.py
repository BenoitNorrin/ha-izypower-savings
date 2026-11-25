"""Exercise Home Assistant's actual flow manager and selector schemas."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant.common import MockConfigEntry

from custom_components.izypower_savings.config import normalize
from custom_components.izypower_savings.const import DOMAIN

SOURCES = {"consumption_sensor": "sensor.home_power", "grid_sensor": "sensor.grid_power", "currency": "EUR"}
FLAT = {"base_rate": .25, "off_peak_enabled": False, "super_off_peak_enabled": False}


async def start_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "sensors"
    return result


@pytest.mark.parametrize("mode", ["none", "unpaid", "paid"])
async def test_complete_flow_without_ev_charger(hass, mode):
    with patch("custom_components.izypower_savings.async_setup_entry", new=AsyncMock(return_value=True)):
        result = await start_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], SOURCES)
        assert result["step_id"] == "tariffs"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], FLAT)
        assert result["step_id"] == "export"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"export_mode": mode, "export_rate": .1})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"]["additional_consumption"] == []
        assert result["data"]["export_mode"] == mode
        await hass.async_block_till_done()


async def test_invalid_windows_are_reported_in_ui(hass):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SOURCES)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        **FLAT, "off_peak_enabled": True, "off_peak_rate": .1, "off_peak_windows": "not-a-time"})
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_windows"}


async def test_duplicate_consumption_is_rejected(hass):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        **SOURCES, "additional_consumption": ["sensor.home_power"]})
    assert result["errors"] == {"base": "duplicate_source"}


@pytest.mark.parametrize("field", ["consumption_sensor", "grid_sensor", "additional_consumption", "export_sensor"])
async def test_energy_is_rejected_at_each_power_field(hass, field):
    hass.states.async_set("sensor.counter", "100", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.plain_power", "0", {"unit_of_measurement": "kW"})
    result = await start_flow(hass)
    values = {**SOURCES, field: ["sensor.counter"] if field == "additional_consumption" else "sensor.counter"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    assert result["errors"] == {field: "invalid_power_unit"}
    values[field] = ["sensor.plain_power"] if field == "additional_consumption" else "sensor.plain_power"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    assert result["step_id"] == "tariffs"


async def test_invalid_report_clock_is_flagged_in_options(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=normalize({**SOURCES, **FLAT}))
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.wrong_clock", "100", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    values = {k: v for k, v in SOURCES.items() if k != "currency"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        **values, "source_timestamp": "sensor.wrong_clock"})
    assert result["errors"] == {"source_timestamp": "invalid_timestamp"}


async def test_super_off_peak_can_be_configured(hass):
    with patch("custom_components.izypower_savings.async_setup_entry", new=AsyncMock(return_value=True)):
        result = await start_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], SOURCES)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            **FLAT, "super_off_peak_enabled": True, "super_off_peak_rate": .05, "super_off_peak_windows": "02:00-04:00"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"export_mode": "unpaid"})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"]["super_off_peak_enabled"]
        await hass.async_block_till_done()


async def test_single_household_only(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=normalize({**SOURCES, **FLAT}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_can_remove_optional_sensors(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=normalize({**SOURCES, **FLAT,
        "additional_consumption": ["sensor.optional_load"], "export_sensor": "sensor.export_power"}))
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "sensors"
    sources = {k: v for k, v in SOURCES.items() if k != "currency"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], sources)
    result = await hass.config_entries.options.async_configure(result["flow_id"], FLAT)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"export_mode": "unpaid"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["additional_consumption"] == []
    assert result["data"]["export_sensor"] == ""


async def test_extra_load_can_be_a_workshop_instead_of_a_charger(hass):
    with patch("custom_components.izypower_savings.async_setup_entry", new=AsyncMock(return_value=True)):
        result = await start_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            **SOURCES, "additional_consumption": ["sensor.workshop_power"]})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], FLAT)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"export_mode": "none"})
        assert result["data"]["additional_consumption"] == ["sensor.workshop_power"]
        await hass.async_block_till_done()
