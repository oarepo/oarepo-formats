# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Serializer streaming a record as a BagIt bag (RFC 8493) whose payload is an attached RO-Crate.

The archive is a zipped bag, so a preservation workflow can take it directly and any BagIt validator
can check it:

```text
<id>-bagit.zip
└── <id>/                         # the bag
    ├── bagit.txt                 # BagIt-Version: 1.0, Tag-File-Character-Encoding: UTF-8
    ├── bag-info.txt              # identification, Payload-Oxum, record state, deployment fields
    ├── data/                     # the payload: the RO-Crate, the same content as the RO-Crate ZIP
    │   ├── ro-crate-metadata.json        # + provenance, + the profile
    │   ├── README.generated.md
    │   ├── metadata/datacite.xml, dublin-core.xml, schemaorg.jsonld, record.json
    │   └── data/<file key>
    ├── manifest-md5.txt          # written after data/
    ├── manifest-sha512.txt       # written after data/
    └── tagmanifest-sha512.txt    # written last
```

The crate's files end up in `data/data/` of the bag: the bag's `data/` is the RO-Crate root, and the
crate keeps its own layout, so the crate is identical to the one of the RO-Crate ZIP.

## Streaming

Nothing is staged on disk and no file is held in memory: the record's files are read one chunk at a
time while the archive is written, and each chunk updates the file's MD5 and SHA-512. The manifests
can only be written after the payload, so they are lazy generators - zipstream-ng runs them when the
stream reaches them - with a size declared up front, taken from the same path list they are built
from, so `Content-Length` stays exact.

`bagit.txt`, `bag-info.txt` and the payload come first and are known before streaming; `manifest-md5.txt`
and `manifest-sha512.txt` follow the payload; `tagmanifest-sha512.txt` comes last and covers both
manifests (RFC 8493 requires a tag manifest to list every payload manifest).

## Fixity

The MD5 lines of the record's files are the checksums Invenio stores, so a bag validator compares them
with the bytes the storage returned: a file corrupted in storage makes the bag invalid, which is the
signal. The MD5 the serializer computes while streaming is compared with the stored one and a mismatch
is logged with both values (the stored one is kept). A file whose stored checksum is not MD5 gets the
computed MD5 instead. The `md5` values in `ro-crate-metadata.json` come from the same stored
checksums, so the crate and the bag agree by construction.

## When the package is an AIP

The export always produces a valid bagged RO-Crate; it is an OAIS *Archival Information Package* of a
record version when the caller may read the record's files (otherwise nothing is streamed) and the
version is published. The bag records the record state (`OARepo-Record-State`), so a package taken from
a draft can not be mistaken for one.

See [oarepo_formats.rocrate][] for the export and the customization that registers it.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from flask import Response, current_app, g, stream_with_context
from zipstream import ZIP_STORED, ZipStream

