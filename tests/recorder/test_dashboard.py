"""Read the dashboard's actual bar queries after restarting HA and SQLite."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml
from homeassistant import loader
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics, statistics_during_period
from homeassistant.helpers import frame
from pytest_homeassistant.common import MockConfigEntry, async_test_home_assistant

from custom_components.izypower_savings.config import normalize
from custom_components.izypower_savings.const import DOMAIN, ENTRY_MINOR_VERSION
from custom_components.izypower_savings.coordinator import SavingsCoordinator


@pytest.mark.parametrize("zone,upgrade", [("Europe/Paris", False), ("Asia/Kolkata", True)])
async def test_dashboard_bars_survive_restart(tmp_path, async_test_recorder, zone, upgrade):
    tz = ZoneInfo(zone)
    config = normalize({"timezone": zone, "base_rate": .24, "export_mode": "paid", "export_rate": .12,
                        "consumption_sensor": "sensor.load", "grid_sensor": "sensor.grid",
                        "source_timestamp": "sensor.clock", "max_age_seconds": 0})
    # Gains in EUR: Nov 30 +.06, Dec 1 +.12, Dec 31 -.02, Jan 1 -.02, Jan 2 +.12.
    points = [
        ("2025-11-30T12:00", 1000, 0), ("2025-11-30T12:15", 1000, 0),
        ("2025-12-01T12:00", 2000, 0), ("2025-12-01T12:15", 2000, 0),
        ("2025-12-31T23:55", 0, 1000), ("2026-01-01T00:05", 0, 1000),
        ("2026-01-02T12:00", 1000, -2000), ("2026-01-02T12:15", 1000, -2000),
    ]
    now = datetime.fromisoformat(points[0][0]).replace(tzinfo=tz)
    config_dir = str(tmp_path)
    db = {"db_url": f"sqlite:///{tmp_path / 'recorder.db'}", "auto_purge": False}
    sid = f"{DOMAIN}:benefit"
    dashboard = Path(__file__).resolve().parents[2] / "examples/dashboard.yaml"
    views = yaml.safe_load(dashboard.read_text())["views"]
    expected = {
        "day": {"2025-11-30": .06, "2025-12-01": .12, "2025-12-31": -.02, "2026-01-01": -.02, "2026-01-02": .12},
        "month": {"2025-11-01": .06, "2025-12-01": .10, "2026-01-01": .10},
        "year": {"2025-01-01": .16, "2026-01-01": .10},
    }

    @asynccontextmanager
    async def boot(minor_version):
        async with async_test_home_assistant(config_dir=config_dir) as hass:
            frame.async_setup(hass)
            hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
            await hass.config.async_set_time_zone(zone)
            entry = MockConfigEntry(domain=DOMAIN, entry_id="solar", data=config, minor_version=minor_version)
            entry.add_to_hass(hass)
            try:
                async with async_test_recorder(hass, db) as recorder:
                    with patch.object(SavingsCoordinator, "now", staticmethod(lambda: now.astimezone(timezone.utc))):
                        assert await hass.config_entries.async_setup(entry.entry_id)
                        try:
                            yield hass, recorder, entry
                        finally:
                            await hass.config_entries.async_unload(entry.entry_id)
            finally:
                await hass.async_stop(force=True)

    async def check_bars(hass, recorder):
        await recorder.async_block_till_done()
        seen = set()
        for view in views:
            card = view["sections"][0]["cards"][0]
            period = card["period"]
            assert card["chart_type"] == "bar"
            assert card["stat_types"] == ["change"]
            assert card["entities"][0]["entity"] == sid
            rows = await recorder.async_add_executor_job(
                statistics_during_period, hass, datetime(2025, 11, 1, tzinfo=tz),
                datetime(2026, 1, 3, tzinfo=tz), {sid}, period, None, {"change"})
            amounts = {datetime.fromtimestamp(row["start"], tz).date().isoformat(): row["change"]
                       for row in rows[sid]}
            assert len(amounts) == len(rows[sid])
            assert amounts == pytest.approx(expected[period])
            seen.add(period)
        assert seen == expected.keys()

    async with boot(ENTRY_MINOR_VERSION) as (hass, recorder, entry):
        for timestamp, load, grid in points:
            now = datetime.fromisoformat(timestamp).replace(tzinfo=tz)
            hass.states.async_set("sensor.load", str(load), {"unit_of_measurement": "W"})
            hass.states.async_set("sensor.grid", str(grid), {"unit_of_measurement": "W"})
            hass.states.async_set("sensor.clock", now.isoformat())
            await entry.runtime_data.async_capture()
        await check_bars(hass, recorder)
        # Remove the derived bars from this temporary DB so a passing restart
        # requires replaying the files. Legacy installations can also have a
        # spurious UTC slot in the previous local day.
        recorder.async_clear_statistics([sid])
        if upgrade:
            async_add_external_statistics(hass, {
                "source": DOMAIN, "statistic_id": sid, "name": "Old gain", "unit_of_measurement": "EUR",
                "unit_class": None, "has_sum": True, "mean_type": StatisticMeanType.NONE,
            }, [{"start": datetime(2025, 12, 30, 18, tzinfo=timezone.utc), "sum": 99., "state": 99.}])
        await recorder.async_block_till_done()

    now += timedelta(minutes=1)
    async with boot(1 if upgrade else ENTRY_MINOR_VERSION) as (hass, recorder, entry):
        await check_bars(hass, recorder)
        assert entry.minor_version == ENTRY_MINOR_VERSION
