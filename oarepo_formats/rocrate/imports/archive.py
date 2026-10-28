# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Reading an attached package: a ZIP in the request body, or a ZIP behind a URL.

An *attached* package is a ZIP whose metadata (`ro-crate-metadata.json`) sits next to the data: the plain
`rocrate-zip` export, or a BagIt bag (`rocrate-bagit`) with a `<id>/` directory, `bagit.txt` and manifests.
The reader gives the import three things, and nothing else:

- the **member list** (the ZIP's table of contents), so that the plan can be checked before anything is
  created - and, for a package behind a URL, without downloading it;
- the **members it needs as bytes** (the metadata descriptor, `record.json`, the bag manifests);
- a **stream per member** of the payload, so the files can be written one at a time.

## The two transports

| Transport | Source | Random access |
|---|---|---|
| body (`application/zip`) | the request stream, spooled to a temporary file | the spooled file is seekable |
| URL (`application/json` `{"url": ...}`) | a static object served with `Accept-Ranges: bytes` |
`RangeStream` over HTTP ranges, with the tail preloaded |

A ZIP needs random access (the central directory is at the end, each member at its own offset), so a
remote archive is read through HTTP `Range` requests: the tail first (`ReplyStream` from
invenio-records-resources caches it in memory, exactly as it does for an uploaded ZIP), then each member
when it is written. The archive itself is never downloaded as a whole.

A server that does not honour `Range` is refused: falling back to a plain download would happen inside the
request, which is what the URL transport exists to avoid. The same goes for this package's own export
URLs - they are streamed responses whose bytes differ on every request; they belong in the body transport.

## What is refused, and why

The checks mirror the platform's ZIP preview (`RECORDS_RESOURCES_ZIP_MAX_ENTRIES`, `_MAX_RATIO`) plus the
three it does not make: duplicate member names, encrypted entries, and an absolute or `..` path. Nothing is
written to disk, so a hostile name can not escape anything; refusing it keeps the plan honest.

`BagIt` manifests are verified by the importer, not here: this module only *decodes* them (RFC 8493
percent-encoding, which is not `urllib.parse.unquote`).
"""

from __future__ import annotations

import hashlib
import http.client
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from functools import cached_property
from io import RawIOBase
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urljoin, urlparse

import requests
from flask import current_app
from invenio_records_resources.services.files.extractors.reply_stream import ReplyStream
from invenio_records_resources.services.files.transfer.base import TransferStatus
from marshmallow import ValidationError
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from ..exports.zip import METADATA_FILE

if TYPE_CHECKING:
    from .crate import ROCrate

ARCHIVE_MEDIA_TYPES = ("application/zip", "application/vnd.oarepo.aip+zip")
"""Media types of an attached package in the request body (an AIP is imported like any other bag)."""

TAIL_BYTES = 22 + 65535
"""Bytes read from the end of a remote archive: the end-of-central-directory record, its ZIP64 locator, and
the comment space the format allows. The central directory itself is read next if it does not fit."""

CHUNK_SIZE = 1024 * 1024

IGNORED_NAMES = (".DS_Store",)
IGNORED_PREFIXES = ("__MACOSX/",)
"""Artefacts a re-zipped package (macOS Finder) may carry: never part of the package."""

BAGIT_FILE = "bagit.txt"
BAG_INFO_FILE = "bag-info.txt"
MANIFESTS = ("manifest-sha512.txt", "manifest-md5.txt")
PAYLOAD_DIR = "data/"

HTTP_OK = 200
HTTP_PARTIAL_CONTENT = 206
MAX_REDIRECTS = 3

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

RETRIABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
"""HTTP statuses of a download that a later attempt may not get: retried like a network error, not failed."""
"""Limits of the remote reads: a `HEAD` or a download follows at most three redirects, each re-checked."""


class ArchiveError(ValidationError):
    """A package can not be read: a client error, reported as a 400 with the message."""


class ArchiveTooLargeError(ArchiveError):
    """The package in the body is larger than `OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE` (reported as a 413)."""


@dataclass(frozen=True)
class ArchiveEntry:
    """One member of the archive, as the table of contents describes it."""

    path: str
    """The member's name, normalized to `/` separators."""

    size: int
    """Uncompressed size, as the central directory states it."""

    compressed_size: int
    """Compressed size, as the central directory states it."""

    compress_type: int
    """`zipfile.ZIP_STORED`, `ZIP_DEFLATED`, ..."""

    encrypted: bool = False
    """Whether the member is encrypted (no password support)."""


@dataclass(frozen=True)
class Package:
    """What the archive is: a plain attached crate, or a BagIt bag carrying one."""

    kind: str
    """`"crate"` or `"bag"`."""

    root: str
    """The package's directory in the archive: `""` or `"<dir>/"` (a bag's `<id>/`)."""

    crate_root: str
    """The crate's directory: `root` for a crate, `root + "data/"` for a bag."""

    descriptor: str
    """Path of the RO-Crate metadata descriptor."""

    bag_info: dict[str, str] = field(default_factory=dict)
    """`bag-info.txt`, if the package is a bag."""

    @property
    def payload_prefix(self) -> str:
        """Path prefix of the record's files in the archive."""
        return f"{self.crate_root}{PAYLOAD_DIR}"


class RangeStream(RawIOBase):
    """A seekable, read-only stream over HTTP `Range` requests.

    `seek`/`tell`/`readinto` are all a `zipfile.ZipFile` needs; every `readinto` is one request for
    exactly the bytes asked for, so the archive is never fetched as a whole. `If-Range` pins the version:
    if the object changes between two reads, the server answers 200 or 412 instead of handing out a mix of
    two archives, and the read fails.
    """

    def __init__(
        self,
        url: str,
        size: int,
        *,
        etag: str | None = None,
        timeout: float = 30,
        session: requests.Session | None = None,
    ):
        """Create the stream; no request is made until the first read.

        `session` keeps the connection alive between ranges: a member is read in many small requests, and a
        new TLS handshake per request would dominate the transfer.
        """
        super().__init__()
        self.url = url
        self.size = size
        self.etag = etag
        self.timeout = timeout
        self.session = session or requests.Session()
        self._position = 0

    def close(self) -> None:
        """Close the HTTP session with the stream."""
        self.session.close()
        super().close()

    def readable(self) -> bool:
        """Return whether the stream is readable."""
        return True

    def seekable(self) -> bool:
        """Return whether the stream is seekable (through ranges)."""
        return True

    def tell(self) -> int:
        """Return the current position."""
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        """Move to a position; `SEEK_END` is relative to the archive's size."""
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._position + offset
        elif whence == os.SEEK_END:
            position = self.size + offset
        else:
            raise ValueError(f"Invalid whence {whence!r}")
        if position < 0:
            raise ValueError("Negative seek position")
        self._position = position
        return position

    def readinto(self, buffer: Any) -> int:
        """Fill `buffer` from the current position with one range request."""
        if self._position >= self.size:
            return 0
        end = min(self._position + len(buffer), self.size) - 1
        headers = {"Range": f"bytes={self._position}-{end}"}
        if self.etag and not self.etag.startswith("W/"):  # If-Range takes only a strong validator (RFC 9110)
            headers["If-Range"] = self.etag
        with self.session.get(
            self.url, headers=headers, stream=True, allow_redirects=False, timeout=self.timeout
        ) as response:
            if response.status_code != HTTP_PARTIAL_CONTENT:
                raise ArchiveError(
                    f"The archive could not be read at bytes {self._position}-{end}: HTTP {response.status_code}"
                )
            data = _read_exactly(response, end - self._position + 1)
        buffer[: len(data)] = data
        self._position += len(data)
        return len(data)


def _read_exactly(response: requests.Response, size: int) -> bytes:
    """Read exactly `size` bytes from a streamed response body."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = response.raw.read(min(remaining, CHUNK_SIZE), decode_content=False)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != size:
        raise ArchiveError(f"The archive ended after {size - remaining} of {size} expected bytes")
    return data


class ArchiveReader:
    """The members of one package, and the streams to read them.

    Use [from_body][oarepo_formats.rocrate.imports.archive.ArchiveReader.from_body] or
    [from_url][oarepo_formats.rocrate.imports.archive.ArchiveReader.from_url]; close it (or use it as a
    context manager) when done.
    """

    def __init__(
        self,
        stream: Any,
        *,
        size: int | None = None,
        etag: str | None = None,
    ):
        """Wrap a seekable stream; `size`/`etag` are given for a remote archive."""
        self._stream = stream
        self._size = size
        self.etag = etag
        """The archive's ETag, when it was read from a URL: it pins the version between the two phases."""
        self._zip = self._open_zip()
        self.entries = self._read_entries()
        self._validate()

    #
    # the two transports
    #
    @classmethod
    def from_body(cls, stream: Any) -> ArchiveReader:
        """Spool the request body to a temporary file and read it.

        Raises:
            ArchiveTooLarge: if the body exceeds `OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE`.

        """
        limit = current_app.config["OAREPO_ROCRATE_IMPORT_MAX_ARCHIVE_SIZE"]
        spool = tempfile.SpooledTemporaryFile(max_size=min(limit, 8 * 1024 * 1024))  # noqa: SIM115 closed in close()
        total = 0
        while chunk := stream.read(CHUNK_SIZE):
            total += len(chunk)
            if total > limit:
                spool.close()
                raise ArchiveTooLargeError(f"The package is larger than {limit} bytes; post a smaller one as the body")
            spool.write(chunk)
        spool.seek(0)
        try:
            return cls(spool)
        except Exception:
            spool.close()
            raise

    @classmethod
    def from_url(cls, url: str, *, etag: str | None = None) -> ArchiveReader:
        """Read the table of contents of a ZIP behind a URL, then its members on demand.

        Args:
            url: an allow-listed http(s) URL of a static ZIP served with ranges.
            etag: the ETag a previous phase saw. If the object has changed since, the import stops: the
                plan (paths, sizes, digests) belongs to the archive that was planned.

        Raises:
            ArchiveError: if the URL is not allowed, does not serve a ZIP with ranges, is not a ZIP, or the
                archive is not the one that was planned.

        """
        url, response = _head(url)
        current_etag = response.headers.get("ETag")
        if etag is not None and current_etag is not None and current_etag != etag:
            raise ArchiveError("The archive has changed since the import was planned")
        size = response.headers.get("Content-Length")
        if not size or "bytes" not in response.headers.get("Accept-Ranges", "").lower():
            raise ArchiveError(
                "The archive's host must serve a static ZIP with 'Accept-Ranges: bytes': this package's own "
                "export links are streamed and change on every request, so they can not be read this way. "
                "Post the archive as the request body instead."
            )
        return cls(
            RangeStream(
                url,
                int(size),
                etag=current_etag,
                timeout=current_app.config["OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT"],
            ),
            size=int(size),
            etag=current_etag,
        )

    def close(self) -> None:
        """Close the archive and its source."""
        self._zip.close()
        self._stream.close()

    def __enter__(self) -> Self:
        """Enter the context."""
        return self

    def __exit__(self, *exc: object) -> bool:
        """Close the archive."""
        self.close()
        return False

    #
    # the zip and its members
    #
    def _open_zip(self) -> zipfile.ZipFile:
        """Open the ZIP, preloading the remote tail so `zipfile` reads the table of contents from memory."""
        if self._size is None:  # a spooled body: already seekable
            try:
                return zipfile.ZipFile(self._stream)
            except zipfile.BadZipFile as e:
                raise ArchiveError("The package is not a ZIP archive") from e
        tail_start = max(0, self._size - TAIL_BYTES)
        try:
            archive = zipfile.ZipFile(ReplyStream(self._stream, tail_start, self._size))
        except zipfile.BadZipFile as e:
            raise ArchiveError("The package is not a ZIP archive") from e
        start_dir = archive.start_dir
        if start_dir < tail_start:
            header_size = self._size - start_dir
            archive.close()
            limit = current_app.config["RECORDS_RESOURCES_ZIP_MAX_HEADER_SIZE"]
            if header_size > limit:
                raise ArchiveError(f"The archive's table of contents is larger than {limit} bytes")
            archive = zipfile.ZipFile(ReplyStream(self._stream, start_dir, self._size))
        return archive

    def _read_entries(self) -> tuple[ArchiveEntry, ...]:
        return tuple(
            ArchiveEntry(
                path=info.filename.replace("\\", "/"),
                size=info.file_size,
                compressed_size=info.compress_size,
                compress_type=info.compress_type,
                encrypted=bool(info.flag_bits & 0x1),
            )
            for info in self._zip.infolist()
            if not info.is_dir()
        )

    def _validate(self) -> None:
        """Refuse an archive the platform would not preview either, plus the checks it does not make."""
        validate_entries(self.entries)

    def read(self, path: str) -> bytes:
        """Return one member's content (for the small members the import inspects)."""
        try:
            with self._zip.open(path) as stream:
                return stream.read()
        except KeyError as e:
            raise ArchiveError(f"The archive has no member {path!r}") from e
        except zipfile.BadZipFile as e:
            raise ArchiveError(f"The archive's member {path!r} is corrupt: {e}") from e

    def open(self, path: str) -> Any:
        """Open one member for streaming (the caller closes it)."""
        try:
            return self._zip.open(path)
        except KeyError as e:
            raise ArchiveError(f"The archive has no member {path!r}") from e

    #
    # what the archive is
    #
    @cached_property
    def package(self) -> Package:
        """Return the package's shape: a plain attached crate, or a bag carrying one."""
        paths = [entry.path for entry in self.entries if not _ignored(entry.path)]
        root = _package_root(paths)
        if f"{root}{BAGIT_FILE}" in paths:
            crate_root = f"{root}{PAYLOAD_DIR}"
            descriptor = f"{crate_root}{METADATA_FILE}"
            if descriptor not in paths:
                raise ArchiveError(
                    f"The bag {root!r} has no crate metadata ({descriptor}); a bag is imported through its crate"
                )
            return Package(
                kind="bag",
                root=root,
                crate_root=crate_root,
                descriptor=descriptor,
                bag_info=_bag_info(self.read(f"{root}{BAG_INFO_FILE}") if f"{root}{BAG_INFO_FILE}" in paths else b""),
            )
        descriptor = f"{root}{METADATA_FILE}"
        if descriptor not in paths:
            raise ArchiveError(
                f"The package has no RO-Crate metadata ({descriptor}); it is neither an attached crate nor a bag"
            )
        return Package(kind="crate", root=root, crate_root=root, descriptor=descriptor)

    def manifests(self) -> dict[str, dict[str, str]]:
        """Return the bag manifests, `{"sha512": {archive path: hex}, "md5": {...}}`, RFC 8493 decoded.

        A member path in a manifest is relative to the bag root and percent-encoded per RFC 8493 (`%0D`,
        `%0A` and `%25` only) - not `urllib.parse.unquote`, which would also decode a space and break keys
        containing one.
        """
        package = self.package
        manifests: dict[str, dict[str, str]] = {}
        for name in MANIFESTS:
            path = f"{package.root}{name}"
            if not any(entry.path == path for entry in self.entries):
                continue
            algorithm = "sha512" if "sha512" in name else "md5"
            manifests[algorithm] = _parse_manifest(self.read(path), package.root)
        return manifests


def validate_entries(entries: tuple[ArchiveEntry, ...] | list[ArchiveEntry]) -> None:
    """Refuse the member list of a package that can not be imported safely.

    The rules are the platform's ZIP-preview ones (`RECORDS_RESOURCES_ZIP_MAX_ENTRIES` and
    `RECORDS_RESOURCES_ZIP_MAX_RATIO`) plus the three it does not make: an encrypted member, a duplicate
    name (`zipfile` would silently return the last one), and an absolute or `..` path.

    Raises:
        ArchiveError: on the first rule a member breaks.

    """
    max_entries = current_app.config["RECORDS_RESOURCES_ZIP_MAX_ENTRIES"]
    if len(entries) > max_entries:
        raise ArchiveError(f"The archive has {len(entries)} entries, at most {max_entries} are accepted")
    max_ratio = current_app.config["RECORDS_RESOURCES_ZIP_MAX_RATIO"]
    seen: set[str] = set()
    for entry in entries:
        if entry.encrypted:
            raise ArchiveError(f"The archive's member {entry.path!r} is encrypted")
        if _unsafe(entry.path):
            raise ArchiveError(f"The archive's member {entry.path!r} is not a safe relative path")
        if entry.path in seen:
            raise ArchiveError(f"The archive has two members named {entry.path!r}")
        seen.add(entry.path)
        if entry.compress_type == zipfile.ZIP_DEFLATED and entry.compressed_size:
            ratio = entry.size / entry.compressed_size
            if ratio > max_ratio:
                raise ArchiveError(f"The archive's member {entry.path!r} has a suspicious compression ratio")


def _head(url: str) -> tuple[str, requests.Response]:
    """Return the URL the archive ended up at and its `HEAD`, following at most 3 redirects itself.

    The final URL is what the ranges are read from: a repository link that redirects to object storage is
    the normal case, and a range request to the link itself would only receive the redirect.
    """
    url, response = _follow_redirects("HEAD", url, what="archive")
    if response.status_code != HTTP_OK:
        raise ArchiveError(f"The archive could not be read: HTTP {response.status_code}")
    return url, response


def _follow_redirects(method: str, url: str, *, what: str, **kwargs: Any) -> tuple[str, requests.Response]:
    """Request `url`, following at most `MAX_REDIRECTS` redirects itself, each target checked by `_check_url`.

    `requests` would follow them without asking: a redirect must not be a way around the allow-list. A
    relative `Location` is allowed (RFC 9110); a redirect without one is refused.

    Returns:
        The URL the request ended up at, and the (not a redirect) response - still open when streamed.

    Raises:
        ArchiveError: for a refused URL, a redirect without `Location`, or too many redirects.

    """
    timeout = current_app.config["OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT"]
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(url)
        response = requests.request(method, url, allow_redirects=False, timeout=timeout, **kwargs)
        if response.status_code in REDIRECT_STATUSES and not response.headers.get("Location"):
            response.close()
            raise ArchiveError(f"The {what}'s URL redirects without a Location")
        if not response.is_redirect:
            return url, response
        response.close()
        url = urljoin(url, response.headers["Location"])
    raise ArchiveError(f"The {what}'s URL redirects too many times")


def _check_url(url: str) -> None:
    """Refuse a URL that is not http(s) or whose host is not in `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ArchiveError("The URL must be an absolute http(s) URL")
    allowed = current_app.config.get("RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS") or []
    if parsed.netloc not in allowed:
        raise ArchiveError(
            f"The host {parsed.netloc!r} is not allowed; list it in "
            "RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS (netloc, port included)"
        )


class DownloadStream(RawIOBase):
    """The body of an HTTP response as a file, refusing to read more than `size` bytes.

    A stream that runs past the size the crate states is corrupt whatever its digest would say, and a
    server that does not state a length must not be able to fill the bucket: hence the cap (`too_long` is
    the reason given when it is reached).

    A connection that breaks while the body is read surfaces from urllib3 (`ProtocolError`,
    `ReadTimeoutError`, ...), which is neither a `requests` error nor an `OSError`; it is raised as
    `requests.ConnectionError` here, so that the task retries it like any other network error.
    """

    def __init__(self, response: requests.Response, size: int, too_long: str):
        """Wrap the response body, allowing at most `size` bytes."""
        super().__init__()
        self._response = response
        self._raw = response.raw
        self._remaining = size
        self._too_long = too_long

    def readable(self) -> bool:
        """Return whether the stream is readable."""
        return True

    def seekable(self) -> bool:
        """Return whether the stream is seekable (never)."""
        return False

    def readinto(self, buffer: Any) -> int:
        """Read into a buffer, up to the cap."""
        try:
            if self._remaining <= 0:  # at the cap: only an EOF is acceptable
                if not self._raw.read(1):
                    return 0
                raise ArchiveError(self._too_long)
            read = self._raw.readinto(memoryview(buffer)[: self._remaining])
        except (Urllib3HTTPError, http.client.HTTPException) as e:
            raise requests.ConnectionError(f"The download was interrupted: {e}") from e
        self._remaining -= read
        return read

    def close(self) -> None:
        """Close the response (and with it the connection)."""
        self._response.close()
        super().close()


def _open_download(planned: PlannedFile) -> DownloadStream:
    """Open a referenced file's URL for reading, in place of a member of an archive.

    The limits invenio's `fetch_file` does not have: a timeout (`OAREPO_ROCRATE_IMPORT_ARCHIVE_TIMEOUT`),
    at most `MAX_REDIRECTS` redirects, each re-checked against `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`
    (a redirect must not be a way around the allow-list), a one-line error instead of the response body,
    and a body no longer than the crate's `contentSize` - or than `OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE`
    when neither the crate nor the server states a size.

    The body is asked for unencoded (`Accept-Encoding: identity`): the stored bytes must be the file, not
    its gzip. A server that compresses anyway is decoded, and its `Content-Length` (the compressed size)
    is then not compared.

    A network error, and a status a later attempt may not get (`RETRIABLE_STATUSES`: 429, 503, ...), is not
    handled here: it propagates as a `requests` error, and the task retries it.

    Raises:
        ArchiveError: for a refused host, an HTTP error, a size the crate and the server disagree on, or a
            file larger than allowed.
        requests.RequestException: for a network error or a retriable status.

    """
    _, response = _follow_redirects(
        "GET", planned.url or "", what="file", stream=True, headers={"Accept-Encoding": "identity"}
    )
    if response.status_code in RETRIABLE_STATUSES:
        response.close()
        raise requests.HTTPError(f"The file could not be read yet: HTTP {response.status_code}", response=response)
    if response.status_code != HTTP_OK:
        response.close()
        raise ArchiveError(f"The file could not be read: HTTP {response.status_code}")
    encoded = response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity")
    if encoded:
        response.raw.decode_content = True
    stated = None if encoded else _int_or_none(response.headers.get("Content-Length"))
    if stated is not None and planned.size is not None and stated != planned.size:
        response.close()
        raise ArchiveError(f"The file is {stated} bytes, but the crate states {planned.size}")
    max_size = current_app.config["OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE"]
    known = planned.size if planned.size is not None else stated
    if known is not None and known > max_size:
        response.close()
        raise ArchiveError(f"The file is {known} bytes, more than OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE ({max_size})")
    if known is not None:
        return DownloadStream(response, known, "The file is longer than its stated size")
    too_long = f"The file is larger than OAREPO_ROCRATE_IMPORT_MAX_FILE_SIZE ({max_size})"
    return DownloadStream(response, max_size, too_long)


def _int_or_none(value: str | None) -> int | None:
    """Return the header value as an int, or None when it is missing or not a number."""
    return int(value) if value and value.isdigit() else None


def _ignored(path: str) -> bool:
    """Whether a member is an artefact of re-zipping rather than part of the package."""
    return path.startswith(IGNORED_PREFIXES) or path.rsplit("/", 1)[-1] in IGNORED_NAMES


def _unsafe(path: str) -> bool:
    r"""Whether a member name is absolute or contains an empty, `.` or `..` segment.

    `_read_entries` already replaced `\` by `/` and dropped the directories, so only the name itself is
    left to judge.
    """
    return not path or path.startswith("/") or any(segment in ("", ".", "..") for segment in path.split("/"))


def _package_root(paths: list[str]) -> str:
    """Return the package's directory: `""`, or the single directory every member is in."""
    if not paths:
        return ""
    tops = {path.split("/", 1)[0] for path in paths}
    if len(tops) == 1 and all("/" in path for path in paths):
        return f"{next(iter(tops))}/"
    return ""


def _bag_info(content: bytes) -> dict[str, str]:
    """Return `bag-info.txt` as a mapping (a folded value stays on one line)."""
    info: dict[str, str] = {}
    current: str | None = None
    for line in content.decode("utf-8", errors="replace").splitlines():
        if line[:1].isspace() and current:  # a folded continuation line
            info[current] = f"{info[current]} {line.strip()}"
            continue
        name, separator, value = line.partition(":")
        if not separator:
            current = None
            continue
        current = name.strip()
        info[current] = value.strip()
    return info


def _parse_manifest(content: bytes, root: str) -> dict[str, str]:
    """Return `{archive path: hex digest}` for one bag manifest."""
    digests: dict[str, str] = {}
    for line in content.decode("utf-8", errors="replace").splitlines():
        digest, separator, path = line.partition(" ")
        if not separator or not path.strip():
            continue
        digests[f"{root}{_rfc8493_decode(path.strip())}"] = digest.strip().lower()
    return digests


def _rfc8493_decode(value: str) -> str:
    """Decode a bag manifest path: RFC 8493 escapes only `%0D`, `%0A` and `%25`."""
    return value.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")


@dataclass(frozen=True)
class PlannedFile:
    """One record file an attached package carries, with the digests the package expects of it."""

    key: str
    """The file key."""

    path: str | None = None
    """The archive member holding the content; None for a download."""

    size: int | None = None
    """The size the table of contents (or the crate's `contentSize`) states, when it states one."""

    url: str | None = None
    """The URL to download the content from, instead of reading it from `path`."""

    md5: str | None = None
    """The md5 the crate or the bag manifest states, hex - the checksum stored at upload."""

    sha512: str | None = None
    """The sha512 the bag manifest states, hex - computed from the bytes at export time."""


def plan_files(crate: ROCrate, package: Package, reader: ArchiveReader) -> list[PlannedFile]:
    """Return the stored record files of an attached crate, with their member and expected digests.

    A referenced file (a web-based `File`, see [CrateFile][oarepo_formats.rocrate.imports.crate.CrateFile]) has
    no member and is not planned here; the import fetches or references it like a detached crate's file.

    Raises:
        ArchiveError: if the crate names a file the archive does not carry.

    """
    manifests = reader.manifests()
    md5_manifest = manifests.get("md5", {})
    sha512_manifest = manifests.get("sha512", {})
    entries = {entry.path: entry for entry in reader.entries}
    planned = []
    for crate_file in crate.files:
        if crate_file.path is None:  # referenced, not carried by the archive
            continue
        path = f"{package.crate_root}{crate_file.path}"
        entry = entries.get(path)
        if entry is None:
            raise ArchiveError(f"The crate names the file {crate_file.key!r}, but the archive has no member {path!r}")
        planned.append(
            PlannedFile(
                key=crate_file.key,
                path=path,
                size=entry.size,
                md5=crate_file.checksums.get("md5") or md5_manifest.get(path),
                sha512=sha512_manifest.get(path) or crate_file.checksums.get("sha512"),
            )
        )
    return planned


def fixity_of(file: PlannedFile, *, md5: str, sha512: str) -> tuple[bool, str | None]:
    """Compare the digests of a written file with the package's, and say whether the member is acceptable.

    The two digests answer different questions:

    - **sha512** (bag manifest) was computed from the bytes the source's storage returned at export time.
      A mismatch means the bytes changed *after* the export - in transit or in the copy - so it always
      fails the member.
    - **md5** (the crate's `md5`, which is also `manifest-md5.txt`) is the checksum Invenio stored at
      upload. A mismatch means the source's storage was already corrupt at export time; it fails the
      member only under `OAREPO_ROCRATE_IMPORT_VERIFY = "strict"`.

    Args:
        file: the planned member.
        md5: the md5 computed from the written bytes, hex.
        sha512: the sha512 computed from the written bytes, hex.

    Returns:
        `(accepted, reason)` - `reason` is None when accepted.

    """
    strict = current_app.config["OAREPO_ROCRATE_IMPORT_VERIFY"] == "strict"
    if file.sha512 and file.sha512 != sha512:
        return False, "the sha512 of the member does not match the bag manifest"
    if file.md5 and file.md5.lower() != md5.lower():
        reason = "the md5 of the member does not match the checksum the package states"
        return (False, reason) if strict else (True, reason)
    return True, None


class HashingStream(RawIOBase):
    """A read-through wrapper that hashes what is read from it, and remembers how a read failed.

    The files service reads the stream once, so the digest is complete after `set_file_content` - no
    second pass over the content, which for a remote archive would mean a second download. `error` is the
    exception a read raised, kept because the files service swallows it (see `write_member`).
    """

    def __init__(self, stream: Any, *algorithms: str):
        """Wrap `stream`, computing the given digests (`"sha512"`, `"md5"`, ...)."""
        super().__init__()
        self._stream = stream
        self._hashes = {name: hashlib.new(name) for name in algorithms}
        self.error: Exception | None = None

    def readable(self) -> bool:
        """Return whether the stream is readable."""
        return True

    def seekable(self) -> bool:
        """Return whether the stream is seekable (never)."""
        return False

    def read(self, size: int = -1) -> bytes:
        """Read and hash."""
        try:
            data = self._stream.read(size)
        except Exception as e:
            self.error = e
            raise
        for digest in self._hashes.values():
            digest.update(data)
        return data

    def readinto(self, buffer: Any) -> int:
        """Read into a buffer and hash."""
        try:
            read = self._stream.readinto(buffer)
        except Exception as e:
            self.error = e
            raise
        for digest in self._hashes.values():
            digest.update(bytes(buffer[:read]))
        return read

    def digest(self, algorithm: str) -> str:
        """Return the hex digest computed so far."""
        return self._hashes[algorithm].hexdigest()

    def close(self) -> None:
        """Close the wrapped stream."""
        self._stream.close()
        super().close()


def write_member(  # noqa: PLR0913 - the arguments are the two objects it works with and what to write
    files_service: Any,
    identity: Any,
    record_id: str,
    reader: ArchiveReader | None,
    planned: PlannedFile,
    *,
    uow: Any = None,
) -> str | None:
    """Write one planned file into the record, verify it and commit it.

    The bytes come from the archive (`reader`) or, for a referenced file, from its URL - streamed into the
    files service in one pass (both digests are computed while it is written) and compared with the
    package's before it is committed. A mismatch deletes the entry instead, so nothing unverified is left
    in the draft.

    The md5 is hashed here rather than read back from the files service: before `commit_file` the object
    version is only in the write-in-progress cache, so the service would report no checksum at all.

    Returns:
        `None` when the file is in the record (written now, or by an earlier run), else the reason it is
        not.

    """
    if already_written(files_service, identity, record_id, planned.key):
        return None  # a resumed task: the content is already there
    with _source_of(reader, planned) as member:
        stream = HashingStream(member, "sha512", "md5")
        written = files_service.set_file_content(
            identity, record_id, planned.key, stream, content_length=planned.size, uow=uow
        )
    # invenio swallows a failed write: an `OSError` of the stream (a dropped connection is one) becomes
    # `FailedFileUploadException`, its content component *deletes the file record*, and `set_file_content`
    # returns with `errors` instead of raising. Raise here, so that the caller's unit of work rolls the
    # deletion back (the member stays `pending`) and a network error keeps its type (the task retries it).
    if stream.error is not None:
        raise stream.error
    if written.errors:
        raise RuntimeError(f"{planned.key}: {written.errors}")
    digests = {"sha512": stream.digest("sha512"), "md5": stream.digest("md5")}
    accepted, reason = fixity_of(planned, md5=digests["md5"], sha512=digests["sha512"])
    if not accepted:
        files_service.delete_file(identity, record_id, planned.key, uow=uow)
        return reason
    if reason:
        current_app.logger.warning("RO-Crate import %s: %s was imported although %s", record_id, planned.key, reason)
    files_service.commit_file(identity, record_id, planned.key, uow=uow)
    return None


def _source_of(reader: ArchiveReader | None, planned: PlannedFile) -> Any:
    """Open the bytes of one planned file: its URL, or the archive member the plan names.

    Raises:
        ArchiveError: if the plan names a member but no archive was opened (a planner bug, reported, not
            an `AttributeError` deep in the write).

    """
    if planned.url:
        return _open_download(planned)
    if reader is None or planned.path is None:
        raise ArchiveError(f"The plan names the member {planned.path!r} of an archive that is not open")
    return reader.open(planned.path)


def already_written(files_service: Any, identity: Any, record_id: str, key: str) -> bool:
    """Whether the file is already committed (a retried task must not write it twice).

    A write that did not commit was rolled back with its unit of work, so the file is still `pending` and is
    simply written again.
    """
    metadata = files_service.read_file_metadata(identity, record_id, key).to_dict()
    return metadata.get("status") == TransferStatus.COMPLETED