from ... import __version__
from ...files import add_checksum_terms
from .graph import to_json_bytes
from .provenance import FORMATS_URL, add_provenance, failed_export_action
from .schema import DataCiteToROCrateSchema
from .zip import (
    DEFAULT_INCLUDED_EXPORTS,
    METADATA_FILE,
    CrateContent,
    ROCrateZipSerializer,
    chunks,
    declare_profile,
    is_administrator,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from oarepo_runtime.api import Export

BAGIT_TXT = b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
"""Content of `bagit.txt` (RFC 8493 2.1.1): exactly two lines, UTF-8, no BOM."""

BAGIT_TXT_NAME = "bagit.txt"
BAG_INFO = "bag-info.txt"
MANIFEST_MD5 = "manifest-md5.txt"
MANIFEST_SHA512 = "manifest-sha512.txt"
TAGMANIFEST_SHA512 = "tagmanifest-sha512.txt"

PAYLOAD_FOLDER = "data/"
"""Payload directory of the bag; it is also the RO-Crate root, hence `data/data/<key>` for the files."""

MD5_DIGITS = 32
SHA512_DIGITS = 128
"""Length of a hex digest, in characters: the manifest line size depends on it."""

BAGIT_INCLUDED_EXPORTS: Mapping[str, str] = {**DEFAULT_INCLUDED_EXPORTS, "json": "metadata/record.json"}
"""Export code -> archive path of the exports added to the bag.

The default RO-Crate ZIP exports plus the record's own API JSON (`metadata/record.json`), which only
the bag carries: it is what a re-ingest would need.
"""

BAG_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def bag_name(record_id: str) -> str:
    """Return the directory name of the bag - the record's id - refusing anything that is not a path segment.

    Raises:
        ValueError: if the id contains a character outside `[A-Za-z0-9._-]`, or has no letter or digit
            (`.`, `..`), which would make the directory a relative path.

    """
    if not BAG_NAME.match(record_id) or not any(character.isalnum() for character in record_id):
        raise ValueError(f"Record id {record_id!r} can not be used as the directory of the bag")
    return record_id


def manifest_path(crate_path: str) -> str:
    """Return the path of a crate member in a manifest: in the payload directory, RFC 8493 2.1.3 encoded.

    Only `%`, CR and LF are percent-encoded; a space or a non-ASCII character stays as it is (the tag
    files are UTF-8). The ZIP entry name is [`bag_entry`][oarepo_formats.rocrate.exports.bag.bag_entry], which is
    the same path without that encoding.

    `%25` for a literal `%` is what RFC 8493 asks for; bagit-python 1.9 decodes only `%0D`/`%0A`, so it
    reports a bag with such a key as incomplete. The RFC takes precedence.
    """
    encoded = crate_path.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return PAYLOAD_FOLDER + encoded


def bag_entry(bag: str, crate_path: str) -> str:
    """Return the ZIP entry name of a crate member: the bag directory, the payload directory, the path."""
    return f"{bag}/{PAYLOAD_FOLDER}{crate_path}"


def manifest(digests: Mapping[str, str]) -> bytes:
    """Return the bytes of a manifest: one `<digest>  <path>` line per file, sorted by the path's UTF-8 bytes.

    Two spaces between the digest and the path, as `md5sum` and `sha512sum` write them and as
    bagit-python does.
    """
    return "".join(f"{digests[path]}  {path}\n" for path in sorted(digests, key=str.encode)).encode()


def manifest_size(paths: Iterable[str], digest_length: int) -> int:
    """Return the exact size of a manifest of `paths`, each with a `digest_length` character digest.

    Declared to zipstream-ng before the manifest is generated; the generator checks the result against
    it, because a wrong size would corrupt the archive after the headers were sent.
    """
    return sum(digest_length + 2 + len(path.encode()) + 1 for path in paths)


def bag_info(fields: Iterable[tuple[str, str]]) -> bytes:
    """Return `bag-info.txt`: one `Label: value` line per field, in the order given, UTF-8.

    A value stays on one line - a line break in it (a title) becomes a space. RFC 8493 *recommends*
    continuation lines for long values, but a single long line is valid and simpler to check.

    Raises:
        ValueError: if a label contains a colon or a line break, which RFC 8493 does not allow.

    """
    lines = []
    for label, value in fields:
        if ":" in label or "\n" in label or "\r" in label:
            raise ValueError(f"Label {label!r} can not be used in bag-info.txt")
        lines.append(f"{label}: {' '.join(str(value).splitlines())}\n")
    return "".join(lines).encode()


@dataclass
class _Digests:
    """Digests of the payload by manifest path, filled while the payload is streamed."""

    md5: dict[str, str] = field(default_factory=dict)
    sha512: dict[str, str] = field(default_factory=dict)


class GeneratedTagFile:
    """A tag file whose bytes are generated when the stream reaches it, i.e. after the payload.

    zipstream-ng calls the generator of an entry only when it reaches it, so a manifest can be built
    from digests that are complete only after the payload was streamed. Its size is declared up front
    (the archive size is computed from it), so the generated bytes are checked against it.
    """

    def __init__(self, size: int, build: Callable[[], bytes]):
        """Create the tag file with its declared size and the function building its bytes."""
        self.size = size
        self._build = build
        self.content: bytes | None = None

    def __iter__(self) -> Iterator[bytes]:
        """Generate the bytes once, refusing a size that would corrupt the archive."""
        self.content = content = self._build()
        if len(content) != self.size:
            raise RuntimeError(f"Generated {len(content)} bytes, declared {self.size}: the archive would be corrupt")
        yield content


class ROCrateBagItSerializer(ROCrateZipSerializer):
    """Serialize a record to a streamed zipped BagIt bag whose payload is the attached RO-Crate.

    `serialize_object` returns a `flask.Response`, so the export must be registered with
    `streaming=True` (as [AddROCrateBagItExport][oarepo_formats.rocrate.model.AddROCrateBagItExport] does).
    It needs a request context: the files are read with the identity of the current user.

    Everything checked by [collect][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.collect] fails before
    the response is returned; a file that can not be read fails in the middle of the stream, which leaves
    a truncated download no ZIP reader accepts.
    """

    package_level = "standard"
    """Value of `OARepo-Package-Level` in `bag-info.txt`: this export's package, or `aip` for the AIP."""

    person_prefix = "#owner-"
    """`@id` prefix of a person entity; the AIP export uses `#user-` and records names and e-mails."""

    def __init__(
        self,
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        include_exports: Mapping[str, str] = BAGIT_INCLUDED_EXPORTS,
        extension: str = "-bagit.zip",
    ):
        """Create the serializer.

        Args:
            schema_cls: `DataCiteToROCrateSchema` or a subclass of it (the metadata mapping).
            include_exports: export code -> archive path of the model's exports to add to the payload.
            extension: appended to the record's id in the download name, as the export's `extension` is
                on the oarepo-ui route (`<id><extension>`).

        """
        super().__init__(schema_cls=schema_cls, include_exports=include_exports)
        self.extension = extension

    def export_failed(
        self,
        obj: dict,  # noqa: ARG002 the record is not needed
        crate: CrateContent,
        export: Export,
        path: str,
        error: str,
    ) -> None:
        """Record an included export that was left out of the package as a failed action in the crate."""
        crate.graph.append(failed_export_action(export, path, error))

    def serialize_object(self, obj: dict) -> Response:
        """Return a streaming response with the zipped BagIt bag of a serialized record.

        Raises:
            PermissionDeniedError: if the user may not read the record's files.
            ArchiveDownloadTooLargeException: if the files exceed `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`.
            ValueError: if the model has no DataCite JSON export, a file key is not a safe path, or the
                record's id can not be the directory of the bag.

        """
        obj = self.record_to_export(obj)
        crate = self.collect(obj)
        extra_info = self.extend_package(crate, obj)
        add_provenance(crate, obj, person_prefix=self.person_prefix)
        bagit_profile = current_app.config.get("OAREPO_ROCRATE_BAGIT_PROFILE")
        if bagit_profile:
            declare_profile(crate, bagit_profile)
        add_checksum_terms(crate.document)

        bag = bag_name(obj["id"])
        metadata = to_json_bytes(crate.document)
        payload = [(METADATA_FILE, metadata), *crate.members]
        payload_paths = [manifest_path(crate_path) for crate_path, _ in payload] + [
            manifest_path(crate_path) for crate_path, _ in crate.files
        ]
        payload_size = sum(len(content) for _, content in payload) + sum(
            file_record.file.size for _, file_record in crate.files
        )
        digests = _Digests()
        for crate_path, content in payload:
            digests.md5[manifest_path(crate_path)] = hashlib.md5(content, usedforsecurity=False).hexdigest()
            digests.sha512[manifest_path(crate_path)] = _sha512_hex(content)
        for crate_path, file_record in crate.files:
            stored = _stored_md5(file_record)
            if stored is not None:
                digests.md5[manifest_path(crate_path)] = stored  # the fixity Invenio keeps

        info = bag_info(self._bag_info_fields(obj, payload_size, len(payload_paths), extra_info))
        self._log_export(obj, len(payload_paths), payload_size)
        self.record_export(obj)

        zs = ZipStream(compress_type=ZIP_STORED, sized=True)
        zs.add(BAGIT_TXT, f"{bag}/{BAGIT_TXT_NAME}", size=len(BAGIT_TXT))
        zs.add(info, f"{bag}/{BAG_INFO}", size=len(info))
        for crate_path, content in payload:
            zs.add(content, bag_entry(bag, crate_path), size=len(content))
        for crate_path, file_record in crate.files:
            zs.add(
                self._hashed_chunks(obj, crate_path, file_record, digests),
                bag_entry(bag, crate_path),
                size=file_record.file.size,
            )

        md5_manifest = GeneratedTagFile(manifest_size(payload_paths, MD5_DIGITS), lambda: manifest(digests.md5))
        sha512_manifest = GeneratedTagFile(
            manifest_size(payload_paths, SHA512_DIGITS), lambda: manifest(digests.sha512)
        )
        zs.add(iter(md5_manifest), f"{bag}/{MANIFEST_MD5}", size=md5_manifest.size)
        zs.add(iter(sha512_manifest), f"{bag}/{MANIFEST_SHA512}", size=sha512_manifest.size)

        tag_files = (BAG_INFO, BAGIT_TXT_NAME, MANIFEST_MD5, MANIFEST_SHA512)

        def tag_manifest_content() -> bytes:
            """Hash the tag files; the stream reaches this entry after the two manifests were generated."""
            md5_content, sha512_content = md5_manifest.content, sha512_manifest.content
            if md5_content is None or sha512_content is None:
                raise RuntimeError("The tag manifest was generated before the manifests it covers")
            return manifest(
                {
                    BAG_INFO: _sha512_hex(info),
                    BAGIT_TXT_NAME: _sha512_hex(BAGIT_TXT),
                    MANIFEST_MD5: _sha512_hex(md5_content),
                    MANIFEST_SHA512: _sha512_hex(sha512_content),
                }
            )

        tag_manifest = GeneratedTagFile(manifest_size(tag_files, SHA512_DIGITS), tag_manifest_content)
        zs.add(iter(tag_manifest), f"{bag}/{TAGMANIFEST_SHA512}", size=tag_manifest.size)

        return Response(
            stream_with_context(iter(zs)),
            mimetype="application/zip",
            headers={
                "Content-Length": str(len(zs)),
                "Content-Disposition": f"attachment; filename={obj['id']}{self.extension}",
            },
        )

    def record_to_export(self, obj: dict) -> dict:
        """Return the record to export; a subclass replaces it with the record it has to carry.

        The AIP export re-reads the record with `system_identity` and `expand=True` here, after its entry
        check, so that the crate, the included exports and `record.json` all come from the same view.
        """
        return obj

    def extend_package(
        self,
        crate: CrateContent,  # noqa: ARG002 the hook exists to be overridden
        obj: dict,  # noqa: ARG002
    ) -> dict[str, str]:
        """Add what this package carries beyond the RO-Crate ZIP's content; return extra `bag-info.txt` fields.

        Called after [collect][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.collect], before the crate is
        serialized, so an override can add members and context to `crate` in place. The AIP export adds
        the repository members and the audit log / check runs provenance here.
        """
        return {}

    def record_export(self, obj: dict) -> None:
        """Record that the package was created, before the response is returned.

        No-op here; the AIP export writes an audit log entry.
        """

    def _bag_info_fields(
        self, obj: dict, payload_size: int, payload_count: int, extra: Mapping[str, str] | None = None
    ) -> list[tuple[str, str]]:
        """Return the `bag-info.txt` fields of the record, `extra` and the configuration last."""
        title = (obj.get("metadata") or {}).get("title")
        description = f"RO-Crate BagIt export of the record {obj['id']}"
        if title:
            description = f"{description}: {title}"
        fields = [
            ("Bagging-Date", datetime.now(UTC).date().isoformat()),
            ("Bag-Software-Agent", f"oarepo-formats {__version__} <{FORMATS_URL}>"),
            ("Payload-Oxum", f"{payload_size}.{payload_count}"),
            ("External-Identifier", f"urn:uuid:{uuid.uuid4()}"),
            ("External-Description", description),
            ("Internal-Sender-Identifier", str(obj["id"])),
            ("OARepo-Package-Level", self.package_level),
        ]
        concept_id = (obj.get("parent") or {}).get("id")
        if concept_id:
            fields.append(("Bag-Group-Identifier", str(concept_id)))
        fields.append(("OARepo-Record-State", "draft" if obj.get("is_draft") else "published"))
        fields.extend((extra or {}).items())
        configured = current_app.config.get("OAREPO_ROCRATE_BAGIT_INFO") or {}
        written = {label.lower() for label, _ in fields}
        fields.extend((label, str(value)) for label, value in configured.items() if label.lower() not in written)
        return fields

    def _hashed_chunks(self, obj: dict, crate_path: str, file_record: Any, digests: _Digests) -> Iterator[bytes]:
        """Stream a stored file, hashing it on the way; its digests are complete when the generator ends.

        The MD5 Invenio stores is kept in the manifest even when the bytes disagree with it: that is
        what makes the bag invalid, and the mismatch is logged with both values.
        """
        path = manifest_path(crate_path)
        stored = _stored_md5(file_record)
        size = file_record.file.size
        md5 = hashlib.md5(usedforsecurity=False)
        sha512 = hashlib.sha512()
        read = 0
        for chunk in chunks(file_record):
            read += len(chunk)
            md5.update(chunk)
            sha512.update(chunk)
            yield chunk
        if read != size:
            current_app.logger.error(
                "RO-Crate BagIt: file %r of record %s: stored size %d, read %d",
                file_record.key,
                obj["id"],
                size,
                read,
            )
        computed = md5.hexdigest()
        if stored is not None and computed != stored:
            current_app.logger.error(
                "RO-Crate BagIt: file %r of record %s: stored md5 %s, computed %s, the bag will be invalid",
                file_record.key,
                obj["id"],
                stored,
                computed,
            )
        digests.md5[path] = stored if stored is not None else computed
        digests.sha512[path] = sha512.hexdigest()

    def _log_export(self, obj: dict, payload_count: int, payload_size: int) -> None:
        """Log who exported which record, for accountability (restricted content may be in the package)."""
        current_app.logger.info(
            "RO-Crate BagIt: exported record %s for user %s: %d payload files, %d bytes%s",
            obj["id"],
            g.identity.id,
            payload_count,
            payload_size,
            ", size limit bypassed" if is_administrator(g.identity) else "",
        )


def _stored_md5(file_record: Any) -> str | None:
    """Return the MD5 Invenio stores for a file, or None if the stored checksum uses another algorithm."""
    algorithm, _, value = (file_record.file.checksum or "").partition(":")
    return value if algorithm == "md5" and value else None


def _sha512_hex(content: bytes) -> str:
    """Return the lowercase hex SHA-512 digest of `content`."""
    return hashlib.sha512(content).hexdigest()
