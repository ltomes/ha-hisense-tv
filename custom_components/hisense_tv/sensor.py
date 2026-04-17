"""Sensor entities for Hisense TV."""

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_PLAYING, STATE_PAUSED
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_JELLYFIN_ENTITY, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    airplay_info = data.get("airplay_info")

    entities = []

    if entry.options.get(CONF_JELLYFIN_ENTITY):
        entities.append(HisenseTVNowPlayingSensor(hass, entry))

    if airplay_info:
        entities.append(HisenseTVDisplayInfoSensor(hass, entry, airplay_info))

    if entities:
        async_add_entities(entities)


class _SensorBase(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass, entry):
        self.hass = hass
        self._entry = entry
        self._unsub: list = []

    @property
    def device_info(self):
        model = "Hisense TV"
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        airplay_info = data.get("airplay_info")
        if airplay_info and airplay_info.model:
            model = airplay_info.model
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Hisense",
            model=model,
        )

    async def async_will_remove_from_hass(self):
        for u in self._unsub:
            u()
        self._unsub.clear()


class HisenseTVNowPlayingSensor(_SensorBase):
    _attr_name = "Now Playing"
    _attr_icon = "mdi:play-circle"

    def __init__(self, hass, entry):
        super().__init__(hass, entry)
        self._jellyfin = entry.options[CONF_JELLYFIN_ENTITY]
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_now_playing"

    async def async_added_to_hass(self):
        self._unsub.append(
            async_track_state_change_event(self.hass, [self._jellyfin], self._on_change)
        )

    @callback
    def _on_change(self, event):
        self.async_write_ha_state()

    @property
    def native_value(self):
        s = self.hass.states.get(self._jellyfin)
        if not s or s.state not in (STATE_PLAYING, STATE_PAUSED):
            return "Idle"
        a = s.attributes
        title = a.get("media_title", "")
        series = a.get("media_series_title")
        if series:
            return f"{series} S{a.get('media_season','')}E{a.get('media_episode','')} - {title}"
        return title or "Playing"


class HisenseTVDisplayInfoSensor(_SensorBase):
    """Static display capabilities from AirPlay -- updates on HA restart."""

    _attr_name = "Display Info"
    _attr_icon = "mdi:projector"

    def __init__(self, hass, entry, airplay_info):
        super().__init__(hass, entry)
        self._info = airplay_info
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_display_info"

    @property
    def native_value(self):
        d = self._info.display
        return f"{d.width_max}x{d.height_max}@{d.max_fps}"

    @property
    def extra_state_attributes(self):
        d = self._info.display
        return {
            "resolution_max": f"{d.width_max}x{d.height_max}",
            "resolution_native": f"{d.width_native}x{d.height_native}",
            "max_fps": d.max_fps,
            "hdr_modes": d.hdr_modes,
            "dolby_vision_codecs": d.dolby_vision_codecs,
            "max_bitrate_peak_mbps": round(d.max_bitrate_peak / 1_000_000, 1) if d.max_bitrate_peak else None,
            "max_bitrate_avg_mbps": round(d.max_bitrate_avg / 1_000_000, 1) if d.max_bitrate_avg else None,
            "display_uuid": d.uuid,
            "firmware": self._info.firmware_version,
            "firmware_build_date": self._info.firmware_build_date,
            "hardware": self._info.hardware_revision,
            "serial_number": self._info.serial_number,
            "device_id": self._info.device_id,
            "model": self._info.model,
        }
