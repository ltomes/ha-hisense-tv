"""Button entities for Hisense TV."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .adb import AdbClient, ensure_adb_key
from .const import CONF_ADB_PORT, CONF_AIRPLAY_HOST, DEFAULT_ADB_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    host = entry.data.get(CONF_AIRPLAY_HOST)
    if host:
        async_add_entities([HisenseTVPairAdbButton(hass, entry)])


class HisenseTVPairAdbButton(ButtonEntity):
    """Button to initiate or re-pair the ADB connection."""

    _attr_has_entity_name = True
    _attr_name = "Pair ADB"
    _attr_icon = "mdi:link-variant"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_pair_adb"

    @property
    def device_info(self) -> DeviceInfo:
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        airplay_info = data.get("airplay_info")
        model = "Hisense TV"
        if airplay_info and airplay_info.model:
            model = airplay_info.model
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Hisense",
            model=model,
        )

    async def async_press(self) -> None:
        """Attempt ADB connection. The TV will show a pairing prompt."""
        host = self._entry.data.get(CONF_AIRPLAY_HOST)
        port = self._entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT)

        if not host:
            _LOGGER.error("No host configured for ADB pairing")
            return

        key_path = await self.hass.async_add_executor_job(
            ensure_adb_key, self.hass.config.config_dir
        )

        _LOGGER.info("Attempting ADB pairing with %s:%d — approve the prompt on the TV", host, port)

        client = AdbClient(host, port, key_path)
        if await client.connect():
            _LOGGER.info("ADB pairing successful with %s", host)
            await client.disconnect()
            # Reload the integration to start ADB polling
            await self.hass.config_entries.async_reload(self._entry.entry_id)
        else:
            _LOGGER.error(
                "ADB pairing failed with %s. Make sure ADB debugging is enabled "
                "and you approved the prompt on the TV.",
                host,
            )
            await client.disconnect()
