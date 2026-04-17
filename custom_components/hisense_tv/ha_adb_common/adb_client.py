"""Base ADB client with shared connection management and commands.

Subclasses add device-specific polling (media sessions, wake state, etc.).
"""

import logging

from .media_session import MediaSessionInfo, parse_all_media_sessions, parse_media_session

_LOGGER = logging.getLogger(__name__)

# Standard Android media key codes
MEDIA_KEYCODES = {
    "play": 126,
    "pause": 127,
    "play-pause": 85,
    "stop": 86,
    "next": 87,
    "previous": 88,
}

DEVICE_KEYCODES = {
    "power": 26,
    "sleep": 223,
    "wakeup": 224,
    "volume_up": 24,
    "volume_down": 25,
    "mute": 164,
}

ALL_KEYCODES = {**MEDIA_KEYCODES, **DEVICE_KEYCODES}


class AdbClientBase:
    """Base ADB client with connection management and common commands."""

    def __init__(self, host: str, port: int, key_path: str):
        self._host = host
        self._port = port
        self._key_path = key_path
        self._device = None
        self._connected = False

        # Media session state
        self._last_info: MediaSessionInfo | None = None
        self._all_sessions: list[MediaSessionInfo] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_info(self) -> MediaSessionInfo | None:
        return self._last_info

    @property
    def all_sessions(self) -> list[MediaSessionInfo]:
        return self._all_sessions

    def clear_info(self) -> None:
        """Clear the cached media session info."""
        self._last_info = None
        self._all_sessions = []

    async def connect(self) -> bool:
        """Connect to the device via ADB."""
        try:
            import asyncio

            from adb_shell.adb_device_async import AdbDeviceTcpAsync
            from adb_shell.auth.sign_pythonrsa import PythonRSASigner

            # FromRSAKeyPath does blocking file I/O — run it in the default
            # executor so we don't stall Home Assistant's event loop.
            loop = asyncio.get_running_loop()
            signer = await loop.run_in_executor(
                None, PythonRSASigner.FromRSAKeyPath, self._key_path
            )
            self._device = AdbDeviceTcpAsync(self._host, self._port)
            await self._device.connect(rsa_keys=[signer], auth_timeout_s=10)
            self._connected = True
            _LOGGER.info("ADB connected to %s:%d", self._host, self._port)
            return True
        except Exception as err:
            _LOGGER.debug("ADB connection failed: %s", err)
            self._connected = False
            self._device = None
            return False

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        if self._device:
            try:
                await self._device.close()
            except Exception:
                pass
        self._device = None
        self._connected = False

    async def shell(self, cmd: str) -> str | None:
        """Run a shell command, reconnecting if needed."""
        if not self._connected or not self._device:
            if not await self.connect():
                return None
        try:
            return await self._device.shell(cmd)
        except Exception as err:
            _LOGGER.debug("ADB shell failed: %s", err)
            self._connected = False
            self._device = None
            return None

    # -- Common commands --

    async def send_keyevent(self, command: str) -> bool:
        """Send a key event via ADB input keyevent."""
        keycode = ALL_KEYCODES.get(command)
        if keycode is None:
            _LOGGER.warning("Unknown keyevent command: %s", command)
            return False
        result = await self.shell(f"input keyevent {keycode}")
        if result is not None:
            _LOGGER.debug("ADB keyevent %d (%s)", keycode, command)
        return result is not None

    async def send_media_command(self, command: str) -> bool:
        """Send a media command via ADB input keyevent."""
        keycode = MEDIA_KEYCODES.get(command)
        if keycode is None:
            _LOGGER.warning("Unknown media command: %s", command)
            return False
        result = await self.shell(f"input keyevent {keycode}")
        if result is not None:
            _LOGGER.debug("ADB keyevent %d (%s)", keycode, command)
            return True
        return False

    async def bring_to_foreground(self, package: str) -> bool:
        """Bring an app to the foreground using monkey command."""
        result = await self.shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1 2>/dev/null"
        )
        if result is not None:
            _LOGGER.debug("Brought %s to foreground via monkey", package)
        return result is not None

    async def is_awake(self) -> bool | None:
        """Check if the device screen is on / awake via ADB."""
        result = await self.shell("dumpsys power 2>/dev/null | grep mWakefulness")
        if result:
            return "Awake" in result
        return None

    # -- Media session polling --

    async def poll_media_sessions(
        self, foreground_package: str | None = None, head_lines: int = 200
    ) -> MediaSessionInfo | None:
        """Query media sessions. Stores all sessions and returns the best match."""
        dump = await self.shell(
            f"dumpsys media_session 2>/dev/null | head -{head_lines}"
        )
        if dump is None:
            return None
        self._all_sessions = parse_all_media_sessions(dump)
        self._last_info = parse_media_session(dump, foreground_package)
        return self._last_info

    # -- Running apps (recent visible tasks) --

    async def poll_running_apps(self) -> list[dict]:
        """Get running apps from Android's recent tasks.

        Returns a list of dicts with 'package' and 'foreground' keys,
        ordered by recency (most recent first). Only includes visible
        user apps, filtering out system UI, home, and invisible tasks.
        """
        result = await self.shell(
            "dumpsys activity recents 2>/dev/null "
            "| grep -E 'Recent #|realActivity|visible='"
        )
        if not result:
            return []

        import re
        apps = []
        seen = set()

        # System packages to exclude
        _EXCLUDED = {
            "com.android.systemui",
            "com.android.tv.settings",
            "com.nvidia.diagtools",
            "android",
        }

        for line in result.split("\n"):
            stripped = line.strip()

            # Match recent task lines with visibility and package info
            if "Recent #" in stripped and "visible=true" in stripped:
                # Extract package from A=uid:package pattern
                match = re.search(r"A=\d+:(\S+)", stripped)
                if not match:
                    # Try I=package/activity pattern
                    match = re.search(r"I=(\S+)/", stripped)
                if match:
                    pkg = match.group(1)
                    if pkg not in seen and pkg not in _EXCLUDED and not pkg.startswith("."):
                        seen.add(pkg)
                        apps.append({"package": pkg})

        return apps
