"""ADB interface for Hisense Google TV devices.

Extends AdbClientBase with Hisense-specific compound polling (wake state,
input switching, HDMI status, HDR detection, audio routing, media sessions).

Re-exports MediaSessionInfo and ensure_adb_key for existing importers.

Tested on: Hisense L9Q
Should work on: Any Hisense Google TV device (L5G, PX1, PX2, PX3, U8N, etc.)
The `settings get global current_source` command is Hisense-specific.
"""

import logging
import re
from dataclasses import dataclass, field

from .ha_adb_common import AdbClientBase, ensure_adb_key as _ensure_adb_key
from .ha_adb_common import MediaSessionInfo  # noqa: F401  (re-export)
from .ha_adb_common.media_session import parse_all_media_sessions

_LOGGER = logging.getLogger(__name__)

DOMAIN = "hisense_tv"

# Hisense source ID mapping: internal ID -> friendly name
# -1 = Google TV (built-in apps), 3-6 = HDMI 1-4
SOURCE_ID_TO_NAME = {
    -1: "Google TV",
    3: "HDMI 1",
    4: "HDMI 2",
    5: "HDMI 3",
    6: "HDMI 4",
}
SOURCE_NAME_TO_ID = {v: k for k, v in SOURCE_ID_TO_NAME.items()}


@dataclass
class HisenseTVState:
    """Snapshot of Hisense TV state from ADB."""

    awake: bool | None = None
    current_source: int | None = None
    current_source_name: str | None = None
    hdr_active: bool | None = None
    hdmi_connected: dict[str, bool] = field(default_factory=dict)
    earc_connected: bool | None = None
    audio_device: str | None = None
    brightness: int | None = None
    active_app: str | None = None
    media_session: MediaSessionInfo | None = None
    all_media_sessions: list[MediaSessionInfo] = field(default_factory=list)


def ensure_adb_key(hass_config_dir: str) -> str:
    """Ensure an ADB key pair exists for the hisense_tv integration."""
    legacy_paths = [
        # Migrate from old domain name
        __import__("os").path.join(hass_config_dir, ".storage", "hisense_l9q", "adbkey"),
    ]
    return _ensure_adb_key(hass_config_dir, DOMAIN, legacy_paths=legacy_paths)


class AdbClient(AdbClientBase):
    """ADB client for Hisense TV — extends base with compound state polling."""

    async def poll(self) -> HisenseTVState:
        """Poll all state from the device in a single batch."""
        state = HisenseTVState()

        # Single compound command to minimize round trips
        result = await self.shell(
            "dumpsys power 2>/dev/null | grep mWakefulness; "
            "echo '---SEPARATOR---'; "
            "settings get global current_source; "
            "echo '---SEPARATOR---'; "
            "dumpsys display 2>/dev/null | grep mIsHdrLayerPresent; "
            "echo '---SEPARATOR---'; "
            "dumpsys tv_input 2>/dev/null | grep 'HdmiTvInputService/HW'; "
            "echo '---SEPARATOR---'; "
            "settings get global hdmi_earc_connected; "
            "echo '---SEPARATOR---'; "
            "dumpsys audio 2>/dev/null | grep -A1 'STREAM_MUSIC' | grep Devices; "
            "echo '---SEPARATOR---'; "
            "settings get system screen_brightness; "
            "echo '---SEPARATOR---'; "
            "dumpsys activity activities 2>/dev/null | grep mResumedActivity; "
            "echo '---SEPARATOR---'; "
            "dumpsys media_session 2>/dev/null | head -100"
        )

        if not result:
            return state

        parts = result.split("---SEPARATOR---")
        if len(parts) < 8:
            return state

        # 1. Wake state
        wake_str = parts[0].strip()
        if "Awake" in wake_str:
            state.awake = True
        elif "Asleep" in wake_str or "Dozing" in wake_str:
            state.awake = False

        # 2. Current source
        src_str = parts[1].strip()
        try:
            src_id = int(src_str)
            state.current_source = src_id
            state.current_source_name = SOURCE_ID_TO_NAME.get(src_id, f"Source {src_id}")
        except (ValueError, TypeError):
            pass

        # 3. HDR active
        hdr_str = parts[2].strip()
        if "mIsHdrLayerPresent" in hdr_str:
            state.hdr_active = "true" in hdr_str.lower()

        # 4. HDMI connected status
        tv_input_str = parts[3].strip()
        for line in tv_input_str.split("\n"):
            hw_match = re.search(r"HdmiTvInputService/HW(\d+).*state:\s*(\d+)", line)
            if hw_match:
                hw_num = int(hw_match.group(1))
                connected = hw_match.group(2) == "1"
                # HW2=HDMI1, HW3=HDMI2, HW4=HDMI3, HW5=HDMI4
                hdmi_num = hw_num - 1
                name = f"HDMI {hdmi_num}"
                if name in SOURCE_ID_TO_NAME.values():
                    state.hdmi_connected[name] = connected

        # 5. eARC connected
        earc_str = parts[4].strip()
        try:
            state.earc_connected = int(earc_str) > 0
        except (ValueError, TypeError):
            pass

        # 6. Audio device
        audio_str = parts[5].strip()
        if "Devices:" in audio_str:
            devices_part = audio_str.split("Devices:")[-1].strip()
            state.audio_device = devices_part

        # 7. Brightness
        bright_str = parts[6].strip()
        try:
            state.brightness = int(bright_str)
        except (ValueError, TypeError):
            pass

        # 8. Active app
        app_str = parts[7].strip()
        app_match = re.search(r"(\S+)/\.?(\S+)\s", app_str)
        if app_match:
            state.active_app = app_match.group(1)

        # 9. Media session
        if len(parts) >= 9:
            state.all_media_sessions = parse_all_media_sessions(parts[8])
            state.media_session = state.all_media_sessions[0] if state.all_media_sessions else None

        return state

    async def turn_on(self) -> bool:
        """Wake the device."""
        return await self.send_keyevent("wakeup")

    async def turn_off(self) -> bool:
        """Put the device to sleep."""
        return await self.send_keyevent("sleep")

    async def switch_input(self, source_name: str) -> bool:
        """Switch to a different HDMI input."""
        source_id = SOURCE_NAME_TO_ID.get(source_name)
        if source_id is None:
            _LOGGER.warning("Unknown source name: %s", source_name)
            return False

        result = await self.shell(
            f"settings put global current_source {source_id}"
        )
        return result is not None
