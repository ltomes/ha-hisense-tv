"""Media player entity for Hisense TV devices.

Architecture -- ADB is the backbone for everything:
  - Power/wake: ADB (dumpsys power)
  - Input switching: ADB (settings put global current_source)
  - Media session: ADB (dumpsys media_session)
  - App detection: ADB (dumpsys activity)
  - HDR/eARC/display: ADB (dumpsys display, settings)
  - All commands: ADB key events
  - Playback metadata: Jellyfin entity (optional rich overlay)
  - Display info: AirPlay (static, fetched once)
  - DAG role: display, source, or both (depending on device mode)

Intent tracking bridges the gap between instant commands and slow state updates:
  1. User presses pause -> intent recorded -> UI shows paused immediately
  2. ADB keyevent sent -> device pauses
  3. Next ADB poll (10s) confirms paused -> intent cleared
  4. If not confirmed in 10s -> retry once -> if still no -> surrender to truth
"""

import logging
import time

from .ha_adb_common import PlaybackIntent, running_apps_from_tasks

from .adb import AdbClient, HisenseTVState, SOURCE_ID_TO_NAME, SOURCE_NAME_TO_ID, ensure_adb_key

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_PLAYING, STATE_PAUSED
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback, Event
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    ATTR_MEDIA_DAG_INPUT_MAP,
    ATTR_MEDIA_DAG_JELLYFIN_ENTITY,
    ATTR_MEDIA_DAG_NOW_PLAYING,
    ATTR_MEDIA_DAG_RECEIVER_ENTITY,
    ATTR_MEDIA_DAG_RECEIVER_INPUT,
    ATTR_MEDIA_DAG_ROLE,
    ATTR_MEDIA_DAG_RUNNING_APPS,
    CONF_ADB_PORT,
    CONF_AIRPLAY_HOST,
    CONF_DEVICE_MODE,
    CONF_INPUT_ENTITY_MAP,
    CONF_JELLYFIN_ENTITY,
    CONF_POWER_ENTITY,
    CONF_RECEIVER_ENTITY,
    CONF_RECEIVER_INPUT,
    DEFAULT_ADB_PORT,
    DOMAIN,
    MODE_BOTH,
    MODE_DISPLAY_ONLY,
    MODE_SOURCE_EARC,
)

ATTR_MEDIA_DAG_ACTIVE_SOURCE_ENTITY = "media_dag_active_source_entity"
ATTR_MEDIA_DAG_SOURCE_ENTITIES = "media_dag_source_entities"

_LOGGER = logging.getLogger(__name__)

ADB_POLL_INTERVAL = 10  # seconds
ADB_RETRY_INTERVAL = 300  # seconds before retrying a failed ADB connection
INTENT_TIMEOUT = 10  # seconds before self-heal fires
MAX_RETRIES = 1

_BASE_FEATURES = (
    MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.SELECT_SOURCE
)

_PLAYBACK_FEATURES = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HisenseTVMediaPlayer(hass, entry, data.get("airplay_info"))])


