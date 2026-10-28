# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Serializer streaming a record as a ZIP archive with the RO-Crate metadata, metadata exports and files.

The archive is an *Attached RO-Crate Package* (spec 4.3):

```text
<id>.zip
├── ro-crate-metadata.json
├── README.generated.md        # the README export, if the model has it
├── metadata/
│   ├── datacite.xml           # the model's other exports, if it has them
│   ├── dublin-core.xml
│   └── schemaorg.jsonld
└── data/
    └── <file key>             # the record's files the user may read
```

The RO-Crate metadata is the one of the metadata export
([ROCrateSerializer][oarepo_formats.rocrate.exports.serializer.ROCrateSerializer]), converted by
[attach][oarepo_formats.rocrate.exports.zip.attach].
[AddROCrateZipExport][oarepo_formats.rocrate.model.AddROCrateZipExport] registers the export.

## Metadata exports

The archive contains the output of other exports of the record's model, unmodified - by default the
README and the DataCite XML, Dublin Core and schema.org JSON-LD exports of RDM models
(`DEFAULT_INCLUDED_EXPORTS`). An export the model does not have is left out; an export that fails
is left out as well and the error is logged to the application logger. Each included export is a
`File` in the root data entity's `hasPart` and `subjectOf`, `about` the root, with its `md5` and the
export's mimetype as `encodingFormat` and its `oai_schema` (the schema it declares for OAI-PMH) as
`conformsTo`, if it has one
([member_properties][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.member_properties] can be
overridden to change that).

If `OAREPO_ROCRATE_PROFILE` is set, the root data entity declares `conformsTo` that profile and the
profile is added to the crate as a contextual entity.

## Checks

Everything that can fail (permissions, `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`, a missing DataCite
export, an unsafe file key) is checked before the response is returned; the files themselves are read
one at a time while the archive is streamed.

## Adding archive members

Members that are not exports (e.g. an `ro-crate-preview.html`, spec 4.3.2) can be added by a subclass:

```python
class MyZipSerializer(ROCrateZipSerializer):
    def extra_members(self, obj, crate):
        members = list(super().extra_members(obj, crate))  # the included exports
        describe_member(crate, "LICENSE.txt", name="License", encodingFormat="text/plain")
        return [*members, ("LICENSE.txt", b"...")]
```
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from flask import Response, current_app, g, stream_with_context
from flask_resources.serializers import BaseSerializer
from invenio_administration.permissions import administration_permission
from invenio_records_resources.services.files.transfer.constants import REMOTE_TRANSFER_TYPE
from invenio_stats.proxies import current_stats
from oarepo_runtime.api import ExportEngine
from oarepo_runtime.proxies import current_runtime
from werkzeug.exceptions import NotAcceptable
from zipstream import ZIP_STORED, ZipStream

from ...files import add_checksum_terms
from .graph import DESCRIPTOR_INDEX, ROOT_INDEX, to_json_bytes
from .schema import DataCiteToROCrateSchema
from .serializer import ROCrateSerializer

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from oarepo_runtime.api import Export

METADATA_FILE = "ro-crate-metadata.json"
DATA_FOLDER = "data/"
"""Folder of the record's files in the archive."""

DEFAULT_INCLUDED_EXPORTS: Mapping[str, str] = {
    "readme": "README.generated.md",
    "datacite-xml": "metadata/datacite.xml",
    "dublincore": "metadata/dublin-core.xml",
    "jsonld": "metadata/schemaorg.jsonld",
}
"""Export code -> archive path of the exports added to the archive, if the model has them.

`readme` is the [README export][oarepo_formats.readme], the others are exports of RDM models.
"""

CHUNK_SIZE = 1024 * 1024


def is_administrator(identity: Any) -> bool:
    """Return whether an identity has `administration-access`, i.e. `Administration()` in a permission policy.

    Administrators are not subject to `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`: a preservation
    export of a large record is a curator task.
    """
    return administration_permission.allows(identity)


@dataclass
class CrateContent:
    """Everything the RO-Crate serializers need before they write the archive.

    Returned by [collect][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.collect], after every check
    that has to fail before the first byte is streamed.
    """

    document: dict
    """The attached RO-Crate metadata document (spec 16.2), not yet serialized."""

    members: list[tuple[str, bytes]]
    """`(crate path, content)` of the in-memory members - the included exports."""

    files: list[tuple[str, Any]]
    """`(crate path, file record)` of the record's files, in the order of the root's `hasPart`."""

    record: Any
    """The record, for the statistics events of a download."""

    @property
    def graph(self) -> list[dict[str, Any]]:
        """The entities of the metadata document (its `@graph`)."""
        return self.document["@graph"]

    @graph.setter
    def graph(self, entities: list[dict[str, Any]]) -> None:
        self.document["@graph"] = entities

    @property
    def metadata_descriptor(self) -> dict[str, Any]:
        """The `ro-crate-metadata.json` descriptor of the metadata document (`flatten` puts it first)."""
        return self.graph[DESCRIPTOR_INDEX]

    @property
    def root_data_entity(self) -> dict[str, Any]:
        """The root data entity - the dataset the crate describes (`flatten` puts it after the descriptor)."""
        return self.graph[ROOT_INDEX]


