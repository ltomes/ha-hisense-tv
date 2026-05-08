"""Constants for the Hisense TV integration."""

# Re-export shared Media DAG constants
from .ha_adb_common.const import (  # noqa: F401
    ATTR_MEDIA_DAG_JELLYFIN_ENTITY,
    ATTR_MEDIA_DAG_NOW_PLAYING,
    ATTR_MEDIA_DAG_RECEIVER_ENTITY,
    ATTR_MEDIA_DAG_RECEIVER_INPUT,
    ATTR_MEDIA_DAG_ROLE,
    ATTR_MEDIA_DAG_RUNNING_APPS,
)

DOMAIN = "hisense_tv"

CONF_JELLYFIN_ENTITY = "jellyfin_entity"
CONF_RECEIVER_ENTITY = "receiver_entity"
CONF_RECEIVER_INPUT = "receiver_input"
CONF_POWER_ENTITY = "power_entity"
CONF_DEVICE_MODE = "device_mode"
CONF_AIRPLAY_HOST = "airplay_host"
CONF_AIRPLAY_PORT = "airplay_port"
CONF_ADB_PORT = "adb_port"
CONF_INPUT_ENTITY_MAP = "input_entity_map"  # HDMI input name -> entity_id

# Real-time Android keypress capture via persistent ADB getevent stream.
# Fires `hisense_tv_key` HA events for downstream automations and any
# external consumer (e.g. the Thor jmp-input-bridge). Opt-in; default
# off. See keyevent_stream.py and bare-metal/nodes/thor/INPUT-PLAN.md.
CONF_ENABLE_KEYEVENT_STREAM = "enable_keyevent_stream"

CONF_ADB_PAIRED = "adb_paired"

DEFAULT_NAME = "Hisense TV"
DEFAULT_ADB_PORT = 5555
DEFAULT_AIRPLAY_PORT = 7000

# Device modes
MODE_DISPLAY_ONLY = "display_only"      # Receives from receiver, display only
MODE_SOURCE_EARC = "source_via_earc"    # Uses built-in apps, sends audio back via eARC
MODE_BOTH = "both"                       # Can do either depending on context

DEVICE_MODES = [MODE_DISPLAY_ONLY, MODE_SOURCE_EARC, MODE_BOTH]

# Additional DAG attributes specific to display devices
ATTR_MEDIA_DAG_INPUT_MAP = "media_dag_input_map"
