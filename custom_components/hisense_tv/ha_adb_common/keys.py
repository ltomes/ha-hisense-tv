"""ADB key management — generation and migration."""

import logging
import os
import shutil

_LOGGER = logging.getLogger(__name__)


def ensure_adb_key(
    hass_config_dir: str,
    domain: str,
    legacy_paths: list[str] | None = None,
) -> str:
    """Ensure an ADB key pair exists. Migrate from legacy paths or generate if missing.

    Args:
        hass_config_dir: Home Assistant config directory.
        domain: Integration domain name (e.g. "nvidia_shield", "hisense_tv").
            Used to namespace the key storage.
        legacy_paths: Optional list of legacy key paths to check for migration,
            in priority order. Avoids forcing device re-pairing.

    Returns:
        Path to the private key file.
    """
    key_dir = os.path.join(hass_config_dir, ".storage", domain)
    os.makedirs(key_dir, exist_ok=True)
    key_path = os.path.join(key_dir, "adbkey")

    if os.path.exists(key_path):
        return key_path

    # Try migrating from legacy locations
    all_legacy = list(legacy_paths or [])
    # Always check the generic location as a last resort
    generic = os.path.join(hass_config_dir, ".storage", "adbkey")
    if generic not in all_legacy:
        all_legacy.append(generic)

    for legacy_key in all_legacy:
        if os.path.exists(legacy_key):
            shutil.copy2(legacy_key, key_path)
            pub = legacy_key + ".pub"
            if os.path.exists(pub):
                shutil.copy2(pub, key_path + ".pub")
            _LOGGER.info("Migrated ADB key from %s to %s", legacy_key, key_path)
            return key_path

    # Generate new key
    from adb_shell.auth.keygen import keygen

    keygen(key_path)
    _LOGGER.info("Generated new ADB key at %s", key_path)

    return key_path
