"""Android media session parsing.

Parses `dumpsys media_session` output from ADB to extract now-playing
information from any app using Android's MediaSession API.
"""

from dataclasses import dataclass


@dataclass
class MediaSessionInfo:
    """Parsed media session data from Android."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    package: str | None = None
    playing: bool = False
    paused: bool = False


def parse_all_media_sessions(dump: str) -> list[MediaSessionInfo]:
    """Parse `dumpsys media_session` output for ALL active media sessions.

    Returns a list of every active session that has metadata or a known
    playback state, sorted: playing first, then paused, then others.
    """
    if not dump:
        return []

    sessions: list[MediaSessionInfo] = []
    current_pkg: str | None = None
    in_active = False
    current_state = 0  # 0=unknown, 2=paused, 3=playing
    current_meta: MediaSessionInfo | None = None

    for line in dump.split("\n"):
        stripped = line.strip()

        if "package=" in stripped:
            pkg = stripped.split("package=", 1)
            if len(pkg) > 1:
                # New session block — save the previous one if valid
                if in_active and current_meta:
                    current_meta.package = current_pkg
                    sessions.append(current_meta)
                current_pkg = pkg[1].strip()
                in_active = False
                current_state = 0
                current_meta = None

        if "active=true" in stripped:
            in_active = True

        if in_active and "state=PlaybackState" in stripped:
            if "state=3" in stripped:
                current_state = 3
            elif "state=2" in stripped:
                current_state = 2

        if in_active and "metadata: size=" in stripped and "description=" in stripped:
            desc_start = stripped.find("description=")
            if desc_start < 0:
                continue
            desc = stripped[desc_start + 12:]
            parts = [p.strip() for p in desc.split(", ")]

            info = MediaSessionInfo()
            info.package = current_pkg
            info.playing = current_state == 3
            info.paused = current_state == 2

            if len(parts) >= 1 and parts[0] not in ("null", ""):
                info.title = parts[0]
            if len(parts) >= 2 and parts[1] not in ("null", ""):
                info.artist = parts[1]
            if len(parts) >= 3 and parts[2] not in ("null", ""):
                info.album = parts[2]

            if info.title:
                current_meta = info

    # Don't forget the last session
    if in_active and current_meta:
        current_meta.package = current_pkg
        sessions.append(current_meta)

    # Sort: playing first, then paused, then others
    def sort_key(s: MediaSessionInfo) -> int:
        if s.playing:
            return 0
        if s.paused:
            return 1
        return 2

    sessions.sort(key=sort_key)
    return sessions


def parse_media_session(
    dump: str, foreground_package: str | None = None
) -> MediaSessionInfo | None:
    """Parse `dumpsys media_session` output for the best active session.

    Returns the best match from all active sessions:
    1. The session whose package matches the foreground app (if provided)
    2. The session that is currently playing (state=3)
    3. The session that is paused (state=2)
    4. Any active session with metadata
    """
    sessions = parse_all_media_sessions(dump)
    if not sessions:
        return None

    # Prefer the session matching the foreground app
    if foreground_package:
        for s in sessions:
            if s.package == foreground_package:
                return s

    # Already sorted: playing > paused > other
    return sessions[0]
