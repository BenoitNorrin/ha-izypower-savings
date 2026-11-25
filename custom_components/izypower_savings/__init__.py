"""Config-entry lifecycle for Izypower Savings."""
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady

from .config import normalize
from .coordinator import SavingsCoordinator
from .const import ENTRY_MINOR_VERSION

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass, entry):
    config = normalize({**entry.data, **entry.options})
    coordinator = SavingsCoordinator(hass, config)

    @callback
    def finish_statistics_upgrade():
        if hass.config_entries.async_get_entry(entry.entry_id) is entry:
            hass.config_entries.async_update_entry(entry, minor_version=ENTRY_MINOR_VERSION)

    try:
        await coordinator.async_load(finish_statistics_upgrade if entry.minor_version < ENTRY_MINOR_VERSION else None)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigEntryNotReady("Historique local à vérifier ; ne pas supprimer les fichiers") from exc
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    await coordinator.async_start()
    return True


async def _options_updated(hass, entry):
    if normalize({**entry.data, **entry.options}) != entry.runtime_data.config:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
        return True
    return False
