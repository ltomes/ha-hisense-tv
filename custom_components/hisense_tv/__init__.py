"""The Hisense TV integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_AIRPLAY_HOST, CONF_AIRPLAY_PORT, DEFAULT_AIRPLAY_PORT, DOMAIN
from .airplay import fetch_airplay_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hisense TV from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Fetch AirPlay info on setup (cached in hass.data)
    airplay_host = entry.data.get(CONF_AIRPLAY_HOST)
    airplay_port = entry.data.get(CONF_AIRPLAY_PORT, DEFAULT_AIRPLAY_PORT)
    airplay_info = None
    if airplay_host:
        airplay_info = await fetch_airplay_info(airplay_host, airplay_port)
        if airplay_info:
            _LOGGER.info(
                "Hisense AirPlay: %s (%s), FW %s, display %dx%d, HDR: %s",
                airplay_info.name, airplay_info.hardware_revision,
                airplay_info.firmware_version,
                airplay_info.display.width_max, airplay_info.display.height_max,
                ", ".join(airplay_info.display.hdr_modes),
            )

    hass.data[DOMAIN][entry.entry_id] = {
        "entry": entry,
        "airplay_info": airplay_info,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