def check_file_key(key: str) -> str:
    """Return the key if it is a safe relative path, refusing one that would escape the archive root.

    Used by the archive paths of the export and by the RO-Crate import: a crate's file names are untrusted
    input too.
    """
    segments = key.split("/")
    if not key or key.startswith("/") or any(s in ("", ".", "..") for s in segments):
        raise ValueError(f"File key {key!r} can not be used as a path in the RO-Crate archive")
    return key


def _archive_path(key: str) -> str:
    """Return the archive path of a file key, refusing paths that would escape the archive root."""
    return DATA_FOLDER + check_file_key(key)


def attach(crate: CrateContent, referenced: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, str]:
    """Convert the crate's detached RO-Crate document to the metadata of an attached one (spec 16.2), in place.

    - The root `@id` becomes `./` (a landing page `@id` is kept as `url`; a PID already is the
      `cite-as` and an `identifier`).
    - Every `File` in the root's `hasPart` gets its archive path (`data/<key>`) as `@id`; the download
      URL moves to `contentUrl` and `localPath` is dropped.
    - A file whose key is in `referenced` - one whose content the repository does not store, see
      [is_referenced][oarepo_formats.rocrate.exports.zip.is_referenced] - stays a web-based data entity
      (spec 7.4.3, allowed in an attached crate too): its `@id` is still the download URL and its
      `localPath` the file key, it gets the properties `referenced` gives for it, and it has no archive
      member.

    Args:
        crate: the crate holding the metadata export's document; its files are web-based data entities
            with `localPath` set to the file key.
        referenced: file key -> further properties of the files that are not written into the archive.

    Returns:
        The archive paths of the stored files by file key.

    Raises:
        ValueError: if a file key is not a safe relative path.

    """
    referenced = referenced or {}
    root = crate.root_data_entity
    renamed = {root["@id"]: "./"}
    if root["@id"] not in ("./", root.get("cite-as")):
        root["url"] = root["@id"]

    paths: dict[str, str] = {}
    part_ids = {ref["@id"] for ref in root.get("hasPart", [])}
    for entity in crate.graph:
        if entity["@id"] in part_ids and entity["@type"] == "File":
            if entity.get("localPath") in referenced:
                entity.update(referenced[entity["localPath"]])
                continue
            key = entity.pop("localPath")
            paths[key] = _archive_path(key)
            renamed[entity["@id"]] = quote(paths[key])
            entity["contentUrl"] = entity["@id"]

    def rename(value: Any) -> Any:
        if isinstance(value, list):
            return [rename(v) for v in value]
        if isinstance(value, dict):
            return {k: (renamed.get(v, v) if k == "@id" else rename(v)) for k, v in value.items()}
        return value

    crate.graph = [rename(entity) for entity in crate.graph]
    return paths


def declare_profile(crate: CrateContent, profile: Mapping[str, Any]) -> None:
    """Declare a profile on the crate's root data entity, in place, adding to a `conformsTo` already there.

    The root's `conformsTo` is a single reference when one profile is declared and a list when there
    are more; the profile entity is added to the crate once.
    """
    root = crate.root_data_entity
    reference = {"@id": profile["@id"]}
    declared = root.get("conformsTo")
    if declared is None:
        root["conformsTo"] = reference
    else:
        declared = declared if isinstance(declared, list) else [declared]
        if reference not in declared:
            declared = [*declared, reference]
        root["conformsTo"] = declared[0] if len(declared) == 1 else declared
    if not any(entity.get("@id") == profile["@id"] for entity in crate.graph):
        crate.graph.append({"@type": ["CreativeWork", "Profile"], **profile})


def describe_member(crate: CrateContent, path: str, **properties: Any) -> None:
    """Describe an additional archive member in the metadata: a `File` in the root data entity's `hasPart`.

    Args:
        crate: the crate being built; its metadata document is changed in place.
        path: the member's path in the archive, also its `@id`.
        **properties: further properties of the `File` entity, e.g. `name`, `encodingFormat`.

    """
    crate.root_data_entity.setdefault("hasPart", []).append({"@id": path})
    crate.graph.append({"@id": path, "@type": "File", **properties})


def is_referenced(file_record: Any) -> bool:
    """Return whether a file is only referenced: a remote file (`R` transfer), whose content lives elsewhere.

    Its content can not be read from the repository's storage, so the archives describe it by its
    download URL instead of carrying it.
    """
    return file_record.transfer.transfer_type == REMOTE_TRANSFER_TYPE


