"""Real-time Android keypress capture via ADB getevent.

Opens a long-lived `getevent -lt` stream against the Hisense Google TV
device's input event devices (TV, laser projector, etc.) and fires
`hisense_tv_key` HA events for every EV_KEY transition. Consumers
(HA automations, the Thor jmp-input-bridge container, anything else
that wants real-time remote presses) listen on the event bus.

Connection model
----------------
We hold a SECOND, dedicated `AdbDeviceTcpAsync` connection rather than
sharing the existing media_player polling client. `streaming_shell`
holds the ADB session for the lifetime of the command, and we don't
want to block the existing 10s-interval state poll on it. Modern
adbd accepts multiple parallel TCP connections from authorized clients,
so this is safe.

Why streaming_shell at all
--------------------------
Verified 2026-05-08: the HA container has no `adb` binary, so a
subprocess-based path is closed. `adb-shell 0.4.4` ships
`AdbDeviceTcpAsync.streaming_shell()` as an async generator yielding
lines as they arrive. That's exactly what we need.

Event format
------------
Fires `hisense_tv_key` on the HA bus with:
    entry_id   — the integration config-entry id (stable identifier)
    host       — the projector's IP (convenience for matching)
    key        — symbolic Linux key name (e.g. "KEY_VOLUMEUP")
                 or hex code if `getevent -l` couldn't translate it
    action     — "down" | "up" | "repeat"
    device     — "/dev/input/event9" or "/dev/input/event10"

References
----------
- `bare-metal/nodes/thor/INPUT-PLAN.md` — Piece 1, Open Q-1.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_NAME = "hisense_tv_key"

# Default input device nodes — verified on Hisense L9Q (2026-05-08
# via `getevent -lp`):
#   /dev/input/event9  — "SmartRC Consumer Control" (media + nav remote)
#   /dev/input/event10 — "SmartRC Keypad" (full keyboard table)
# Other Hisense Google TV devices may number these differently; the
# device list is overrideable via the KeyEventStreamer constructor.
DEFAULT_DEVICES = ("/dev/input/event9", "/dev/input/event10")

# `getevent -lt` line format (timestamp + key form):
#   [   13104.094503] /dev/input/event9: EV_KEY       KEY_VOLUMEUP         DOWN
#   [   13104.149534] /dev/input/event9: EV_KEY       KEY_VOLUMEUP         UP
#   [   13104.094503] /dev/input/event9: EV_KEY       022a                 DOWN
# When `-l` cannot translate the keycode, the second column is hex (e.g. 022a).
# Action column is "DOWN", "UP", or a hex value like "00000002" for repeat.
_EV_KEY_RE = re.compile(
    r"^\[\s*[\d.]+\s*\]\s+(/dev/input/event\d+):\s+EV_KEY\s+(\S+)\s+(\S+)\s*$"
)
_TEXT_ACTIONS = {"DOWN": "down", "UP": "up"}

# Stream restart backoff: start fast, cap at 60s. ADB reconnect is cheap
# when the projector is reachable; long backoff just hurts reactivity
# after a transient drop.
_BACKOFF_INITIAL = 5.0
_BACKOFF_MAX = 60.0
_BACKOFF_GROWTH = 1.5


class KeyEventStreamer:
    """Drives the persistent ADB getevent stream and HA event firing."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        key_path: str,
        entry_id: str,
        devices: tuple[str, ...] = DEFAULT_DEVICES,
    ) -> None:
        self._hass = hass
        self._host = host
        self._port = port
        self._key_path = key_path
        self._entry_id = entry_id
        self._devices = devices
        self._task: asyncio.Task | None = None
        self._device: Any = None  # AdbDeviceTcpAsync
        self._stop_requested = False

    async def start(self) -> None:
        """Spawn the background task that maintains the stream."""
        if self._task and not self._task.done():
            return
        self._stop_requested = False
        self._task = self._hass.loop.create_task(
            self._run(), name=f"hisense_tv_keyevent_stream:{self._entry_id}"
        )
        _LOGGER.info(
            "Hisense keyevent stream task launched (host=%s, devices=%s)",
            self._host,
            list(self._devices),
        )

    async def stop(self) -> None:
        """Cancel the stream task and close the ADB connection."""
        self._stop_requested = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Hisense keyevent stop saw: %s", err)
        self._task = None
        await self._close_device()

    # -- internals --

    async def _close_device(self) -> None:
        if self._device is None:
            return
        try:
            await self._device.close()
        except Exception:  # noqa: BLE001
            pass
        self._device = None

    async def _connect(self) -> bool:
        """Open a fresh streaming-dedicated ADB connection."""
        try:
            from adb_shell.adb_device_async import AdbDeviceTcpAsync
            from adb_shell.auth.sign_pythonrsa import PythonRSASigner

            loop = asyncio.get_running_loop()
            signer = await loop.run_in_executor(
                None, PythonRSASigner.FromRSAKeyPath, self._key_path
            )
            self._device = AdbDeviceTcpAsync(self._host, self._port)
            await self._device.connect(rsa_keys=[signer], auth_timeout_s=10)
            _LOGGER.info(
                "Hisense keyevent stream ADB connected (%s:%d)",
                self._host,
                self._port,
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Hisense keyevent stream connect failed: %s", err)
            await self._close_device()
            return False

    async def _run(self) -> None:
        """Connect → stream → reconnect-on-failure forever (until stop)."""
        backoff = _BACKOFF_INITIAL
        while not self._stop_requested:
            if not await self._connect():
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_GROWTH, _BACKOFF_MAX)
                continue
            backoff = _BACKOFF_INITIAL  # success — reset

            cmd = "getevent -lt " + " ".join(self._devices)
            try:
                # read_timeout_s=None: idle periods between key presses are
                # the normal case (TV remote held still). We want the read
                # to wait indefinitely for the next event rather than the
                # default 10s timeout.
                async for raw in self._device.streaming_shell(cmd, read_timeout_s=None):
                    if self._stop_requested:
                        break
                    self._dispatch_chunk(raw)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Hisense keyevent stream lost (%s); reconnecting", err
                )
            finally:
                await self._close_device()

    def _dispatch_chunk(self, chunk: str) -> None:
        """A streaming_shell yield can contain multiple newlines; split safely."""
        for line in chunk.splitlines():
            if line:
                self._dispatch_line(line)

    def _dispatch_line(self, line: str) -> None:
        m = _EV_KEY_RE.match(line)
        if not m:
            return
        device, key, action_raw = m.group(1), m.group(2), m.group(3)
        action = _TEXT_ACTIONS.get(action_raw)
        if action is None:
            # Numeric — typically autorepeat (2). 0 = up, 1 = down, anything
            # else = repeat. Hex strings come through as e.g. "00000002".
            try:
                v = int(action_raw, 16)
            except ValueError:
                return
            action = "up" if v == 0 else "down" if v == 1 else "repeat"
        self._hass.bus.async_fire(
            EVENT_NAME,
            {
                "entry_id": self._entry_id,
                "host": self._host,
                "key": key,
                "action": action,
                "device": device,
            },
        )
