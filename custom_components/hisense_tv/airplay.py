"""AirPlay device info reader for Hisense TV devices.

Reads device information from the AirPlay 2 server on port 7000.
This endpoint requires no authentication and returns rich device metadata
including display capabilities, HDR modes, firmware version, and more.
"""

import ipaddress
import logging
import plistlib
import re
from dataclasses import dataclass, field

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5


@dataclass
class DisplayCapabilities:
    """Display capabilities from AirPlay info."""

    width_max: int = 0
    height_max: int = 0
    width_native: int = 0
    height_native: int = 0
    max_fps: int = 0
    hdr_modes: list[str] = field(default_factory=list)
    dolby_vision_codecs: list[str] = field(default_factory=list)
    max_bitrate_peak: int = 0
    max_bitrate_avg: int = 0
    uuid: str = ""


@dataclass
class AirPlayDeviceInfo:
    """Parsed AirPlay device information."""

    name: str = ""
    model: str = ""
    manufacturer: str = ""
    hardware_revision: str = ""
    serial_number: str = ""
    firmware_version: str = ""
    firmware_build_date: str = ""
    device_id: str = ""
    os: str = ""
    sdk_version: str = ""
    source_version: str = ""
    build: str = ""
    volume_control_type: int = 0
    display: DisplayCapabilities = field(default_factory=DisplayCapabilities)
    raw: dict = field(default_factory=dict)


def _validate_host(host: str) -> bool:
    """Validate that host is a safe IP address or hostname."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # Allow hostnames but reject anything with slashes or special chars
    return bool(re.match(r'^[a-zA-Z0-9._-]+$', host))


async def fetch_airplay_info(host: str, port: int = 7000) -> AirPlayDeviceInfo | None:
    """Fetch and parse AirPlay device info from the /info endpoint.

    Returns None if the device is unreachable or doesn't respond.
    """
    if not _validate_host(host):
        _LOGGER.warning("Invalid AirPlay host: %s", host)
        return None

    url = f"http://{host}:{port}/info"

    try:
        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.debug("AirPlay info returned %d", resp.status)
                    return None

                data = await resp.read()
                raw = plistlib.loads(data)

    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Failed to fetch AirPlay info from %s: %s", host, err)
        return None
    except plistlib.InvalidFileException:
        _LOGGER.debug("Invalid plist from %s", host)
        return None

    info = AirPlayDeviceInfo(
        name=raw.get("name", ""),
        model=raw.get("model", ""),
        manufacturer=raw.get("manufacturer", ""),
        hardware_revision=raw.get("hardwareRevision", ""),
        serial_number=raw.get("serialNumber", ""),
        firmware_version=raw.get("firmwareRevision", ""),
        firmware_build_date=raw.get("firmwareBuildDate", ""),
        device_id=raw.get("deviceID", ""),
        os=raw.get("operatingSystem", ""),
        sdk_version=raw.get("sdk", ""),
        source_version=raw.get("sourceVersion", ""),
        build=raw.get("build", ""),
        volume_control_type=raw.get("volumeControlType", 0),
        raw=raw,
    )

    # Parse display info
    displays = raw.get("displays", [])
    if displays:
        d = displays[0]
        info.display = DisplayCapabilities(
            width_max=d.get("widthPixelsMax", 0),
            height_max=d.get("heightPixelsMax", 0),
            width_native=d.get("widthPixels", 0),
            height_native=d.get("heightPixels", 0),
            max_fps=d.get("maxFPS", 0),
            uuid=d.get("uuid", ""),
            max_bitrate_peak=d.get("HDRInfo", {}).get("highestPlayablePeakBitRate", 0),
            max_bitrate_avg=d.get("HDRInfo", {}).get("highestPlayableAverageBitRate", 0),
        )

        # Collect HDR modes
        hdr_modes = []
        dolby_codecs = []
        for mode in d.get("HDRSupportedModes", []):
            name = mode.get("HDRMode", "")
            if name and name not in hdr_modes:
                hdr_modes.append(name)
            for codec in mode.get("codecStrings", []):
                if codec not in dolby_codecs:
                    dolby_codecs.append(codec)

        info.display.hdr_modes = hdr_modes
        info.display.dolby_vision_codecs = dolby_codecs

    return info
