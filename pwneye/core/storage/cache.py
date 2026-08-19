from datetime import datetime, timezone
from pathlib import Path
import os
import re
import tempfile
import yaml

from pwneye.config import CACHE_DIR


def _utc_now() -> str:
    """
    Return the current UTC time in ISO 8601 format.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cache_path(host: str) -> Path:
    """
    Build the cache file path for a target host.
    """
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return CACHE_DIR / f"{safe_host}.yaml"


def get_target_cache_path(host: str) -> Path:
    """
    Return the cache file path used for the given target host.
    """
    return _cache_path(host)


def _empty_document(host: str) -> dict:
    """
    Create a new cache document for the given target.
    """
    now = _utc_now()
    return {
        "target": {
            "host": host,
            "first_seen": now,
            "last_seen": now,
        },
        "onvif": {},
        "rtsp": {},
    }


def load_target(host: str) -> dict | None:
    """
    Load a target cache entry if it exists.
    """
    path = _cache_path(host)
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        return None

    target = data.setdefault("target", {})
    target.setdefault("host", host)
    target.setdefault("first_seen", _utc_now())
    target["last_seen"] = _utc_now()

    data.setdefault("onvif", {})
    data.setdefault("rtsp", {})
    return data


def save_target(host: str, data: dict) -> None:
    """
    Save a target cache entry to disk.
    """
    path = _cache_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)

    target = data.setdefault("target", {})
    target.setdefault("host", host)
    target.setdefault("first_seen", _utc_now())
    target["last_seen"] = _utc_now()

    data.setdefault("onvif", {})
    data.setdefault("rtsp", {})

    # Write to a temporary file in the same directory and atomically replace the
    # target file. An interrupt or crash mid-write can then never corrupt the
    # cache: a reader always sees either the previous or the new complete
    # document, never a half-written one. The temp file inherits mkstemp's 0600
    # permissions, which also keeps cached credentials owner-only.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                data,
                handle,
                sort_keys=False,
                allow_unicode=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def count_entries() -> int:
    """
    Return the number of cache entry files currently stored on disk.
    """
    if not CACHE_DIR.exists():
        return 0

    return sum(1 for path in CACHE_DIR.glob("*.yaml") if path.is_file())


def clear_all() -> int:
    """
    Remove all cache entry files and return the number of deleted entries.
    """
    if not CACHE_DIR.exists():
        return 0

    deleted = 0
    for path in CACHE_DIR.glob("*.yaml"):
        if not path.is_file():
            continue

        path.unlink(missing_ok=True)
        deleted += 1

    return deleted


def upsert_onvif_success(
    host: str,
    *,
    port: int,
    username: str,
    password: str,
    manufacturer: str | None = None,
    streams: list[str] | None = None,
) -> None:
    """
    Persist a successful ONVIF authentication for the target.
    """
    data = load_target(host) or _empty_document(host)
    onvif = data.setdefault("onvif", {})

    onvif.update({
        "supported": True,
        "port": port,
        "auth": {
            "username": username,
            "password": password,
        },
    })

    if manufacturer:
        onvif["manufacturer"] = manufacturer

    if streams:
        onvif["streams"] = list(dict.fromkeys(streams))

    save_target(host, data)


def upsert_onvif_discovery(
    host: str,
    *,
    manufacturer: str | None = None,
) -> None:
    """
    Persist non-authenticated ONVIF discovery data for the target.
    """
    if not manufacturer:
        return

    data = load_target(host) or _empty_document(host)
    onvif = data.setdefault("onvif", {})
    onvif["manufacturer"] = manufacturer
    save_target(host, data)


def upsert_rtsp_banner(
    host: str,
    *,
    port: int,
    banner: str,
) -> None:
    """
    Persist an RTSP banner for the target.
    """
    if not banner:
        return

    data = load_target(host) or _empty_document(host)
    rtsp = data.setdefault("rtsp", {})
    rtsp["banner"] = {
        "port": port,
        "value": banner,
    }
    save_target(host, data)


def upsert_rtsp_success(
    host: str,
    *,
    port: int,
    username: str,
    password: str,
    path: str,
    protocol: str,
    url: str,
) -> None:
    """
    Persist a successful RTSP authentication for the target.
    """
    data = load_target(host) or _empty_document(host)
    rtsp = data.setdefault("rtsp", {})

    rtsp.update({
        "supported": True,
        "port": port,
        "auth": {
            "username": username,
            "password": password,
        },
        "path": path,
        "protocol": protocol,
        "url": url,
    })

    save_target(host, data)


def upsert_rtsp_channels(
    host: str,
    *,
    channels: list[dict],
) -> None:
    """
    Persist valid RTSP channels discovered for the target.
    """
    if not channels:
        return

    data = load_target(host) or _empty_document(host)
    rtsp = data.setdefault("rtsp", {})

    normalized = []
    seen = set()

    for channel in channels:
        channel_id = channel.get("channel")
        url = channel.get("url")

        if channel_id is None or not url or channel_id in seen:
            continue

        normalized.append({
            "channel": channel_id,
            "port": channel.get("port"),
            "path": channel.get("path"),
            "protocol": channel.get("protocol"),
            "url": url,
        })
        seen.add(channel_id)

    if normalized:
        rtsp["channels"] = normalized

    save_target(host, data)


def get_cached_onvif_auth(data: dict | None) -> dict | None:
    """
    Return cached ONVIF authentication details, if available.
    """
    if not data:
        return None

    onvif = data.get("onvif") or {}
    auth = onvif.get("auth") or {}

    username = auth.get("username")
    password = auth.get("password")
    port = onvif.get("port")

    if port is None or username is None or password is None:
        return None

    return {
        "port": port,
        "username": username,
        "password": password,
        "manufacturer": onvif.get("manufacturer"),
        "streams": onvif.get("streams", []),
    }


def get_cached_onvif_manufacturer(data: dict | None) -> str | None:
    """
    Return a cached ONVIF manufacturer hint, if available.
    """
    if not data:
        return None

    onvif = data.get("onvif") or {}
    manufacturer = onvif.get("manufacturer")
    if not manufacturer:
        return None

    return str(manufacturer)


def get_cached_rtsp_banner(data: dict | None) -> dict | None:
    """
    Return a cached RTSP banner, if available.
    """
    if not data:
        return None

    rtsp = data.get("rtsp") or {}
    banner = rtsp.get("banner") or {}
    port = banner.get("port")
    value = banner.get("value")
    if port is None or not value:
        return None

    return {
        "port": port,
        "value": str(value),
    }


def get_cached_rtsp_auth(data: dict | None) -> dict | None:
    """
    Return cached RTSP authentication details, if available.
    """
    if not data:
        return None

    rtsp = data.get("rtsp") or {}
    auth = rtsp.get("auth") or {}

    username = auth.get("username")
    password = auth.get("password")
    port = rtsp.get("port")
    path = rtsp.get("path")
    protocol = rtsp.get("protocol")
    url = rtsp.get("url")

    if None in (port, path, protocol, url, username, password):
        return None

    return {
        "port": port,
        "path": path,
        "protocol": protocol,
        "url": url,
        "username": username,
        "password": password,
    }


def get_cached_rtsp_channels(data: dict | None) -> list[dict]:
    """
    Return cached RTSP channel details, if available.
    """
    if not data:
        return []

    rtsp = data.get("rtsp") or {}
    channels = rtsp.get("channels") or []
    if not isinstance(channels, list):
        return []

    normalized = []
    for channel in channels:
        if not isinstance(channel, dict):
            continue

        channel_id = channel.get("channel")
        url = channel.get("url")
        if channel_id is None or not url:
            continue

        normalized.append({
            "channel": channel_id,
            "port": channel.get("port"),
            "path": channel.get("path"),
            "protocol": channel.get("protocol"),
            "url": url,
        })

    return normalized
