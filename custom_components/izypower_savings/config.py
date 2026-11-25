"""Configuration normalization shared by the UI and runtime."""
from __future__ import annotations

import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .const import DEFAULT_MAX_AGE, DEFAULT_MAX_GAP
from .model import build_plan

DEFAULTS = {
    "additional_consumption": [], "export_sensor": "", "source_timestamp": "",
    "invert_grid": False, "off_peak_enabled": False, "off_peak_rate": 0., "off_peak_windows": "",
    "super_off_peak_enabled": False, "super_off_peak_rate": 0., "super_off_peak_windows": "",
    "export_mode": "none", "export_rate": 0., "currency": "EUR", "timezone": "Europe/Paris",
    "max_age_seconds": DEFAULT_MAX_AGE, "max_gap_seconds": DEFAULT_MAX_GAP,
}
SOURCE_FIELDS = ("consumption_sensor", "grid_sensor", "additional_consumption", "export_sensor",
                 "source_timestamp", "invert_grid", "max_age_seconds", "max_gap_seconds")


def normalize(config: dict) -> dict:
    result = {**DEFAULTS, **config}
    extra = result["additional_consumption"] or []
    if not isinstance(extra, (list, tuple)) or any(not isinstance(e, str) or "." not in e for e in extra):
        raise ValueError("missing_source")
    result["additional_consumption"] = list(extra)
    for key in ("export_sensor", "source_timestamp"):
        result[key] = result[key] or ""
        if not isinstance(result[key], str) or (result[key] and "." not in result[key]):
            raise ValueError("missing_source")
    for key in ("consumption_sensor", "grid_sensor"):
        if not isinstance(result.get(key), str) or "." not in result[key]:
            raise ValueError("missing_source")
    power_sources = [result["consumption_sensor"], result["grid_sensor"], *result["additional_consumption"]]
    if result["export_sensor"]:
        power_sources.append(result["export_sensor"])
    if len(set(power_sources)) != len(power_sources):
        raise ValueError("duplicate_source")
    for key in ("max_age_seconds", "max_gap_seconds"):
        value = result[key]
        if isinstance(value, bool) or int(value) != value:
            raise ValueError("invalid_interval")
        result[key] = int(value)
    if not 0 <= result["max_age_seconds"] <= 86400 or not 60 <= result["max_gap_seconds"] <= 3600:
        raise ValueError("invalid_interval")
    result["currency"] = str(result["currency"]).upper()
    if not re.fullmatch(r"[A-Z]{3}", result["currency"]):
        raise ValueError("invalid_currency")
    ZoneInfo(result["timezone"])
    build_plan(result)
    return result


def source_key(config: dict) -> str:
    data = {key: config[key] for key in SOURCE_FIELDS}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def source_entities(config: dict) -> list[str]:
    return [config["consumption_sensor"], config["grid_sensor"], *config["additional_consumption"],
            *[config[key] for key in ("export_sensor", "source_timestamp") if config[key]]]
