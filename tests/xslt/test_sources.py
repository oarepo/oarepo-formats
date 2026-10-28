# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Downloaded stylesheets: a broken network or upstream release must never break imports."""

from __future__ import annotations

import hashlib

import pytest
import requests

from oarepo_formats.xslt import sources
from oarepo_formats.xslt.sources import StylesheetError, clear_memory_cache, load_stylesheet
from tests.xslt.conftest import xsl

URL = "https://example.org/xslt/v1.xslt"
V1 = xsl('<xsl:template match="/"><v1/></xsl:template>')
V2 = xsl('<xsl:template match="/"><v2/></xsl:template>')
BROKEN = xsl('<xsl:template match="/"><xsl:value-of select="1 +"/></xsl:template>')


class FakeResponse:
    """Just enough of requests.Response for sources.py."""

    def __init__(self, url, status_code=200, text="", headers=None):
        """Fake."""
        self.url = url
        self.status_code = status_code
        self.content = text.encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        """Fake."""
        return self

    def __exit__(self, *args):
        """Fake."""

    def raise_for_status(self):
        """Fake."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, size):
        """Fake."""
        for i in range(0, len(self.content), size):
            yield self.content[i : i + size]


class FakeServer:
    """Replaces requests.get; ``respond`` is the next response (or exception)."""

    def __init__(self):
        """Fake."""
        self.requests: list[tuple[str, dict]] = []
        self.respond = None

    def get(self, url, headers=None, **kwargs):
        """Fake."""
        self.requests.append((url, headers or {}))
        if isinstance(self.respond, Exception):
            raise self.respond
        return self.respond(url) if callable(self.respond) else self.respond


@pytest.fixture
def server(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(sources.requests, "get", server.get)
    return server


def expire(app, monkeypatch):
    """Make the memory copy and the shared cache entry due for a refresh."""
    clear_memory_cache()
    monkeypatch.setitem(app.config, "OAREPO_XSLT_REFRESH_INTERVAL", -1)


def test_download_is_cached(server):
    server.respond = FakeResponse(URL, text=V1, headers={"ETag": '"1"'})
    stylesheet = load_stylesheet(URL)
    assert stylesheet.text == V1
    assert stylesheet.sha256 == hashlib.sha256(V1.encode()).hexdigest()
    # memory cache, then (another process) the shared cache: no new download
    assert load_stylesheet(URL) == stylesheet
    clear_memory_cache()
    assert load_stylesheet(URL).text == V1
    assert len(server.requests) == 1


def test_not_modified_keeps_cached_copy(app, monkeypatch, server):
    """Upstream answers a refresh with 304; `invenio oarepo xslt refresh` (force_refresh) downloads anyway."""
    server.respond = FakeResponse(URL, text=V1, headers={"ETag": '"1"'})
    load_stylesheet(URL)
    expire(app, monkeypatch)
    server.respond = FakeResponse(URL, status_code=304)
    assert load_stylesheet(URL).text == V1
    server.respond = FakeResponse(URL, text=V2)
    assert load_stylesheet(URL, force_refresh=True).text == V2


def test_refresh_picks_up_new_version(app, monkeypatch, server):
    server.respond = FakeResponse(URL, text=V1)
    load_stylesheet(URL)
    expire(app, monkeypatch)
    server.respond = FakeResponse(URL, text=V2)
    assert load_stylesheet(URL).text == V2


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("dns"),
        FakeResponse(URL, status_code=503),
        FakeResponse(URL, text=BROKEN),
    ],
    ids=["network", "5xx", "does-not-compile"],
)
def test_failed_refresh_keeps_cached_copy(app, monkeypatch, server, failure):
    server.respond = FakeResponse(URL, text=V1)
    load_stylesheet(URL)
    expire(app, monkeypatch)
    server.respond = failure
    assert load_stylesheet(URL).text == V1


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("dns"), FakeResponse(URL, text=BROKEN)],
    ids=["network", "does-not-compile"],
)
def test_first_download_failure_is_an_error(server, failure):
    server.respond = failure
    with pytest.raises(StylesheetError):
        load_stylesheet(URL)


def test_sha256_pin(server):
    server.respond = FakeResponse(URL, text=V1)
    pinned = f"{URL}#sha256={hashlib.sha256(V1.encode()).hexdigest()}"
    assert load_stylesheet(pinned).text == V1
    with pytest.raises(StylesheetError, match="does not match the pinned sha256"):
        load_stylesheet(f"{URL}#sha256={'0' * 64}")


def test_allow_list(app, monkeypatch, server):
    server.respond = FakeResponse(URL, text=V1)
    monkeypatch.setitem(app.config, "OAREPO_XSLT_ALLOWED_URL_PREFIXES", ["https://trusted.org/"])
    with pytest.raises(StylesheetError, match="not in OAREPO_XSLT_ALLOWED_URL_PREFIXES"):
        load_stylesheet(URL)
    assert server.requests == []


def test_redirect_to_http_is_refused(server):
    server.respond = FakeResponse("http://example.org/xslt/v1.xslt", text=V1)
    with pytest.raises(StylesheetError, match="Only https"):
        load_stylesheet(URL)


@pytest.mark.parametrize("headers", [{"Content-Length": "100"}, {}], ids=["content-length", "streamed"])
def test_oversized_download_is_refused(app, monkeypatch, server, headers):
    monkeypatch.setitem(app.config, "OAREPO_XSLT_MAX_SIZE", 50)
    server.respond = FakeResponse(URL, text=V1, headers=headers)
    with pytest.raises(StylesheetError, match="larger than 50 bytes"):
        load_stylesheet(URL)
