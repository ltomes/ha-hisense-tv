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

# Default input device filter — verified on Hisense L9Q (2026-05-08
# via `getevent -lt` enumeration):
#   /dev/input/event1  — "MTK Smart TV IR Receiver" (physical IR remote)
#   /dev/input/event5  — "mediatek,cec" (HDMI CEC line on the projector
#                         side; we can't get CEC at all on Thor's HDMI
#                         ingress per OPEN-QUESTIONS Q-CEC, so this is
#                         our only foothold for CEC opcodes from devices
#                         downstream of the projector)
#   /dev/input/event9  — "SmartRC Consumer Control" (BT remote: media+nav)
#   /dev/input/event10 — "SmartRC Keypad" (BT remote: full keyboard table)
# Excluded: cameras, virtual-search, MTK PMU, front-panel keypad —
# these surface noise unrelated to remote/CEC input.
# Other Hisense Google TV devices may number these differently; the
# device list is overrideable via the KeyEventStreamer constructor.
#
# The ADB command runs `getevent -lt` with NO device argument (Android's
# getevent accepts at most one device path); we filter incoming lines
# against this set in _dispatch_line.
DEFAULT_DEVICES = (
    "/dev/input/event1",
    "/dev/input/event5",
    "/dev/input/event9",
    "/dev/input/event10",
)

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
        self._devices = frozenset(devices)
        self._task: asyncio.Task | None = None
        self._device: Any = None  # AdbDeviceTcpAsync
        self._stop_requested = False
        # Reachability gate driven by media_player's ADB poll.
        # Pre-2026-05-10 this class ran its own reconnect loop with
        # exponential backoff. That stranded silently when the TV was
        # powered off via KEY_POWER: the TCP connection stayed alive
        # while getevent died, streaming_shell with read_timeout_s=None
        # never raised, and the loop never reconnected. Now we wait
        # on this Event and let media_player drive transitions.
        self._reachable = asyncio.Event()

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
        self._reachable.set()  # unblock _run if waiting on reachable
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

    def set_reachable(self, connected: bool) -> None:
        """Drive reachability from media_player's ADB poll.

        Called via dispatcher on every adb.connected transition.
        - connected=True: unblocks _run from waiting on _reachable;
          the next loop iteration will (re)connect and stream.
        - connected=False: clears the gate AND tears down any active
          stream by closing the ADB device. The streaming_shell raises
          on next read, _run drops back to the wait-on-reachable state,
          and stays there silently until media_player flips back on.
        """
        if connected:
            if not self._reachable.is_set():
                _LOGGER.info(
                    "Hisense keyevent: TV reachable per media_player; arming stream"
                )
            self._reachable.set()
        else:
            if self._reachable.is_set():
                _LOGGER.info(
                    "Hisense keyevent: TV unreachable per media_player; tearing down stream"
                )
            self._reachable.clear()
            # Force the in-flight streaming_shell to raise so _run
            # drops out of its async-for and back into the
            # wait-on-reachable branch. Schedule rather than await to
            # avoid blocking the dispatcher caller.
            if self._device is not None:
                self._hass.async_create_task(self._close_device())

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
        """Reachable-gated stream loop.

        Two states with explicit transitions:
          IDLE:    waiting on self._reachable (TV unreachable per
                   media_player). Silent. No probes, no log spam, no
                   ADB attempts. Transitions to ACTIVE when media_player
                   dispatches connected=True.
          ACTIVE:  TV reachable. Connect, stream, dispatch events.
                   On any read error (incl. read_timeout=30s catching
                   getevent-died-but-TCP-alive), drop back to top of
                   loop and reassess: if still reachable per
                   media_player, immediate reconnect (cheap on a
                   working TV); if no longer reachable, fall back to
                   IDLE wait.
        """
        while not self._stop_requested:
            if not self._reachable.is_set():
                # Quiet wait. media_player will flip this when its
                # 10s/300s ADB poll succeeds.
                await self._reachable.wait()
                if self._stop_requested:
                    break

            # ACTIVE — TV is reachable. Try to connect.
            if not await self._connect():
                # Reachable says yes but our connect failed — likely a
                # transient race during media_player's first successful
                # poll. Give it 5s and retry. If TV truly unreachable,
                # media_player's next poll will flip us back to IDLE.
                _LOGGER.debug(
                    "Hisense keyevent: connect failed despite reachable; retrying in 5s"
                )
                try:
                    await asyncio.wait_for(asyncio.sleep(5), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue

            cmd = "getevent -lt"
            try:
                # read_timeout_s=30: catches the case where the TCP
                # connection stays alive but getevent died (TV
                # power-off without dropping ADB). 30s is short enough
                # that we recover quickly when this happens, but long
                # enough that legitimate idle (remote held still)
                # doesn't trigger constant reconnects on a working TV.
                # If reachable=True still: reconnect is cheap (~1s) so
                # the user-visible cost of a false reconnect is near
                # zero.
                async for raw in self._device.streaming_shell(cmd, read_timeout_s=30.0):
                    if self._stop_requested:
                        break
                    self._dispatch_chunk(raw)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                # Don't WARN-log routine reconnects (TV idle for 30s
                # while reachable). Logging at DEBUG keeps noise down;
                # if the user is debugging they can enable DEBUG for
                # this module.
                _LOGGER.debug(
                    "Hisense keyevent stream interrupted (%s); reassessing reachability",
                    err,
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
        if device not in self._devices:
            return
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
