# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of reading an attached package: its shape, its member list, its manifests, and remote reads.

These are the hostile-input rules of the reader, checked without an application database. Writing the
members into a record (digests, retries, the mails) is tested through the REST API in
`tests/api_tests/test_rocrate_import.py`.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO

import pytest
import responses
from flask import Flask

from oarepo_formats.rocrate.imports.archive import (
    ArchiveEntry,
    ArchiveError,
    ArchiveReader,
    ArchiveTooLargeError,
    validate_entries,
)
from oarepo_formats.rocrate.imports.crate import ROCrate
from tests.rocrate.documents import root_of

from .packages import DESCRIPTOR, RangeServer, attached_package, bag_package, big_package, zip_of


@pytest.fixture
def app():
    """Return a bare application: the reader reads its limits from the config."""
    application = Flask("test")
    application.config.update(
        RECORDS_RESOURCES_ZIP_MAX_ENTRIES=100,
        RECORDS_RESOURCES_ZIP_MAX_HEADER_SIZE=64 * 1024,
        RECORDS_RESOURCES_ZIP_MAX_RATIO=200.0,
        OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE=1024 * 1024,
        OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT=5,
        RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS=["data.example.org:8443"],
    )
    with application.app_context():
        yield application


def read_body(content: bytes) -> ArchiveReader:
    """Return a reader over a package posted as the request body."""
    return ArchiveReader.from_body(BytesIO(content))


@pytest.mark.parametrize(
    ("content", "kind", "root", "crate_root"),
    [
        pytest.param(attached_package(), "crate", "", "", id="plain-crate"),
        pytest.param(bag_package(), "bag", "abc-123/", "abc-123/data/", id="bag"),
        pytest.param(bag_package(level="aip"), "bag", "abc-123/", "abc-123/data/", id="aip-bag"),
        pytest.param(
            attached_package(root="crate/", extra={"__MACOSX/._x": b"", "crate/.DS_Store": b""}),
            "crate",
            "crate/",
            "crate/",
            id="re-zipped-around-a-directory",
        ),
    ],
)
def test_the_package_shape_is_recognised(app, content, kind, root, crate_root):
    with read_body(content) as reader:
        package = reader.package

    assert (package.kind, package.root, package.crate_root) == (kind, root, crate_root)
    assert package.descriptor == f"{crate_root}{DESCRIPTOR}"


@pytest.mark.parametrize(
    ("content", "error", "match"),
    [
        pytest.param(zip_of({"data/a.csv": b"x"}), ArchiveError, "no RO-Crate metadata", id="no-descriptor"),
        pytest.param(
            zip_of({"bag/bagit.txt": b"BagIt-Version: 1.0\n", "bag/data/a.csv": b"x"}),
            ArchiveError,
            "has no crate metadata",
            id="bag-without-a-crate",
        ),
        pytest.param(b"not a zip at all", ArchiveError, "not a ZIP", id="not-a-zip"),
        pytest.param(
            attached_package(files={"data.bin": b"x" * (1024 * 1024 + 10)}), ArchiveTooLargeError, None, id="too-large"
        ),
    ],
)
def test_an_unusable_package_is_refused(app, content, error, match):
    with pytest.raises(error, match=match), read_body(content) as reader:
        _ = reader.package


def entry(path: str, *, size: int = 10, compressed: int = 10, encrypted: bool = False) -> ArchiveEntry:
    """Return one member of a table of contents."""
    return ArchiveEntry(
        path=path, size=size, compressed_size=compressed, compress_type=zipfile.ZIP_DEFLATED, encrypted=encrypted
    )


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        # `zipfile` would silently return the last member of that name
        pytest.param([entry("a.csv"), entry("a.csv")], "two members", id="duplicate"),
        *(
            pytest.param([entry(path)], "not a safe relative path", id=f"unsafe-{path or 'empty'}")
            for path in ("/absolute.csv", "../up.csv", "a/../b.csv", "")
        ),
        pytest.param([entry("a.csv", encrypted=True)], "encrypted", id="encrypted"),
        pytest.param([entry(f"{index}.csv") for index in range(101)], "at most", id="too-many"),
        pytest.param([entry("a.csv", size=1_000_000, compressed=10)], "compression ratio", id="bomb"),
    ],
)
def test_an_unsafe_member_list_is_refused(app, entries, match):
    with pytest.raises(ArchiveError, match=match):
        validate_entries(entries)


def test_bag_manifests_are_rfc8493_decoded(app):
    """A manifest path escapes only `%0D`, `%0A` and `%25`; a space stays a space."""
    with read_body(bag_package(files={"a b%c.csv": b"x"})) as reader:
        manifests = reader.manifests()

    assert manifests["sha512"]["abc-123/data/data/a b%c.csv"] == hashlib.sha512(b"x").hexdigest()
    assert manifests["md5"]["abc-123/data/data/a b%c.csv"] == hashlib.md5(b"x", usedforsecurity=False).hexdigest()


