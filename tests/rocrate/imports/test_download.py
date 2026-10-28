# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of opening a referenced file's URL: what is refused, what is retried, what is read.

Against a fake host (`responses`) and a bare application; the downloads within an import are tested in
`tests/api_tests/test_rocrate_import.py`.
"""

from __future__ import annotations

import gzip
from types import SimpleNamespace

import pytest
import requests
import responses
from flask import Flask
from urllib3.exceptions import ProtocolError

from oarepo_formats.rocrate.imports.archive import ArchiveError, DownloadStream, PlannedFile, _open_download

HOST = "files.example.org"
URL = f"https://{HOST}/data.csv"
CONTENT = b"a,b\n1,2\n" * 100


@pytest.fixture
def app():
    """Return a bare application with the download's limits."""
    application = Flask("test")
    application.config.update(
        OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT=5,
        OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE=10 * 1024,
        RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS=[HOST],
    )
    with application.app_context():
        yield application


@pytest.fixture
def host():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        yield mocked


def download(size: int | None = len(CONTENT)) -> bytes:
    with _open_download(PlannedFile(key="data.csv", url=URL, size=size)) as stream:
        return stream.read()


def test_a_file_is_read(app, host):
    host.add(responses.GET, URL, body=CONTENT)

    assert download() == CONTENT
    assert host.calls[0].request.headers["Accept-Encoding"] == "identity"  # the bytes, not their gzip


def test_a_compressed_body_is_decoded(app, host):
    """A server that compresses although asked not to: the file is stored, not its gzip."""
    compressed = gzip.compress(CONTENT)
    host.add(
        responses.GET,
        URL,
        body=compressed,
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
        auto_calculate_content_length=False,
    )

    assert download() == CONTENT


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_a_status_a_later_attempt_may_not_get_is_retried(app, host, status):
    host.add(responses.GET, URL, status=status)

    with pytest.raises(requests.RequestException, match=f"HTTP {status}"):
        download()


def test_another_http_error_fails_the_file(app, host):
    host.add(responses.GET, URL, status=404, body=b"<html>" + b"x" * 10_000)

    with pytest.raises(ArchiveError, match="The file could not be read: HTTP 404") as error:
        download()
    assert "<html" not in str(error.value)  # one line, not the response body


@pytest.mark.parametrize(
    ("headers", "match"),
    [
        pytest.param({}, "redirects without a Location", id="no-location"),
        pytest.param({"Location": "https://elsewhere.example.org/x"}, "not allowed", id="to-a-refused-host"),
    ],
)
def test_a_redirect_is_checked(app, host, headers, match):
    host.add(responses.GET, URL, status=302, headers=headers)

    with pytest.raises(ArchiveError, match=match):
        download()


def test_a_relative_redirect_is_followed(app, host):
    host.add(responses.GET, URL, status=302, headers={"Location": "/stored/data.csv"})
    host.add(responses.GET, f"https://{HOST}/stored/data.csv", body=CONTENT)

    assert download() == CONTENT


@pytest.mark.parametrize(
    ("size", "body", "match"),
    [
        pytest.param(
            20 * 1024, b"x" * 20 * 1024, "more than OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE", id="stated-above-the-cap"
        ),
        pytest.param(5, CONTENT, "but the crate states 5", id="server-and-crate-disagree"),
    ],
)
def test_a_size_is_refused_before_the_body_is_read(app, host, size, body, match):
    host.add(responses.GET, URL, body=body, auto_calculate_content_length=True)

    with pytest.raises(ArchiveError, match=match):
        download(size)


def test_a_body_of_unknown_size_is_cut_at_the_cap(app, host):
    """Neither the crate nor the server states a size: the cap is the only limit."""
    host.add(responses.GET, URL, body=b"x" * (11 * 1024), auto_calculate_content_length=False)

    with pytest.raises(ArchiveError, match="larger than OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE"):
        download(None)


def test_a_connection_broken_while_reading_is_a_network_error():
    """urllib3's errors are neither `requests` errors nor `OSError`s: they would not be retried."""

    class BrokenRaw:
        def readinto(self, buffer):
            raise ProtocolError("Connection broken: IncompleteRead(10 bytes read, 90 more expected)")

    response = SimpleNamespace(raw=BrokenRaw(), close=lambda: None)
    stream = DownloadStream(response, 100, "too long")  # ty: ignore[invalid-argument-type] a stub response

    with pytest.raises(requests.ConnectionError, match="interrupted"):
        stream.read(10)
