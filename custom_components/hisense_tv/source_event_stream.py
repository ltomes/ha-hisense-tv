"""Real-time Hisense projector input-source change capture via logcat.

Opens a long-lived `logcat -v threadtime -s AirPlaySystemService_BootupService:I`
stream against the projector and fires `hisense_tv_source_changed` HA
events whenever the `ActiveIdentifier` field transitions to a new value.

Consumers (HA automations, the Thor jmp-input-bridge HDMI gate) listen
on the event bus and decide whether their downstream action should
proceed for the current source.

Why this exists
---------------
The integration's existing media_player poll reads `current_source` via
`settings get global current_source` every 10s/300s (`adb.py:74`). That
cached value goes stale between polls — a remote keypress arriving
2 seconds after the user switched projector input would see the OLD
source value. The Thor jmp-input-bridge cannot tolerate that stale
window: misrouting one stray press silently navigates tsukimi into a
random sub-page. We need a push-from-projector signal.

Lineage / why streaming_shell
-----------------------------
- HA's `ha_adb_common` was branched from the `adb_exporter` container
  (a separate ADB exporter service),
  itself wrapping the public `adb-shell` PyPI library.
- `adb_exporter` uses `logcat -d` (dump-and-exit, polled, shipped to
  Loki). It does NOT stream — see exporter.py:489-493.
- This module is the first STREAMING logcat consumer on this infra.
  It uses `AdbDeviceTcpAsync.streaming_shell()` from `adb-shell` —
  the same primitive `keyevent_stream.py` uses for `getevent -lt`.

Source-change signal (verified 2026-05-10 by direct logcat capture
during user-initiated input toggle):
    Tag:    AirPlaySystemService_BootupService (Priority I)
    PID:    1667 (mediatek vendor service)
    Cadence: emits getSystemCB compound line every ~3s AND on change.
             We extract ActiveIdentifier=N and InputSourceName=N=<name>
             and fire only on N transitions (heartbeats suppressed).

Observed source identifiers on the Hisense L9Q (SmartLaser 4K):
    ActiveIdentifier=1  → Google TV Home (built-in apps)
    ActiveIdentifier=6  → HDMI 3 (Thor input via MC1)
    Other HDMI inputs and apps map to their own integer IDs; the
    accompanying InputSourceName=N=<name> field in the same line
    is the authoritative human-readable name.

Event format
------------
Fires `hisense_tv_source_changed` on the HA bus with:
    entry_id    — integration config-entry id
    host        — projector IP
    active_id   — integer (current ActiveIdentifier)
    source_name — string (e.g. "HDMI 3", "Google TV Home")
    prev_id     — previous integer (or None on first detection)
    prev_name   — previous string (or None on first detection)

References
----------
- bare-metal/containers/jmp-input-bridge/src/hdmi_gate.py (consumer)
- bare-metal/nodes/thor/INPUT-GATING-RESEARCH.md (why DRM-side alone
  is insufficient — MC1 keeps Thor↔MC1 link alive across projector
  input switches; only the projector's own logcat knows direction)
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_NAME = "hisense_tv_source_changed"

# Tag and command. `-s AirPlaySystemService_BootupService:*` would also
# capture lower-priority levels, but the getSystemCB line is fired at
# Priority I; restricting to I keeps incidental noise out without
# missing the transitions.
_LOGCAT_TAG = "AirPlaySystemService_BootupService"
_LOGCAT_CMD = f"logcat -v threadtime -s {_LOGCAT_TAG}:I"

# Match the getSystemCB compound line. We pull two fields:
#   ActiveIdentifier=<int>
#   InputSourceName=<int>=<human name>     (always matches the active id)
# The same line carries many other key=value pairs; we ignore them.
_ACTIVE_ID_RE = re.compile(r"ActiveIdentifier=(\d+)")
# The projector emits exactly ONE InputSourceName=N=<name> per
# getSystemCB line, and it always reflects the CURRENT active source.
# Verified 2026-05-10: ActiveIdentifier and InputSourceName use
# different ID spaces (e.g. Google TV Home is ActiveIdentifier=1 but
# InputSourceName=14=Google TV Home), so we cannot cross-match by ID.
# Take the name from InputSourceName directly; treat ActiveIdentifier
# only as transition-detection metadata.
_SOURCE_NAME_RE = re.compile(r"InputSourceName=(\d+)=([^/]+?)(?:/[A-Z]|$)")

_BACKOFF_INITIAL = 5.0
_BACKOFF_MAX = 60.0
_BACKOFF_GROWTH = 1.5


class SourceEventStreamer:
    """Drives the persistent ADB logcat stream and HA event firing."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        key_path: str,
        entry_id: str,
    ) -> None:
        self._hass = hass
        self._host = host
        self._port = port
        self._key_path = key_path
        self._entry_id = entry_id
        self._task: asyncio.Task | None = None
        self._device: Any = None  # AdbDeviceTcpAsync
        self._stop_requested = False
        self._reachable = asyncio.Event()
        # Last-seen state for transition detection. None on first line
        # after (re)connect — we always fire the first parsed value
        # so subscribers (the Thor bridge) get a known-good baseline
        # without having to wait for the user to toggle.
        self._last_active_id: int | None = None
        self._last_source_name: str | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_requested = False
        self._task = self._hass.loop.create_task(
            self._run(), name=f"hisense_tv_source_stream:{self._entry_id}"
        )
        _LOGGER.info(
            "Hisense source-event stream task launched (host=%s, tag=%s)",
            self._host,
            _LOGCAT_TAG,
        )

    async def stop(self) -> None:
        self._stop_requested = True
        self._reachable.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Hisense source-event stop saw: %s", err)
        self._task = None
        await self._close_device()

    def set_reachable(self, connected: bool) -> None:
        """Drive reachability from media_player's ADB poll.

        Same pattern as KeyEventStreamer.set_reachable: connected=True
        unblocks the loop; connected=False tears down the current
        stream by closing the ADB device and lets the loop fall back
        to wait-on-reachable.
        """
        if connected:
            if not self._reachable.is_set():
                _LOGGER.info(
                    "Hisense source-event: TV reachable; arming stream"
                )
            self._reachable.set()
        else:
            if self._reachable.is_set():
                _LOGGER.info(
                    "Hisense source-event: TV unreachable; tearing down stream"
                )
            self._reachable.clear()
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
                "Hisense source-event stream ADB connected (%s:%d)",
                self._host,
                self._port,
            )
            # Reset last-known on every reconnect so the first parsed
            # line fires an event and re-establishes the baseline for
            # downstream consumers (which may have missed transitions
            # while we were disconnected).
            self._last_active_id = None
            self._last_source_name = None
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Hisense source-event stream connect failed: %s", err)
            await self._close_device()
            return False

    async def _run(self) -> None:
        while not self._stop_requested:
            if not self._reachable.is_set():
                await self._reachable.wait()
                if self._stop_requested:
                    break

            if not await self._connect():
                try:
                    await asyncio.wait_for(asyncio.sleep(5), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                # read_timeout_s=30: getSystemCB fires every ~3s, so
                # 30s is generous. If the projector goes off-network
                # the read_timeout catches it and we drop back to
                # reassess reachability.
                async for raw in self._device.streaming_shell(_LOGCAT_CMD, read_timeout_s=30.0):
                    if self._stop_requested:
                        break
                    self._dispatch_chunk(raw)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Hisense source-event stream interrupted (%s); "
                    "reassessing reachability",
                    err,
                )
            finally:
                await self._close_device()

    def _dispatch_chunk(self, chunk: str) -> None:
        for line in chunk.splitlines():
            if line:
                self._dispatch_line(line)

    def _dispatch_line(self, line: str) -> None:
        # Fast-reject lines that don't contain the field we need.
        # logcat -s filters by tag but each line is the full message;
        # the heartbeat lines all carry ActiveIdentifier, so this
        # simple containment check is enough.
        if "ActiveIdentifier=" not in line:
            return
        m_id = _ACTIVE_ID_RE.search(line)
        if not m_id:
            return
        active_id = int(m_id.group(1))

        # InputSourceName=N=<name> — N uses a different ID space than
        # ActiveIdentifier. Take the name from the single (verified)
        # InputSourceName field per line; it always reflects the
        # current active source on this device.
        source_name: str | None = None
        m_src = _SOURCE_NAME_RE.search(line)
        if m_src:
            source_name = m_src.group(2).strip()

        # Skip heartbeat: same (id, name) as last seen — suppress fire.
        if (
            active_id == self._last_active_id
            and source_name == self._last_source_name
        ):
            return

        prev_id = self._last_active_id
        prev_name = self._last_source_name
        self._last_active_id = active_id
        self._last_source_name = source_name

        _LOGGER.info(
            "Hisense source change: %s (id=%s) -> %s (id=%s)",
            prev_name,
            prev_id,
            source_name,
            active_id,
        )
        self._hass.bus.async_fire(
            EVENT_NAME,
            {
                "entry_id": self._entry_id,
                "host": self._host,
                "active_id": active_id,
                "source_name": source_name,
                "prev_id": prev_id,
                "prev_name": prev_name,
            },
        )
