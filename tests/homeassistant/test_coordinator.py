"""Adapter tests using real HA State objects, with recorder writes intercepted."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from types import SimpleNamespace

from custom_components.izypower_savings.config import normalize
from custom_components.izypower_savings.coordinator import SavingsCoordinator


def config(**extra):
    return normalize({"consumption_sensor": "sensor.home_power", "grid_sensor": "sensor.grid_power",
        "source_timestamp": "sensor.report_at", "base_rate": .25, **extra})


def states(hass, when, home=1000, grid=400):
    hass.states.async_set("sensor.home_power", str(home), {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_power", str(grid), {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.report_at", when.isoformat())


async def test_partial_day_and_recorded_prices(hass, freezer):
    freezer.move_to("2026-01-10 12:00:00+00:00")
    now = datetime.now(timezone.utc)
    cfg = config()
    c = SavingsCoordinator(hass, cfg)
    c.now = lambda: now
    writes = []
    with patch("custom_components.izypower_savings.coordinator.async_add_external_statistics",
               side_effect=lambda *args: writes.append(args)):
        await c.async_load()
        states(hass, now)
        await c.async_capture()
        now += timedelta(minutes=5)
        states(hass, now)
        await c.async_capture()
        result = c.journal.engine.summary("jour", now)
        assert result["benefit"] > 0
        assert result["measured_hours"] == .08
        assert {item[1]["statistic_id"] for item in writes} == {
            "izypower_savings:benefit", "izypower_savings:savings", "izypower_savings:revenue",
            "izypower_savings:with_installation", "izypower_savings:without_installation"}
        other = SavingsCoordinator(hass, cfg)
        other.now = lambda: now
        await other.async_load()
        assert other.journal.engine.summary("jour", now) == result


async def test_additional_load_is_optional_and_is_not_added_to_grid(hass):
    now = datetime.now(timezone.utc)
    c = SavingsCoordinator(hass, config(additional_consumption=["sensor.workshop_power"]))
    states(hass, now, home=1000, grid=5000)
    hass.states.async_set("sensor.workshop_power", "4", {"unit_of_measurement": "kW"})
    reading = c.reading(now)
    assert reading["consumption_w"] == 5000
    assert reading["grid_w"] == 5000


async def test_unavailable_additional_load_is_not_assumed_zero(hass):
    now = datetime.now(timezone.utc)
    c = SavingsCoordinator(hass, config(additional_consumption=["sensor.workshop_power"]))
    states(hass, now)
    hass.states.async_set("sensor.workshop_power", "unavailable")
    assert c.reading(now)["error"] == "source_unavailable"


async def test_separate_export_and_opposite_grid_sign(hass):
    now = datetime.now(timezone.utc)
    c = SavingsCoordinator(hass, config(invert_grid=True, export_sensor="sensor.export_power"))
    states(hass, now, grid=-400)
    hass.states.async_set("sensor.export_power", "200", {"unit_of_measurement": "W"})
    result = c.reading(now)
    assert result["grid_w"] == 400
    assert result["export_w"] == 200


async def test_stale_report_does_not_create_fake_ongoing_consumption(hass):
    now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
    c = SavingsCoordinator(hass, config())
    states(hass, now-timedelta(hours=1))
    assert c.reading(now)["error"] == "source_stale"


async def test_station_clock_does_not_refresh_independent_charger(hass):
    c = SavingsCoordinator(hass, config(additional_consumption=["sensor.charger"]))
    now = datetime.now(timezone.utc) + timedelta(hours=3)
    states(hass, now, home=400, grid=400)
    hass.states.async_set("sensor.charger", "7000", {"unit_of_measurement": "W"})
    station = SimpleNamespace(device_id="station", config_entry_id="provider")
    charger = SimpleNamespace(device_id="charger", config_entry_id="provider")
    entries = dict.fromkeys(["sensor.home_power", "sensor.grid_power", "sensor.report_at"], station)
    entries["sensor.charger"] = charger
    registry = SimpleNamespace(async_get=entries.get)
    with patch("custom_components.izypower_savings.coordinator.er.async_get", return_value=registry):
        assert c.reading(now)["error"] == "source_stale"
        # A fresh 0 W from the independent charger is a valid observation.
        hass.states.async_set_internal("sensor.charger", "0", {"unit_of_measurement": "W"},
                                       False, None, None, now.timestamp())
        assert c.reading(now)["consumption_w"] == 400


async def test_delayed_reports_resume_after_an_error_across_restart(hass):
    start = datetime.now(timezone.utc)
    now = start
    c = SavingsCoordinator(hass, config())
    c.now = lambda: now
    with patch("custom_components.izypower_savings.coordinator.async_add_external_statistics"):
        await c.async_load()
        states(hass, start)
        await c.async_capture()
        now = start + timedelta(minutes=10)
        hass.states.async_set("sensor.home_power", "unavailable")
        await c.async_capture()
        other = SavingsCoordinator(hass, config())
        other.now = lambda: now
        await other.async_load()
        assert other.error == "source_unavailable"
        states(hass, start)  # The same cached report cannot end the outage.
        await other.async_capture()
        assert other.error == "source_unavailable"
        states(hass, start + timedelta(minutes=2))
        await other.async_capture()
        assert other.error is None
        assert not other.journal.engine.has_interval
        states(hass, start + timedelta(minutes=7))
        await other.async_capture()
        assert other.journal.engine.has_interval
        assert sum(row["seconds"] for row in other.journal.engine.rows.values()) == 300