def test_an_attached_crate_names_its_files_by_member_path_and_skips_metadata_members(app):
    """`@id` is the member path; the export's own included exports (`subjectOf`) are not record files."""
    content = attached_package({"a b.csv": b"x"}, members={"metadata/record.json": b"{}"})

    with read_body(content) as reader:
        crate = ROCrate(json.loads(reader.read(reader.package.descriptor)), attached=True)

    assert [(file.key, file.path, file.url) for file in crate.files] == [("a b.csv", "data/a b.csv", None)]


#
# a package behind a URL
#
REDIRECTED = "https://data.example.org:8443/objects/crate.zip"


def direct(server: RangeServer) -> None:
    """Serve the archive at its URL."""
    server.register()


def redirected(location: str):
    """Serve the archive at `REDIRECTED`, behind a 302 from its URL (`location` absolute or relative)."""

    def setup(server: RangeServer) -> None:
        responses.add(responses.HEAD, RangeServer.url, status=302, headers={"Location": location})
        server.register(url=REDIRECTED)

    return setup


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(direct, id="direct"),
        pytest.param(redirected(REDIRECTED), id="absolute-redirect"),
        pytest.param(redirected("/objects/crate.zip"), id="relative-redirect"),
    ],
)
@responses.activate
def test_a_remote_package_is_read_by_ranges(app, setup):
    server = RangeServer(big_package())
    setup(server)

    with ArchiveReader.from_url(RangeServer.url) as reader:
        descriptor = reader.read(reader.package.descriptor)
        after_metadata = server.served
        member = reader.read(f"{reader.package.payload_prefix}data.csv")

    assert root_of(json.loads(descriptor))["name"] == "A dataset"
    # the decision is made from the tail (65 KB) and the descriptor alone: the payload is not touched
    assert after_metadata < 70 * 1024
    assert len(member) == 200 * 1024
    # nothing is downloaded twice
    assert server.served < len(server.content) + len(member)


def no_ranges(server: RangeServer) -> str:
    server.accept_ranges = False
    server.register()
    return RangeServer.url


def not_allowed(server: RangeServer) -> str:
    return "https://evil.example.org/archive.zip"


def redirect_out(server: RangeServer) -> str:
    responses.add(responses.HEAD, RangeServer.url, status=302, headers={"Location": "https://evil.example.org/a.zip"})
    return RangeServer.url


def missing(server: RangeServer) -> str:
    responses.add(responses.HEAD, RangeServer.url, status=404)
    return RangeServer.url


@pytest.mark.parametrize(
    ("setup", "etag", "match"),
    [
        pytest.param(no_ranges, None, "Accept-Ranges", id="no-range-support"),
        pytest.param(not_allowed, None, "is not allowed", id="host-not-allowed"),
        pytest.param(redirect_out, None, "is not allowed", id="redirect-out-of-the-allow-list"),
        pytest.param(missing, None, "HTTP 404", id="missing"),
        # the task pins the ETag the request saw: a replaced object must not be written instead
        pytest.param(direct, '"other"', "has changed", id="changed-since-the-plan"),
    ],
)
@responses.activate
def test_a_remote_package_is_refused(app, setup, etag, match):
    server = RangeServer(big_package())
    url = setup(server) or RangeServer.url

    with pytest.raises(ArchiveError, match=match):
        ArchiveReader.from_url(url, etag=etag)


@responses.activate
def test_an_archive_that_changes_between_reads_is_refused(app):
    """`If-Range` makes the host answer 412 instead of bytes from two versions of the archive."""
    server = RangeServer(big_package()).register()

    with ArchiveReader.from_url(server.url) as reader:
        server.etag = '"v2"'
        with pytest.raises(ArchiveError, match="could not be read"):
            reader.read(f"{reader.package.payload_prefix}data.csv")


def test_a_weak_etag_is_not_sent_as_if_range(app):
    """`If-Range` takes only a strong validator; a weak one would make every range answer 200."""
    server = RangeServer(attached_package(), etag='W/"v1"')
    sent = []

    def get(request):
        sent.append(request.headers.get("If-Range"))
        return server.get(request)

    with responses.RequestsMock() as mocked:
        mocked.add_callback(responses.HEAD, server.url, callback=server.head)
        mocked.add_callback(responses.GET, server.url, callback=get)
        with ArchiveReader.from_url(server.url) as reader:
            reader.read(reader.package.descriptor)

    assert sent
    assert set(sent) == {None}
