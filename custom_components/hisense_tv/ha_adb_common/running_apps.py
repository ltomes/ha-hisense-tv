"""Build the running apps list from recent tasks + media sessions.

Produces the media_dag_running_apps attribute value that media_zone reads.
Recent tasks (from dumpsys activity recents) provide the list of running apps.
Media sessions provide playback state for apps that have active sessions.
"""

from .media_session import MediaSessionInfo


def running_apps_from_tasks(
    recent_tasks: list[dict],
    media_sessions: list[MediaSessionInfo] | None = None,
    foreground_package: str | None = None,
) -> list[dict] | None:
    """Build a list of running app dicts from recent tasks + media sessions.

    Args:
        recent_tasks: List of {"package": str} from ADB recent tasks.
        media_sessions: Optional media sessions for playback state enrichment.
        foreground_package: The current foreground app package.

    Returns None if there are fewer than 2 apps (no point showing a switcher
    with only one option).
    """
    if not recent_tasks or len(recent_tasks) < 2:
        return None

    # Index media sessions by package for quick lookup
    session_by_pkg: dict[str, MediaSessionInfo] = {}
    if media_sessions:
        for s in media_sessions:
            if s.package:
                session_by_pkg[s.package] = s

    apps = []
    for task in recent_tasks:
        pkg = task["package"]
        app: dict = {"package": pkg}

        # Mark foreground app
        if pkg == foreground_package:
            app["foreground"] = True

        # Enrich with media session data if available
        session = session_by_pkg.get(pkg)
        if session:
            if session.playing:
                app["state"] = "playing"
            elif session.paused:
                app["state"] = "paused"
            else:
                app["state"] = "idle"
            if session.title:
                app["title"] = session.title
            if session.artist:
                app["artist"] = session.artist
        else:
            app["state"] = "running"

        apps.append(app)

    return apps if len(apps) >= 2 else None


# Keep backward compat for any code still using this
def running_apps_from_sessions(sessions: list[MediaSessionInfo]) -> list[dict] | None:
    """Build running apps from media sessions only (legacy)."""
    if not sessions:
        return None
    tasks = [{"package": s.package} for s in sessions if s.package]
    return running_apps_from_tasks(tasks, sessions)
