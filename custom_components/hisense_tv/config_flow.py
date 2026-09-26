"""Config flow for Hisense TV integration."""

import logging
import os

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .adb import AdbClient, ensure_adb_key
from .airplay import fetch_airplay_info
from .const import (
    ATTR_MEDIA_DAG_INPUT_MAP,
    CONF_ADB_PAIRED,
    CONF_ADB_PORT,
    CONF_AIRPLAY_HOST,
    CONF_AIRPLAY_PORT,
    CONF_DEVICE_MODE,
    CONF_ENABLE_KEYEVENT_STREAM,
    CONF_ENABLE_SOURCE_STREAM,
    CONF_INPUT_ENTITY_MAP,
    CONF_JELLYFIN_ENTITY,
    CONF_POWER_ENTITY,
    CONF_RECEIVER_ENTITY,
    CONF_RECEIVER_INPUT,
    DEFAULT_ADB_PORT,
    DEFAULT_AIRPLAY_PORT,
    DEFAULT_NAME,
    DEVICE_MODES,
    DOMAIN,
    MODE_BOTH,
)

CONF_HDMI_INPUTS = "hdmi_inputs"

_LOGGER = logging.getLogger(__name__)


def _get_receiver_inputs(hass, receiver_entity_id: str | None) -> list[str]:
    if not receiver_entity_id:
        return []
    state = hass.states.get(receiver_entity_id)
    if not state:
        return []
    input_map = state.attributes.get(ATTR_MEDIA_DAG_INPUT_MAP, {})
    return list(input_map.keys()) if isinstance(input_map, dict) else []


class HisenseTVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hisense TV."""

    VERSION = 1

    def __init__(self):
        self._user_data = {}

    async def async_step_user(self, user_input=None):
        """Step 1: Basic device info."""
        errors = {}

        if user_input is not None:
            name = user_input.get("name", DEFAULT_NAME)
            airplay_host = user_input.get(CONF_AIRPLAY_HOST, "").strip()
            airplay_port = user_input.get(CONF_AIRPLAY_PORT, DEFAULT_AIRPLAY_PORT)

            if not airplay_host:
                errors["base"] = "no_host"
            else:
                # Validate AirPlay connection
                info = await fetch_airplay_info(airplay_host, airplay_port)
                if info:
                    name = info.name or name
                    _LOGGER.info("Discovered Hisense TV: %s (%s)", info.name, info.serial_number)
                    await self.async_set_unique_id(info.serial_number or airplay_host)
                else:
                    _LOGGER.warning("Could not reach AirPlay at %s, continuing anyway", airplay_host)
                    await self.async_set_unique_id(airplay_host)

                self._abort_if_unique_id_configured()

                # Store for the next step
                self._user_data = {
                    "name": name,
                    CONF_AIRPLAY_HOST: airplay_host,
                    CONF_AIRPLAY_PORT: airplay_port,
                    CONF_ADB_PORT: user_input.get(CONF_ADB_PORT, DEFAULT_ADB_PORT),
                    CONF_DEVICE_MODE: user_input.get(CONF_DEVICE_MODE, MODE_BOTH),
                    CONF_JELLYFIN_ENTITY: user_input.get(CONF_JELLYFIN_ENTITY, ""),
                    CONF_RECEIVER_ENTITY: user_input.get(CONF_RECEIVER_ENTITY, ""),
                    CONF_RECEIVER_INPUT: user_input.get(CONF_RECEIVER_INPUT, ""),
                    CONF_POWER_ENTITY: user_input.get(CONF_POWER_ENTITY, ""),
                }
                return await self.async_step_adb_pair()

        media_player_sel = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player")
        )
        receiver_sel = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player", device_class="receiver")
        )
        power_sel = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "binary_sensor"])
        )
        mode_sel = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value="display_only", label="Display Only (receives from receiver)"),
                    selector.SelectOptionDict(value="source_via_earc", label="Source via eARC (sends audio to receiver)"),
                    selector.SelectOptionDict(value="both", label="Both (display + source)"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=DEFAULT_NAME): str,
                    vol.Required(CONF_AIRPLAY_HOST): str,
                    vol.Optional(CONF_AIRPLAY_PORT, default=DEFAULT_AIRPLAY_PORT): int,
                    vol.Optional(CONF_ADB_PORT, default=DEFAULT_ADB_PORT): int,
                    vol.Optional(CONF_DEVICE_MODE, default=MODE_BOTH): mode_sel,
                    vol.Optional(CONF_JELLYFIN_ENTITY): media_player_sel,
                    vol.Optional(CONF_RECEIVER_ENTITY): receiver_sel,
                    vol.Optional(CONF_RECEIVER_INPUT): str,
                    vol.Optional(CONF_POWER_ENTITY): power_sel,
                }
            ),
            errors=errors,
        )

    async def async_step_adb_pair(self, user_input=None):
        """Step 2: ADB pairing. User clicks Submit after approving on the TV."""
        errors = {}

        if user_input is not None:
            # User clicked Submit — attempt the ADB connection
            host = self._user_data[CONF_AIRPLAY_HOST]
            port = self._user_data.get(CONF_ADB_PORT, DEFAULT_ADB_PORT)

            key_path = await self.hass.async_add_executor_job(
                ensure_adb_key, self.hass.config.config_dir
            )

            client = AdbClient(host, port, key_path)
            connected = await client.connect()
            await client.disconnect()

            if connected:
                # Pairing successful — create the entry
                return self.async_create_entry(
                    title=self._user_data["name"],
                    data={
                        CONF_AIRPLAY_HOST: host,
                        CONF_AIRPLAY_PORT: self._user_data.get(CONF_AIRPLAY_PORT, DEFAULT_AIRPLAY_PORT),
                        CONF_ADB_PORT: port,
                        CONF_ADB_PAIRED: True,
                    },
                    options={
                        CONF_DEVICE_MODE: self._user_data.get(CONF_DEVICE_MODE, MODE_BOTH),
                        CONF_JELLYFIN_ENTITY: self._user_data.get(CONF_JELLYFIN_ENTITY, ""),
                        CONF_RECEIVER_ENTITY: self._user_data.get(CONF_RECEIVER_ENTITY, ""),
                        CONF_RECEIVER_INPUT: self._user_data.get(CONF_RECEIVER_INPUT, ""),
                        CONF_POWER_ENTITY: self._user_data.get(CONF_POWER_ENTITY, ""),
                    },
                )
            else:
                errors["base"] = "adb_failed"

        return self.async_show_form(
            step_id="adb_pair",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "host": self._user_data.get(CONF_AIRPLAY_HOST, ""),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HisenseTVOptionsFlow(config_entry)


class HisenseTVOptionsFlow(config_entries.OptionsFlow):

    def __init__(self, config_entry):
        self._config_entry = config_entry
        self._options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            # Parse HDMI input mappings
            input_entity_map = {}
            for item in user_input.get(CONF_HDMI_INPUTS, []):
                hdmi_name = item.get("input", "").strip()
                entity_id = item.get("entity")
                if hdmi_name and entity_id:
                    input_entity_map[hdmi_name] = entity_id

            options = dict(user_input)
            options.pop(CONF_HDMI_INPUTS, None)
            options[CONF_INPUT_ENTITY_MAP] = input_entity_map
            return self.async_create_entry(title="", data=options)

        media_player_sel = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player")
        )
        receiver_sel = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="media_player", device_class="receiver")
        )
        power_sel = selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["switch", "binary_sensor"])
        )
        mode_sel = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value="display_only", label="Display Only"),
                    selector.SelectOptionDict(value="source_via_earc", label="Source via eARC"),
                    selector.SelectOptionDict(value="both", label="Both"),
                ],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        # HDMI input-to-entity mapping
        current_map = self._options.get(CONF_INPUT_ENTITY_MAP, {})
        current_inputs = [
            {"input": k, "entity": v} for k, v in current_map.items()
        ]
        hdmi_input_sel = selector.ObjectSelector(
            selector.ObjectSelectorConfig(
                multiple=True,
                label_field="input",
                description_field="entity",
                fields={
                    "input": {
                        "label": "HDMI Input",
                        "required": True,
                        "selector": {
                            "select": {
                                "options": [
                                    {"value": "Google TV", "label": "Google TV (built-in apps)"},
                                    {"value": "HDMI 1", "label": "HDMI 1"},
                                    {"value": "HDMI 2", "label": "HDMI 2"},
                                    {"value": "HDMI 3", "label": "HDMI 3"},
                                    {"value": "HDMI 4", "label": "HDMI 4"},
                                ],
                                "mode": "dropdown",
                                "custom_value": True,
                            }
                        },
                    },
                    "entity": {
                        "label": "Source Device",
                        "required": True,
                        "selector": {
                            "entity": {"domain": ["media_player", "remote"]},
                        },
                    },
                },
            )
        )

        schema_dict = {}

        # Device mode
        schema_dict[vol.Optional(
            CONF_DEVICE_MODE, default=self._options.get(CONF_DEVICE_MODE, MODE_BOTH)
        )] = mode_sel

        # Jellyfin
        jf = self._options.get(CONF_JELLYFIN_ENTITY)
        jf_key = vol.Optional(CONF_JELLYFIN_ENTITY)
        if jf:
            jf_key = vol.Optional(CONF_JELLYFIN_ENTITY, default=jf)
        schema_dict[jf_key] = media_player_sel

        # Receiver
        recv = self._options.get(CONF_RECEIVER_ENTITY)
        recv_key = vol.Optional(CONF_RECEIVER_ENTITY)
        if recv:
            recv_key = vol.Optional(CONF_RECEIVER_ENTITY, default=recv)
        schema_dict[recv_key] = receiver_sel

        # Receiver input
        current_input = self._options.get(CONF_RECEIVER_INPUT, "")
        receiver = self._options.get(CONF_RECEIVER_ENTITY)
        inputs = _get_receiver_inputs(self.hass, receiver)
        if inputs:
            input_sel = selector.SelectSelector(
                selector.SelectSelectorConfig(options=inputs, mode=selector.SelectSelectorMode.DROPDOWN, custom_value=True)
            )
            input_key = vol.Optional(CONF_RECEIVER_INPUT)
            if current_input:
                input_key = vol.Optional(CONF_RECEIVER_INPUT, default=current_input)
            schema_dict[input_key] = input_sel
        else:
            schema_dict[vol.Optional(CONF_RECEIVER_INPUT, default=current_input)] = str

        # Power
        pwr = self._options.get(CONF_POWER_ENTITY)
        pwr_key = vol.Optional(CONF_POWER_ENTITY)
        if pwr:
            pwr_key = vol.Optional(CONF_POWER_ENTITY, default=pwr)
        schema_dict[pwr_key] = power_sel

        # HDMI input mappings
        schema_dict[vol.Optional(
            CONF_HDMI_INPUTS, default=current_inputs if current_inputs else []
        )] = hdmi_input_sel

        # Persistent ADB getevent stream (fires hisense_tv_key events).
        # Off by default; opt-in per device. See keyevent_stream.py.
        schema_dict[vol.Optional(
            CONF_ENABLE_KEYEVENT_STREAM,
            default=self._options.get(CONF_ENABLE_KEYEVENT_STREAM, False),
        )] = bool

        # Persistent ADB logcat stream watching the projector's
        # AirPlaySystemService_BootupService tag for ActiveIdentifier
        # transitions. Fires `hisense_tv_source_changed` events on
        # every projector input switch. Off by default. See
        # source_event_stream.py.
        schema_dict[vol.Optional(
            CONF_ENABLE_SOURCE_STREAM,
            default=self._options.get(CONF_ENABLE_SOURCE_STREAM, False),
        )] = bool

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )
