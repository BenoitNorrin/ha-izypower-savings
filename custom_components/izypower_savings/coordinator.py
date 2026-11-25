"""Read existing HA entities and keep an independent, local, replayable history."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval

from .config import source_entities, source_key
from .const import DOMAIN, SIGNAL
from .model import Journal, stamp, watts

STATISTICS = (
    ("benefit", "benefit", "Gain total estimé"),
    ("consumption_savings", "savings", "Économies sur la consommation"),
    ("export_revenue", "revenue", "Revenus d’injection estimés"),
    ("net_cost", "with_installation", "Coût net estimé avec installation"),
    ("without_cost", "without_installation", "Coût estimé sans installation"),
)


class SavingsCoordinator:
    def __init__(self, hass, config):
        self.hass = hass
        self.config = config
        self.journal = Journal(hass.config.path(DOMAIN))
        self.lock = asyncio.Lock()
        self.error = None
        self.ready = False
        self._stopping = False
        self._unsubs = []
        self._delayed = None

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    async def async_load(self, on_legacy_statistics_cleared=None):
        now = self.now()
        await self.hass.async_add_executor_job(self.journal.load, now, self.config)
        key = source_key(self.config)
        if self.journal.engine.source_key != key:
            await self.hass.async_add_executor_job(self.journal.append, {
                "type": "sources", "at": now.isoformat(), "key": key,
                "max_gap_seconds": self.config["max_gap_seconds"],
            })
        self.ready = True
        self.error = self.journal.engine.last_error
        if on_legacy_statistics_cleared is not None:
            engine = self.journal.engine
            if engine.last or engine.rows:
                # Old hourly snapshots can survive an upsert under another
                # hour. Rebuild only our derived series inside the journal span.
                begin = datetime.fromtimestamp(engine.start, timezone.utc).replace(minute=0, second=0, microsecond=0)
                get_instance(self.hass).async_clear_statistics(
                    [f"{DOMAIN}:{suffix}" for _, suffix, _ in STATISTICS],
                    start_time=begin, end_time=max(begin, now) + timedelta(hours=1),
                    on_done=lambda: self.hass.loop.call_soon_threadsafe(on_legacy_statistics_cleared),
                )
            else:
                on_legacy_statistics_cleared()
        self.publish()

    def reading(self, now):
        event = {"type": "sample", "received_at": now.isoformat(), "measured_at": now.isoformat()}
        try:
            def state_for(entity):
                state = self.hass.states.get(entity)
                if state is None or state.state in ("unknown", "unavailable"):
                    raise ValueError("source_unavailable")
                return state

            registry = er.async_get(self.hass)
            clock_entry = None
            if self.config["source_timestamp"]:
                clock_entry = registry.async_get(self.config["source_timestamp"])
                source = state_for(self.config["source_timestamp"])
                at = stamp(source.state)
                age = now.timestamp() - at
                if age < -60 or (self.config["max_age_seconds"] and age > self.config["max_age_seconds"]):
                    raise ValueError("source_stale")
                event["measured_at"] = datetime.fromtimestamp(min(at, now.timestamp()), timezone.utc).isoformat()

            def power(entity):
                state = state_for(entity)
                entry = registry.async_get(entity)
                shared_report = (
                    clock_entry and clock_entry.device_id and clock_entry.config_entry_id
                    and entry and entry.device_id == clock_entry.device_id
                    and entry.config_entry_id == clock_entry.config_entry_id
                )
                if self.config["max_age_seconds"] and not shared_report:
                    age = (now - state.last_reported).total_seconds()
                    if not -60 <= age <= self.config["max_age_seconds"]:
                        raise ValueError("source_stale")
                return watts(state.state, state.attributes.get("unit_of_measurement"))

            consumption = power(self.config["consumption_sensor"])
            extra = [power(entity) for entity in self.config["additional_consumption"]]
            if min([consumption, *extra]) < 0:
                raise ValueError("negative_consumption")
            grid = power(self.config["grid_sensor"]) * (-1 if self.config["invert_grid"] else 1)
            exported = power(self.config["export_sensor"]) if self.config["export_sensor"] else None
            if exported is not None and min(exported, grid) < 0:
                raise ValueError("negative_separate_meter")
            event.update(consumption_w=consumption + sum(extra), main_consumption_w=consumption,
                         additional_consumption_w=extra, grid_w=grid, export_w=exported)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            event["measured_at"] = now.isoformat()
            event["error"] = str(exc) if str(exc) in (
                "source_unavailable", "source_stale", "negative_consumption", "negative_separate_meter",
                "invalid_power_unit", "invalid_number", "missing_timezone") else "invalid_source"
        return event

    def publish(self, dirty=None):
        for key, suffix, label in STATISTICS:
            data = self.journal.engine.statistics(key, dirty)
            if data:
                async_add_external_statistics(self.hass, {
                    "source": DOMAIN, "statistic_id": f"{DOMAIN}:{suffix}", "name": label,
                    "unit_of_measurement": self.config["currency"], "unit_class": None,
                    "has_sum": True, "mean_type": StatisticMeanType.NONE,
                }, data)
        async_dispatcher_send(self.hass, SIGNAL)

    async def async_capture(self, _now=None):
        async with self.lock:
            if not self.ready or self._stopping:
                return
            try:
                event = self.reading(self.now())
                engine = self.journal.engine
                if event.get("error"):
                    if event["error"] == engine.last_error and self.error == engine.last_error:
                        return
                elif engine.last and stamp(event["measured_at"]) <= engine.last["t"]:
                    async_dispatcher_send(self.hass, SIGNAL)
                    return
                dirty = await self.hass.async_add_executor_job(self.journal.append, event)
                self.error = event.get("error")
                self.publish(dirty)
            except (OSError, ValueError, KeyError, TypeError):
                self.error = "history_write_failed"
                async_dispatcher_send(self.hass, SIGNAL)

    @callback
    def _changed(self, _event):
        if self._delayed:
            self._delayed()
        self._delayed = async_call_later(self.hass, 1, self.async_capture)

    async def _started(self, _event=None):
        self._unsubs.append(async_track_state_change_event(self.hass, source_entities(self.config), self._changed))
        self._unsubs.append(async_track_time_interval(self.hass, self.async_capture, timedelta(minutes=1)))
        await self.async_capture()

    async def async_start(self):
        if self.hass.is_running:
            await self._started()
        else:
            self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._started))

    async def async_stop(self):
        self._stopping = True
        if self._delayed:
            self._delayed()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        async with self.lock:
            self.ready = False
