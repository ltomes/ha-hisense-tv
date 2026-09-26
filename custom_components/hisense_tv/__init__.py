"""The Hisense TV integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .adb import ensure_adb_key
from .airplay import fetch_airplay_info
from .const import (
    CONF_ADB_PORT,
    CONF_AIRPLAY_HOST,
    CONF_AIRPLAY_PORT,
    CONF_ENABLE_KEYEVENT_STREAM,
    CONF_ENABLE_SOURCE_STREAM,
    DEFAULT_ADB_PORT,
    DEFAULT_AIRPLAY_PORT,
    DOMAIN,
)
from .keyevent_stream import KeyEventStreamer
from .source_event_stream import SourceEventStreamer

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

    # Optional: persistent ADB getevent stream firing `hisense_tv_key`
    # events. Reuses the same host/port/key as the media_player polling
    # client but runs on its own dedicated TCP connection.
    #
    # Reachability is driven by media_player via the
    # `hisense_tv_adb_state:<entry_id>` dispatcher signal. media_player
    # fires the signal on every adb.connected transition; the streamer
    # gates its connect/stream/teardown on that signal. See
    # keyevent_stream.KeyEventStreamer.set_reachable for rationale.
    #
    # CRITICAL ORDERING: this dispatcher_connect must happen BEFORE
    # async_forward_entry_setups, otherwise the media_player platform's
    # async_added_to_hass / _setup_adb completes inside the
    # forward_entry_setups await and fires the initial reachability
    # signal before the streamer subscriber is registered. The signal
    # is then lost (no listeners) and the streamer never wakes up.
    if entry.options.get(CONF_ENABLE_KEYEVENT_STREAM, False):
        host = entry.data.get(CONF_AIRPLAY_HOST)
        port = entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT)
        if host:
            try:
                from homeassistant.helpers.dispatcher import async_dispatcher_connect

                key_path = ensure_adb_key(hass.config.path())
                streamer = KeyEventStreamer(
                    hass=hass,
                    host=host,
                    port=port,
                    key_path=key_path,
                    entry_id=entry.entry_id,
                )
                signal = f"hisense_tv_adb_state:{entry.entry_id}"
                unsub_dispatcher = async_dispatcher_connect(
                    hass, signal, streamer.set_reachable
                )
                await streamer.start()
                hass.data[DOMAIN][entry.entry_id]["keyevent_streamer"] = streamer
                entry.async_on_unload(unsub_dispatcher)
                entry.async_on_unload(streamer.stop)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Hisense keyevent stream failed to start: %s", err
                )

    # Optional: persistent ADB logcat stream firing
    # `hisense_tv_source_changed` events on projector input-source
    # transitions. Same reachability gating + dispatcher signal as the
    # keyevent stream above. The dispatcher_connect must again happen
    # BEFORE async_forward_entry_setups (same ordering rationale).
    # Source-event stream is bound to the keyevent stream: if you've
    # opted into the keyevent stream you'll also want the
    # source-changed events (same projector, same use case for the
    # Thor jmp-input-bridge HDMI gate). Explicit CONF_ENABLE_SOURCE_STREAM
    # remains supported as an independent override.
    if (
        entry.options.get(CONF_ENABLE_SOURCE_STREAM, False)
        or entry.options.get(CONF_ENABLE_KEYEVENT_STREAM, False)
    ):
        host = entry.data.get(CONF_AIRPLAY_HOST)
        port = entry.data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT)
        if host:
            try:
                from homeassistant.helpers.dispatcher import async_dispatcher_connect

                key_path = ensure_adb_key(hass.config.path())
                src_streamer = SourceEventStreamer(
                    hass=hass,
                    host=host,
                    port=port,
                    key_path=key_path,
                    entry_id=entry.entry_id,
                )
                signal = f"hisense_tv_adb_state:{entry.entry_id}"
                unsub_src = async_dispatcher_connect(
                    hass, signal, src_streamer.set_reachable
                )
                await src_streamer.start()
                hass.data[DOMAIN][entry.entry_id]["source_event_streamer"] = src_streamer
                entry.async_on_unload(unsub_src)
                entry.async_on_unload(src_streamer.stop)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Hisense source-event stream failed to start: %s", err
                )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    streamer = entry_data.get("keyevent_streamer")
    if streamer:
        await streamer.stop()
    src_streamer = entry_data.get("source_event_streamer")
    if src_streamer:
        await src_streamer.stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
