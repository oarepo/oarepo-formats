# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Hand-built packages and a fake HTTP host serving them by ranges - shared by the unit and the API tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from io import BytesIO

import requests
import responses

from oarepo_formats.rocrate.imports.archive import TAIL_BYTES

PROFILE = "https://w3id.org/ro/crate/1.3"
DESCRIPTOR = "ro-crate-metadata.json"
CSV = b"a,b\n1,2\n"


def crate_document(files: dict[str, str], members: dict[str, str] | None = None) -> dict:
    """Return an attached crate: `files` is key -> archive path, `members` is path -> mimetype."""
    members = members or {}
    graph: list[dict] = [
        {"@id": DESCRIPTOR, "@type": "CreativeWork", "about": {"@id": "./"}, "conformsTo": {"@id": PROFILE}},
        {
            "@id": "./",
            "@type": "Dataset",
            "name": "A dataset",
            "hasPart": [{"@id": path} for path in [*files.values(), *members]],
            "subjectOf": [{"@id": path} for path in members],
        },
    ]
    graph.extend({"@id": path, "@type": "File", "name": key} for key, path in files.items())
    graph.extend({"@id": path, "@type": "File", "encodingFormat": mimetype} for path, mimetype in members.items())
    return {"@context": "https://w3id.org/ro/crate/1.3/context", "@graph": graph}


def zip_of(entries: dict[str, bytes]) -> bytes:
    """Return a ZIP with the given members, in the given order."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_file:
        for path, content in entries.items():
            archive_file.writestr(path, content)
    return buffer.getvalue()


def attached_package(
    files: dict[str, bytes] | None = None,
    *,
    root: str = "",
    members: dict[str, bytes] | None = None,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    """Return a plain attached crate ZIP; the descriptor is the first member."""
    files = files or {"data.csv": CSV}
    members = members or {}
    document = crate_document(
        {key: f"data/{key}" for key in files},
        dict.fromkeys(members, "application/json"),
    )
    entries = {f"{root}{DESCRIPTOR}": json.dumps(document).encode()}
    entries.update({f"{root}data/{key}": content for key, content in files.items()})
    entries.update({f"{root}{path}": content for path, content in members.items()})
    entries.update(extra or {})
    return zip_of(entries)


def rfc8493(value: str) -> str:
    """Encode a path the way RFC 8493 manifests do."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def bag_package(files: dict[str, bytes] | None = None, *, root: str = "abc-123/", level: str = "standard") -> bytes:
    """Return a BagIt bag carrying an attached crate, with both manifests."""
    files = files or {"data.csv": CSV}
    entries = {
        f"{root}bagit.txt": b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n",
        f"{root}bag-info.txt": f"Bag-Software-Agent: oarepo-formats\nOARepo-Package-Level: {level}\n".encode(),
        f"{root}data/{DESCRIPTOR}": json.dumps(crate_document({key: f"data/{key}" for key in files})).encode(),
    }
    entries.update({f"{root}data/data/{key}": content for key, content in files.items()})
    for name, algorithm in (("manifest-md5.txt", "md5"), ("manifest-sha512.txt", "sha512")):
        lines = [
            f"{hashlib.new(algorithm, content).hexdigest()}  {rfc8493(f'data/data/{key}')}"
            for key, content in files.items()
        ]
        entries[f"{root}{name}"] = ("\n".join(lines) + "\n").encode()
    return zip_of(entries)


def big_package(size: int = 200 * 1024) -> bytes:
    """Return a plain crate whose payload is larger than the tail a remote reader preloads."""
    return attached_package(files={"data.csv": os.urandom(size)})


class RangeServer:
    """A fake static host serving one archive by HTTP ranges, through `responses`.

    - `etag` is what the host answers now; `next_etags` changes it on each `HEAD` (to replace the object
      between the import's request and its task);
    - `drops` makes that many ranged reads inside `drop_range` fail like a dropped connection;
    - `ranges` records what was read, so a test can bound the bytes transferred.
    """

    url = "https://data.example.org:8443/archive.zip"

    def __init__(self, content: bytes = b"", *, etag: str = '"v1"', accept_ranges: bool = True):
        """Serve `content`; nothing is registered until `register`."""
        self.content = content
        self.etag = etag
        self.next_etags: list[str] = []
        self.accept_ranges = accept_ranges
        self.ranges: list[tuple[int, int]] = []
        self.drops = 0
        self.drop_range = (0, 0)

    @property
    def served(self) -> int:
        """How many bytes the host has returned so far."""
        return sum(end - start + 1 for start, end in self.ranges)

    def drop_member_reads(self, member: str, times: int) -> None:
        """Fail the next `times` reads of `member`'s data - not its local header, not the preloaded tail.

        That is where a real transfer loses its connection: while the files service streams the member.
        """
        with zipfile.ZipFile(BytesIO(self.content)) as archive_file:
            offset = archive_file.getinfo(member).header_offset
        name_length = int.from_bytes(self.content[offset + 26 : offset + 28], "little")
        extra_length = int.from_bytes(self.content[offset + 28 : offset + 30], "little")
        self.drop_range = (offset + 30 + name_length + extra_length, len(self.content) - TAIL_BYTES)
        self.drops = times

    def head(self, request):
        """Answer the `HEAD`: size, ETag and whether ranges are supported."""
        if self.next_etags:
            self.etag = self.next_etags.pop(0)
        headers = {"Content-Length": str(len(self.content)), "ETag": self.etag}
        if self.accept_ranges:
            headers["Accept-Ranges"] = "bytes"
        return 200, headers, b""

    def get(self, request):
        """Answer a ranged `GET` with the slice, 412 for another version, or a dropped connection."""
        if request.headers.get("If-Range") not in (None, self.etag):
            return 412, {}, b""
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("Range", ""))
        if not match:
            return 200, {"Content-Length": str(len(self.content))}, self.content
        start, end = int(match.group(1)), int(match.group(2))
        if self.drops and self.drop_range[0] <= start < self.drop_range[1]:
            self.drops -= 1
            raise requests.ConnectionError("connection reset by peer")
        self.ranges.append((start, end))
        return 206, {"Content-Range": f"bytes {start}-{end}/{len(self.content)}"}, self.content[start : end + 1]

    def register(self, mocked=responses, url: str | None = None) -> RangeServer:
        """Register the host on `mocked` (the `responses` module or a `RequestsMock`) and return it."""
        mocked.add_callback(responses.HEAD, url or self.url, callback=self.head)
        mocked.add_callback(responses.GET, url or self.url, callback=self.get)
        return self