def chunks(file_record: Any) -> Iterator[bytes]:
    """Read a stored file chunk by chunk; the file is only opened when the archive stream reaches it."""
    with file_record.open_stream("rb") as fp:
        while chunk := fp.read(CHUNK_SIZE):
            yield chunk


class ROCrateZipSerializer(BaseSerializer):
    """Serialize a record to a streamed ZIP archive: an attached RO-Crate with metadata exports and files.

    `serialize_object` returns a `flask.Response`, so the export must be registered with
    `streaming=True` (as [AddROCrateZipExport][oarepo_formats.rocrate.model.AddROCrateZipExport] does). It needs
    a request context: the files are read with the identity of the current user.

    A subclass that writes another archive reuses
    [collect][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.collect] and can override
    [export_failed][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.export_failed] - see
    [ROCrateBagItSerializer][oarepo_formats.rocrate.exports.bag.ROCrateBagItSerializer].
    """

    def __init__(
        self,
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        include_exports: Mapping[str, str] = DEFAULT_INCLUDED_EXPORTS,
    ):
        """Create the serializer.

        Args:
            schema_cls: `DataCiteToROCrateSchema` or a subclass of it (the metadata mapping).
            include_exports: export code -> archive path of the model's exports to add to the archive,
                each if the model has it.

        """
        self.metadata_serializer = ROCrateSerializer(schema_cls=schema_cls)
        self.include_exports = include_exports

    def extra_members(self, obj: dict, crate: CrateContent) -> Iterable[tuple[str, bytes]]:
        """Return additional `(archive path, content)` members: the included exports the model has.

        Called before the metadata is serialized, so an override can describe its members in the crate
        (see [describe_member][oarepo_formats.rocrate.exports.zip.describe_member]); `crate.members` and
        `crate.files` are still empty.
        """
        model = current_runtime.models_by_schema[obj["$schema"]]
        members = []
        for code, path in self.include_exports.items():
            export = model.get_export_by_code(code)
            if export is None:
                continue
            content = self._render(obj, export, crate, path)
            if content is None:
                continue
            describe_member(crate, path, **self.member_properties(export, content))
            crate.root_data_entity.setdefault("subjectOf", []).append({"@id": path})
            members.append((path, content))
        return members

    def collect(self, obj: dict) -> CrateContent:
        """Return the crate to write: the metadata document, the in-memory members and the files to stream.

        Everything that can fail happens here, before a response is returned: the metadata export, the
        `read_files` permission and `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE` (not for administrators),
        and a described file without stored content.

        The document is mutated further by the caller (provenance, profile) before it is serialized;
        [add_checksum_terms][oarepo_formats.files.add_checksum_terms] has to run last.

        Raises:
            PermissionDeniedError: if the user may not read the record's files.
            ArchiveDownloadTooLargeException: if the files exceed `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`.
            ValueError: if the model has no DataCite JSON export or a file key is not a safe path.

        """
        # the RO-Crate metadata and the included exports share the serialized DataCite export
        with ExportEngine.export_cache():
            crate = CrateContent(document=self.metadata_serializer.dump_obj(obj), members=[], files=[], record=None)
            crate.record, file_records = self._read_files(obj)
            referenced = {key: file for key, file in file_records.items() if is_referenced(file)}
            paths = attach(crate, {key: self.referenced_file_properties(file) for key, file in referenced.items()})
            crate.members.extend(self.extra_members(obj, crate))
        profile = current_app.config.get("OAREPO_ROCRATE_PROFILE")
        if profile:
            declare_profile(crate, profile)
        for key, path in paths.items():
            if key not in file_records:
                raise ValueError(f"File {key!r} of record {obj['id']} is listed but has no stored content")
            crate.files.append((path, file_records[key]))
        return crate

    def serialize_object(self, obj: dict) -> Response:
        """Return a streaming response with the ZIP archive of a serialized record.

        Raises:
            PermissionDeniedError: if the user may not read the record's files.
            ArchiveDownloadTooLargeException: if the files exceed `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`.
            ValueError: if the model has no DataCite JSON export or a file key is not a safe path.

        """
        crate = self.collect(obj)
        add_checksum_terms(crate.document)  # the added members have checksums even if the record's files have none

        zs = ZipStream(compress_type=ZIP_STORED, sized=True)
        metadata = to_json_bytes(crate.document)
        zs.add(metadata, METADATA_FILE, size=len(metadata))
        for path, content in crate.members:
            zs.add(content, path, size=len(content))
        for path, file_record in crate.files:
            zs.add(chunks(file_record), path, size=file_record.file.size)
        self._emit_download_events(crate.record, [file_record for _, file_record in crate.files])

        return Response(
            stream_with_context(iter(zs)),
            mimetype="application/zip",
            headers={"Content-Length": str(len(zs)), "Content-Disposition": f"attachment; filename={obj['id']}.zip"},
        )

    def serialize_object_list(self, obj_list: dict) -> str:  # noqa: ARG002 always refused
        """Refuse search results: there is no archive of many records."""
        raise NotAcceptable("The RO-Crate ZIP export is available for single records only.")

    def export_failed(
        self,
        obj: dict,
        crate: CrateContent,
        export: Export,
        path: str,
        error: str,
    ) -> None:
        """Handle an included export that is left out of the archive, after the error was logged.

        An export is left out when it raises and when it is streaming (a streaming export can not be
        included in a streamed archive). No-op here; a subclass that has to record the omission
        overrides it - [ROCrateBagItSerializer][oarepo_formats.rocrate.exports.bag.ROCrateBagItSerializer] adds a
        failed action to the crate, whose metadata is serialized after `extra_members`.

        Args:
            obj: the serialized record.
            crate: the crate being built, still mutable.
            export: the export that was left out.
            path: its archive path (`include_exports` value).
            error: the message describing why it is missing.

        """

    def _render(self, obj: dict, export: Export, crate: CrateContent, path: str) -> bytes | None:
        """Return the output of an export, or None if it can not be added to the archive.

        The failure is logged and passed to
        [export_failed][oarepo_formats.rocrate.exports.zip.ROCrateZipSerializer.export_failed].
        """
        if export.streaming:
            current_app.logger.error("RO-Crate ZIP: export %r is streaming and can not be included", export.code)
            self.export_failed(obj, crate, export, path, "The export is streaming and can not be included")
            return None
        try:
            content = export.serializer.serialize_object(obj)
        except Exception as e:
            # one broken format must not make the whole archive unavailable
            current_app.logger.exception(
                "RO-Crate ZIP: export %r of record %s failed, left out", export.code, obj["id"]
            )
            self.export_failed(obj, crate, export, path, f"{type(e).__name__}: {e}")
            return None
        return content if isinstance(content, bytes) else content.encode()

    def referenced_file_properties(self, file_record: Any) -> dict[str, Any]:  # noqa: ARG002 a hook
        """Return further properties of the `File` entity of a referenced (remote) file; none here.

        The archives describe such a file by its download URL (`/content`, which redirects to the content and
        counts the download). The AIP export adds where the content actually is.
        """
        return {}

    def member_properties(self, export: Export, content: bytes) -> dict[str, Any]:
        """Return the properties of the `File` entity of an included export.

        `conformsTo` is the export's `oai_schema`, if it has one. Override to declare another schema -
        never one the export's output does not satisfy.
        """
        properties: dict[str, Any] = {
            "name": str(export.name),
            "description": "Exported automatically by the repository from the metadata of the dataset.",
            "encodingFormat": export.mimetype,
            "contentSize": str(len(content)),
            "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),  # as the record's files
            "about": {"@id": "./"},
        }
        if export.oai_schema:
            properties["conformsTo"] = {"@id": export.oai_schema}
        return properties

    def content_identity(self) -> Any:
        """Return the identity the archive is read with: the requesting user.

        [ROCrateAIPSerializer][oarepo_formats.rocrate.exports.aip.ROCrateAIPSerializer] overrides it with
        `system_identity`, after its own entry check, so that every elevated read is in one place.
        """
        return g.identity

    def _read_files(self, obj: dict) -> tuple[Any, dict[str, Any]]:
        """Return the record and its stored files by key, checked like Invenio's `/files-archive` endpoint.

        A metadata-only record has no files to list, and the files service refuses it.
        """
        model = current_runtime.models_by_schema[obj["$schema"]]
        service = model.draft_file_service if obj.get("is_draft") else model.file_service
        if service is None or not (obj.get("files") or {}).get("enabled", True):  # a model without files
            return None, {}
        # like Invenio's own archive endpoint, the file records are only available as the private _results
        files = self._list_files(service, obj)
        return files._record, {f.key: f for f in files._results if f.file is not None}  # noqa: SLF001

    def _list_files(self, service: Any, obj: dict) -> Any:
        """List the record's files: `read_archive`, unless the identity may ignore the download limit.

        `read_archive` checks the `read_files` permission and
        `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`; `list_files` checks the permission only (and is
        what the AIP export uses: it has no size limit).
        """
        identity = self.content_identity()
        if is_administrator(identity):
            return service.list_files(identity, obj["id"])
        return service.read_archive(identity, obj["id"])

    def _emit_download_events(self, record: Any, file_records: list[Any]) -> None:
        """Emit a `file-download` statistics event per file, as Invenio's archive endpoint does."""
        emitter = current_stats.get_event_emitter("file-download")
        if emitter is None:
            return
        for file_record in file_records:
            emitter(current_app, record=record, obj=file_record.object_version, via_api=True)
