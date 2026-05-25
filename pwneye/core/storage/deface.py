from pathlib import Path
from typing import Any

from pwneye.core.storage import cache


def save_restore_profile(host: str, profile: dict[str, Any]) -> Path:
    """
    Persist a deface restore profile inside the target cache document.
    """
    data = cache.load_target(host)
    if data is None:
        data = {
            "target": {},
            "onvif": {},
            "rtsp": {},
        }

    onvif = data.setdefault("onvif", {})
    onvif["deface_restore"] = profile
    cache.save_target(host, data)
    return cache.get_target_cache_path(host)


def load_restore_profile(host: str) -> dict[str, Any] | None:
    """
    Load a previously saved deface restore profile for the target.
    """
    data = cache.load_target(host)
    if data is None:
        return None

    onvif = data.get("onvif") or {}
    profile = onvif.get("deface_restore")
    if not isinstance(profile, dict):
        return None

    return profile


def clear_restore_profile(host: str) -> None:
    """
    Remove a previously saved deface restore profile from the target cache.
    """
    data = cache.load_target(host)
    if data is None:
        return

    onvif = data.get("onvif") or {}
    if "deface_restore" not in onvif:
        return

    onvif.pop("deface_restore", None)
    cache.save_target(host, data)


def get_restore_profile_path(host: str) -> Path:
    """
    Return the cache path containing the target deface restore profile.
    """
    return cache.get_target_cache_path(host)