class HisenseTVMediaPlayer(MediaPlayerEntity):

    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, hass, entry, airplay_info):
        self.hass = hass
        self._entry = entry
        self._airplay_info = airplay_info
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_media_player"
        self._unsub_listeners: list = []
        # ADB
        self._adb: AdbClient | None = None
        self._adb_poll_unsub: CALLBACK_TYPE | None = None
        self._adb_state: HisenseTVState = HisenseTVState()
        self._running_apps: list[dict] = []
        # Intent tracking
        self._intent: PlaybackIntent | None = None
        self._intent_timer_unsub: CALLBACK_TYPE | None = None

    @property
    def _jellyfin_entity(self):
        return self._entry.options.get(CONF_JELLYFIN_ENTITY) or None

    @property
    def _receiver_entity(self):
        return self._entry.options.get(CONF_RECEIVER_ENTITY) or None

    @property
    def _receiver_input(self):
        return self._entry.options.get(CONF_RECEIVER_INPUT) or None

    @property
    def _power_entity(self):
        return self._entry.options.get(CONF_POWER_ENTITY) or None

    @property
    def _device_mode(self):
        return self._entry.options.get(CONF_DEVICE_MODE, MODE_BOTH)

    @property
    def _dag_role(self) -> str:
        mode = self._device_mode
        if mode == MODE_DISPLAY_ONLY:
            return "display"
        if mode == MODE_SOURCE_EARC:
            return "source"
        return "display_source"

    @property
    def device_info(self) -> DeviceInfo:
        # Auto-detect model from AirPlay, fall back to generic
        model = "Hisense TV"
        if self._airplay_info and self._airplay_info.model:
            model = self._airplay_info.model
        elif self._airplay_info and self._airplay_info.hardware_revision:
            model = self._airplay_info.hardware_revision

        info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="Hisense",
            model=model,
        )
        if self._airplay_info:
            info["sw_version"] = self._airplay_info.firmware_version
            info["hw_version"] = self._airplay_info.hardware_revision
            if self._airplay_info.serial_number:
                info["serial_number"] = self._airplay_info.serial_number
        return info

    async def async_added_to_hass(self):
        entities = []
        if self._jellyfin_entity:
            entities.append(self._jellyfin_entity)
        if self._power_entity:
            entities.append(self._power_entity)
        if entities:
            self._unsub_listeners.append(
                async_track_state_change_event(
                    self.hass, entities, self._on_entity_change
                )
            )
        await self._setup_adb()

    async def _setup_adb(self) -> None:
        try:
            host = self._entry.data.get(CONF_AIRPLAY_HOST)
            if not host:
                return

            # Only auto-connect if ADB was previously paired (key exists)
            import os
            key_dir = os.path.join(self.hass.config.config_dir, ".storage", DOMAIN)
            key_path = os.path.join(key_dir, "adbkey")
            if not await self.hass.async_add_executor_job(os.path.exists, key_path):
                _LOGGER.info(
                    "ADB key not found — skipping auto-connect. "
                    "Reconfigure the integration to pair ADB."
                )
                return

            port = self._entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT)
            self._adb = AdbClient(host, port, key_path)
            if await self._adb.connect():
                self._adb_poll_unsub = async_call_later(
                    self.hass, ADB_POLL_INTERVAL, self._adb_poll_callback
                )
            else:
                _LOGGER.warning(
                    "ADB connection to %s failed. Retrying in %d seconds.",
                    host, ADB_RETRY_INTERVAL,
                )
                self._adb_poll_unsub = async_call_later(
                    self.hass, ADB_RETRY_INTERVAL, self._adb_poll_callback
                )
        except Exception:
            _LOGGER.exception("Failed to set up ADB")

    @callback
    def _adb_poll_callback(self, _now) -> None:
        self.hass.async_create_task(self._adb_poll())

    async def _adb_poll(self) -> None:
        try:
            if not self._adb:
                return
            self._adb_state = await self._adb.poll()
            if self._adb_state.awake:
                self._running_apps = await self._adb.poll_running_apps()
            else:
                self._running_apps = []

            # Check if intent was confirmed by ADB media session
            if self._intent and self._adb_state.media_session:
                ms = self._adb_state.media_session
                target = self._intent.target_state
                if target == STATE_PLAYING and ms.playing:
                    self._clear_intent()
                elif target == STATE_PAUSED and ms.paused:
                    self._clear_intent()
                elif target not in (STATE_PLAYING, STATE_PAUSED) and not ms.playing and not ms.paused:
                    self._clear_intent()

            self.async_write_ha_state()
        except Exception:
            _LOGGER.debug("ADB poll error", exc_info=True)
        finally:
            # Use slow retry if ADB is disconnected, normal interval if connected
            interval = ADB_POLL_INTERVAL if self._adb.connected else ADB_RETRY_INTERVAL
            self._adb_poll_unsub = async_call_later(
                self.hass, interval, self._adb_poll_callback
            )

    async def async_will_remove_from_hass(self):
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        self._clear_intent()
        if self._adb_poll_unsub:
            self._adb_poll_unsub()
            self._adb_poll_unsub = None
        if self._adb:
            await self._adb.disconnect()

    # -- Intent tracking --

    def _set_intent(self, adb_command: str, target: MediaPlayerState) -> None:
        if self._intent_timer_unsub:
            self._intent_timer_unsub()
        self._intent = PlaybackIntent.create(
            target_state=target.value,
            command=adb_command,
        )
        self._intent_timer_unsub = async_call_later(
            self.hass, INTENT_TIMEOUT, self._on_intent_timeout
        )

    def _clear_intent(self) -> None:
        if self._intent_timer_unsub:
            self._intent_timer_unsub()
            self._intent_timer_unsub = None
        self._intent = None

    @callback
    def _on_intent_timeout(self, _now) -> None:
        intent = self._intent
        if not intent:
            return

        ms = self._adb_state.media_session
        if ms:
            if intent.target_state == STATE_PLAYING and ms.playing:
                self._clear_intent()
                self.async_write_ha_state()
                return
            if intent.target_state == STATE_PAUSED and ms.paused:
                self._clear_intent()
                self.async_write_ha_state()
                return

        if intent.retry_count < MAX_RETRIES:
            intent.retry_count += 1
            intent.created_at = time.monotonic()
            if self._adb:
                self.hass.async_create_task(
                    self._adb.send_keyevent(intent.command)
                )
            self._intent_timer_unsub = async_call_later(
                self.hass, INTENT_TIMEOUT, self._on_intent_timeout
            )
            return

        self._clear_intent()
        self.async_write_ha_state()

    @callback
    def _on_entity_change(self, event: Event):
        entity_id = event.data.get("entity_id")
        new_state_obj = event.data.get("new_state")

        if entity_id == self._jellyfin_entity and self._intent and new_state_obj:
            new_jf = new_state_obj.state
            target = self._intent.target_state
            if new_jf == target:
                self._clear_intent()

        self.async_write_ha_state()

    # -- Helpers --

    def _read(self, entity_id):
        if not entity_id:
            return None
        return self.hass.states.get(entity_id)

    def _is_source_mode(self) -> bool:
        """Is the TV acting as a content source (built-in apps)?

        Google TV (source -1) = source mode. HDMI input = display mode.
        """
        return self._adb_state.current_source == -1

    def _is_power_on(self):
        if self._adb_state.awake is not None:
            return self._adb_state.awake
        if self._power_entity:
            ps = self._read(self._power_entity)
            if ps and ps.state == "off":
                return False
        return None

    def _jf_active(self):
        if not self._is_source_mode():
            return False
        jf = self._read(self._jellyfin_entity)
        if jf and jf.state in (STATE_PLAYING, STATE_PAUSED):
            return True
        if self._intent and self._intent.target_state in (
            STATE_PLAYING, STATE_PAUSED
        ):
            return True
        return False

    def _get_active_source_entity_id(self) -> str | None:
        input_map = self._entry.options.get(CONF_INPUT_ENTITY_MAP, {})
        current_input = self._adb_state.current_source_name
        if current_input and current_input in input_map:
            return input_map[current_input]
        return None

    # -- State --

    @property
    def state(self):
        if self._power_entity:
            ps = self._read(self._power_entity)
            if ps and ps.state == "off":
                return MediaPlayerState.OFF

        power = self._is_power_on()
        if power is False:
            return MediaPlayerState.IDLE
        if power is None:
            return MediaPlayerState.IDLE

        if self._intent:
            return MediaPlayerState(self._intent.target_state)

        if self._is_source_mode():
            jf = self._read(self._jellyfin_entity)
            if jf:
                if jf.state == STATE_PLAYING:
                    return MediaPlayerState.PLAYING
                if jf.state == STATE_PAUSED:
                    return MediaPlayerState.PAUSED

            ms = self._adb_state.media_session
            if ms:
                if ms.playing:
                    return MediaPlayerState.PLAYING
                if ms.paused:
                    return MediaPlayerState.PAUSED

        return MediaPlayerState.ON

    @property
    def supported_features(self):
        features = _BASE_FEATURES
        if self._is_source_mode():
            if self._jf_active():
                features |= _PLAYBACK_FEATURES
            elif self._adb_state.media_session and (
                self._adb_state.media_session.playing or self._adb_state.media_session.paused
            ):
                features |= _PLAYBACK_FEATURES
        return features

    # -- Source selection --

    @property
    def source(self) -> str | None:
        return self._adb_state.current_source_name

    @property
    def source_list(self) -> list[str]:
        return list(SOURCE_NAME_TO_ID.keys())

    async def async_select_source(self, source: str) -> None:
        if self._adb:
            await self._adb.switch_input(source)
            src_id = SOURCE_NAME_TO_ID.get(source)
            if src_id is not None:
                self._adb_state.current_source = src_id
                self._adb_state.current_source_name = source
            self.async_write_ha_state()

    # -- Metadata --

    @property
    def app_id(self):
        return self._adb_state.active_app

    @property
    def app_name(self):
        return self._adb_state.active_app

    @property
    def media_content_type(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_content_type") if jf else None
        return None

    @property
    def media_title(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_title") if jf else None
        if self._is_source_mode() and self._adb_state.media_session:
            return self._adb_state.media_session.title
        return None

    @property
    def media_series_title(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_series_title") if jf else None
        return None

    @property
    def media_season(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_season") if jf else None
        return None

    @property
    def media_episode(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_episode") if jf else None
        return None

    @property
    def media_duration(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_duration") if jf else None
        return None

    @property
    def media_position(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_position") if jf else None
        return None

    @property
    def media_position_updated_at(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("media_position_updated_at") if jf else None
        return None

    @property
    def media_image_url(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            return jf.attributes.get("entity_picture") if jf else None
        return None

    def _get_now_playing(self):
        if self._jf_active():
            jf = self._read(self._jellyfin_entity)
            if jf:
                attrs = jf.attributes
                np = {"state": self.state.value, "title": attrs.get("media_title"),
                      "media_type": attrs.get("media_content_type")}
                for src, dst in [("media_series_title", "series"), ("media_season", "season"),
                                 ("media_episode", "episode"), ("media_duration", "duration"),
                                 ("media_position", "position"),
                                 ("media_position_updated_at", "position_updated_at")]:
                    v = attrs.get(src)
                    if v is not None:
                        np[dst] = v
                if attrs.get("entity_picture"):
                    np["thumbnail"] = attrs["entity_picture"]
                return np

        if self._is_source_mode():
            ms = self._adb_state.media_session
            if ms and (ms.playing or ms.paused):
                np = {
                    "state": "playing" if ms.playing else "paused",
                    "title": ms.title or ms.package or "Playing",
                    "source": "media_session",
                }
                if ms.artist:
                    np["artist"] = ms.artist
                if ms.album:
                    np["album"] = ms.album
                return np

        return None

    def _get_running_apps(self) -> list[dict] | None:
        """Build a list of running apps from ADB recent tasks + media sessions."""
        if not self._running_apps:
            return None
        return running_apps_from_tasks(
            self._running_apps,
            self._adb_state.all_media_sessions,
            foreground_package=self._adb_state.active_app,
        )

    @property
    def extra_state_attributes(self):
        attrs = {
            "app_id": self.app_id,
            "app_name": self.app_name,
            ATTR_MEDIA_DAG_ROLE: self._dag_role,
            ATTR_MEDIA_DAG_NOW_PLAYING: self._get_now_playing(),
            "device_mode": self._device_mode,
        }
        running_apps = self._get_running_apps()
        if running_apps:
            attrs[ATTR_MEDIA_DAG_RUNNING_APPS] = running_apps
        if self._power_entity:
            attrs["power_switch"] = self._power_entity
        if self._receiver_entity:
            attrs[ATTR_MEDIA_DAG_RECEIVER_ENTITY] = self._receiver_entity
        if self._receiver_input:
            attrs[ATTR_MEDIA_DAG_RECEIVER_INPUT] = self._receiver_input
        if self._jellyfin_entity:
            attrs[ATTR_MEDIA_DAG_JELLYFIN_ENTITY] = self._jellyfin_entity

        input_map = self._entry.options.get(CONF_INPUT_ENTITY_MAP, {})
        if input_map:
            attrs[ATTR_MEDIA_DAG_INPUT_MAP] = input_map
            attrs[ATTR_MEDIA_DAG_SOURCE_ENTITIES] = list(
                {eid for eid in input_map.values() if eid}
            )
        attrs[ATTR_MEDIA_DAG_ACTIVE_SOURCE_ENTITY] = self._get_active_source_entity_id()

        if self._airplay_info:
            d = self._airplay_info.display
            attrs["display"] = {
                "resolution_max": f"{d.width_max}x{d.height_max}",
                "resolution_native": f"{d.width_native}x{d.height_native}",
                "max_fps": d.max_fps,
                "hdr_modes": d.hdr_modes,
                "dolby_vision_codecs": d.dolby_vision_codecs,
            }
            attrs["firmware"] = self._airplay_info.firmware_version
            attrs["hardware"] = self._airplay_info.hardware_revision

        adb = self._adb_state
        attrs["hdmi_input"] = adb.current_source_name
        attrs["hdr_active"] = adb.hdr_active
        attrs["earc_connected"] = adb.earc_connected
        attrs["audio_device"] = adb.audio_device
        attrs["brightness"] = adb.brightness
        if adb.hdmi_connected:
            attrs["hdmi_connected"] = adb.hdmi_connected

        if self._intent:
            attrs["_intent"] = self._intent.to_debug_dict()
        return attrs

    # -- Commands -- ALL via ADB --

    async def async_turn_on(self):
        if self._adb:
            await self._adb.turn_on()

    async def async_turn_off(self):
        if self._adb:
            await self._adb.turn_off()

    async def async_volume_up(self):
        if self._adb:
            await self._adb.send_keyevent("volume_up")

    async def async_volume_down(self):
        if self._adb:
            await self._adb.send_keyevent("volume_down")

    async def async_mute_volume(self, mute):
        if self._adb:
            await self._adb.send_keyevent("mute")

    async def async_media_play(self):
        self._set_intent("play", MediaPlayerState.PLAYING)
        self.async_write_ha_state()
        if self._adb:
            await self._adb.send_keyevent("play")

    async def async_media_pause(self):
        self._set_intent("pause", MediaPlayerState.PAUSED)
        self.async_write_ha_state()
        if self._adb:
            await self._adb.send_keyevent("pause")

    async def async_media_stop(self):
        self._set_intent("stop", MediaPlayerState.IDLE)
        self.async_write_ha_state()
        if self._adb:
            await self._adb.send_keyevent("stop")

    async def async_media_next_track(self):
        if self._adb:
            await self._adb.send_keyevent("next")

    async def async_media_previous_track(self):
        if self._adb:
            await self._adb.send_keyevent("previous")

    async def async_bring_to_foreground(self, package: str) -> bool:
        """Bring a specific app to the foreground via ADB."""
        if not self._adb:
            _LOGGER.warning("ADB not available for bring_to_foreground")
            return False
        return await self._adb.bring_to_foreground(package)
