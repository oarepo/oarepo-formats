# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Loading stylesheets from their locations.

A location is one of:

| Location | Example | |
|---|---|---|
| `module:path` | `ccmm_invenio.xslt:ccmm-2.0.0-to-1.1.0.xslt` | a file shipped inside a python package |
| `https:` | `https://example.org/v1/my.xslt#sha256=<hex>` | downloaded; `#sha256=` pins the content |

Downloaded stylesheets are stored in the shared flask-caching cache (`invenio_cache`) and refreshed
after `OAREPO_XSLT_REFRESH_INTERVAL` seconds with a conditional GET. A new version replaces the cached
one only if it compiles in the sandbox; network errors keep the cached copy. Each process additionally
keeps an in-memory copy for `OAREPO_XSLT_CHECK_INTERVAL` seconds.

URLs (also after redirects) must be `https:` and, if `OAREPO_XSLT_ALLOWED_URL_PREFIXES` is set, start
with one of its prefixes. Downloads are limited to `OAREPO_XSLT_MAX_SIZE` bytes and
`OAREPO_XSLT_DOWNLOAD_TIMEOUT` seconds.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any

import requests
from flask import current_app
from invenio_cache import current_cache

from ..proxies import current_formats
from .pool import SandboxError

if TYPE_CHECKING:
    from collections.abc import Mapping

log = logging.getLogger(__name__)

CACHE_PREFIX = "oarepo-xslt:"
LOCK_PREFIX = "oarepo-xslt-lock:"
LOCK_TIMEOUT = 30


class StylesheetError(Exception):
    """A stylesheet cannot be loaded or does not compile."""


@dataclass(frozen=True)
class Stylesheet:
    """Text of a stylesheet and where it came from."""

    location: str
    text: str
    sha256: str
    fetched_at: float | None = None
    """When the stylesheet was last downloaded or confirmed by the server (`https:` only)."""


# per-process memory cache: location -> (time of the check for https, stylesheet)
_memory: dict[str, tuple[float, Stylesheet]] = {}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_stylesheet(location: str, *, force_refresh: bool = False) -> Stylesheet:
    """Return the stylesheet stored at the location.

    Args:
        location: see the module documentation.
        force_refresh: download `https:` stylesheets even if the cached copy is fresh.

    Raises:
        StylesheetError: if the stylesheet cannot be loaded.

    """
    if location.startswith("https:"):
        return _load_https(location, force_refresh=force_refresh)
    module, sep, path = location.partition(":")
    if not sep or "/" in module or not path or path.startswith("//"):
        raise StylesheetError(f"Unsupported stylesheet location {location!r}, use module:path or https:")
    if location not in _memory:
        try:
            text = files(module).joinpath(path).read_text("utf-8")
        except (ImportError, OSError) as e:
            raise StylesheetError(f"Cannot read stylesheet {location}: {e}") from e
        _memory[location] = (0, Stylesheet(location, text, _sha256(text)))
    return _memory[location][1]


def check_stylesheet(text: str) -> None:
    """Compile the stylesheet in the sandbox.

    Raises:
        StylesheetError: if it does not compile.

    """
    try:
        response = current_formats.xslt_pool.call({"stylesheet": text, "xml": None})
    except SandboxError as e:
        raise StylesheetError(str(e)) from e
    if not response["ok"]:
        raise StylesheetError(response["error"])


def _load_https(location: str, *, force_refresh: bool) -> Stylesheet:
    config = current_app.config
    now = time.time()
    cached = _memory.get(location)
    if cached and not force_refresh and now - cached[0] < config["OAREPO_XSLT_CHECK_INTERVAL"]:
        return cached[1]

    url, _, fragment = location.partition("#")
    pin = fragment.removeprefix("sha256=").lower() if fragment else None
    key = CACHE_PREFIX + hashlib.sha256(location.encode("utf-8")).hexdigest()
    entry: dict[str, Any] | None = current_cache.get(key)

    if entry is None or force_refresh:
        # no copy to fall back to (or explicit refresh): errors propagate
        entry = _download(url, pin, None)
        current_cache.set(key, entry, timeout=0)
    elif now - entry["fetched_at"] > config["OAREPO_XSLT_REFRESH_INTERVAL"] and current_cache.add(
        LOCK_PREFIX + key, 1, timeout=LOCK_TIMEOUT
    ):
        try:
            entry = _download(url, pin, entry)
        except StylesheetError:
            # keep the old copy; the lock expires after LOCK_TIMEOUT and the refresh is retried
            log.warning("Cannot refresh stylesheet %s, using the cached copy", location, exc_info=True)
        else:
            current_cache.set(key, entry, timeout=0)
            current_cache.delete(LOCK_PREFIX + key)

    stylesheet = Stylesheet(location, entry["text"], entry["sha256"], entry["fetched_at"])
    _memory[location] = (now, stylesheet)
    return stylesheet


def _download(url: str, pin: str | None, entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """Download the stylesheet, conditionally if `entry` (the cached copy) is given.

    A broken new version (wrong pin, does not compile) keeps `entry` with a bumped
    `fetched_at`, so it is not downloaded again before the next refresh interval.
    """
    config = current_app.config
    _check_url(url)
    headers = {}
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    if entry and entry.get("last_modified"):
        headers["If-Modified-Since"] = entry["last_modified"]
    try:
        with requests.get(url, headers=headers, timeout=config["OAREPO_XSLT_DOWNLOAD_TIMEOUT"], stream=True) as resp:
            _check_url(resp.url)  # after redirects
            if resp.status_code == 304 and entry:  # noqa: PLR2004
                return {**entry, "fetched_at": time.time()}
            resp.raise_for_status()
            data = _read_limited(resp, config["OAREPO_XSLT_MAX_SIZE"])
    except requests.RequestException as e:
        raise StylesheetError(f"Cannot download stylesheet {url}: {e}") from e

    try:
        text = data.decode("utf-8")
        if pin and _sha256(text) != pin:
            raise StylesheetError(f"Stylesheet {url} does not match the pinned sha256 {pin}")
        check_stylesheet(text)
    except (UnicodeDecodeError, StylesheetError) as e:
        if entry is None:
            raise StylesheetError(f"Invalid stylesheet {url}: {e}") from e
        log.error("New version of stylesheet %s is invalid, keeping the cached copy: %s", url, e)  # noqa: TRY400
        return {**entry, "fetched_at": time.time()}
    return {
        "text": text,
        "sha256": _sha256(text),
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "fetched_at": time.time(),
    }


def _check_url(url: str) -> None:
    if not url.startswith("https://"):
        raise StylesheetError(f"Only https: stylesheet URLs are allowed, got {url}")
    prefixes = current_app.config["OAREPO_XSLT_ALLOWED_URL_PREFIXES"]
    if prefixes is not None and not any(url.startswith(prefix) for prefix in prefixes):
        raise StylesheetError(f"Stylesheet URL {url} is not in OAREPO_XSLT_ALLOWED_URL_PREFIXES")


def _read_limited(resp: requests.Response, max_size: int) -> bytes:
    if int(resp.headers.get("Content-Length") or 0) > max_size:
        raise StylesheetError(f"Stylesheet {resp.url} is larger than {max_size} bytes")
    chunks, size = [], 0
    for chunk in resp.iter_content(64 * 1024):
        size += len(chunk)
        if size > max_size:
            raise StylesheetError(f"Stylesheet {resp.url} is larger than {max_size} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def clear_memory_cache() -> None:
    """Forget the per-process copies (the shared cache is kept)."""
    _memory.clear()
