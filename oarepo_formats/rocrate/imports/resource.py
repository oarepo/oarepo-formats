# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The `POST /import/rocrate` route and what was configured for it.

`AddROCrateImport` mixes
[ROCrateImportResourceMixin][oarepo_formats.rocrate.imports.resource.ROCrateImportResourceMixin] into the
model's generated `RecordResource` and hands it a
[ROCrateImportConfig][oarepo_formats.rocrate.imports.resource.ROCrateImportConfig] - which import the
converted metadata goes to, and with which conversion. The mixin adds to the resource

```text
POST <model url_prefix>/import/rocrate[?publish][&notifications=off][&metadata=crate|record-json]
    [&files=fetch|reference]
```

and keeps everything else of the resource: the model's error handlers, `url_prefix` and request
identity. The body's media type selects the transport - a detached crate (`application/ld+json`), an
attached package in the body (`application/zip`) or behind a URL (`application/json`, `{"url": ...}`).
Whatever the transport, the record is created and its files are planned in **one unit of work**, so an
invalid crate, an unusable file plan or a missing permission leaves nothing behind.

The overview of the import is in [oarepo_formats.rocrate.imports][]; what happens after the response is
in [oarepo_formats.rocrate.imports.tasks][].
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from flask import current_app, g, jsonify, request
from flask_resources import route
from flask_resources.errors import InvalidContentType
from invenio_db.uow import UnitOfWork
from invenio_records_resources.services.files.transfer.constants import REMOTE_TRANSFER_TYPE
from invenio_records_resources.services.uow import TaskOp
from marshmallow import Schema, ValidationError
from oarepo_runtime.proxies import current_runtime
from werkzeug.exceptions import RequestEntityTooLarge

from ...datacite import DATACITE_JSON_MIMETYPE
from ..exports.aip import FILES as FILES_MEMBER
from .archive import (
    ARCHIVE_MEDIA_TYPES,
    ArchiveReader,
    ArchiveTooLargeError,
    PlannedFile,
    _check_url,
    plan_files,
    write_member,
)
from .common import files_service_for, has_drafts
from .crate import PROFILE_PREFIX, CrateFile, ROCrate
from .crate_datacite import ROCrateToDataCiteSchema
from .tasks import import_archive

if TYPE_CHECKING:
    from collections.abc import Mapping

MIMETYPE = "application/ld+json"
"""Media type of the import body: a detached RO-Crate Package. A `profile` parameter is optional."""

IDENTITY_IMPORT = "application/json"
"""`base_import` of the model's `json` import, which hands the record JSON over unchanged.

Every model has it (oarepo-model's `ImportsPreset`). A `schema_cls` that produces the record's own structure
instead of DataCite is paired with it; the package's own `record.json` goes to it too. Whatever reaches it
is cut down to `PORTABLE_FIELDS` first.
"""

URL_MIMETYPE = "application/json"
"""Media type of a body naming an archive behind a URL: `{"url": "https://..."}`."""

ROUTE = "/import/rocrate"
"""The route below the model's `url_prefix`."""

TRUE_FLAGS = frozenset({"", "1", "true", "yes", "on"})
FALSE_FLAGS = frozenset({"0", "false", "no", "off"})
"""Values of a boolean query argument; `?publish` itself has an empty value."""


@dataclass
class ROCrateImportConfig:
    """What the RO-Crate import of a model was customized with.

    One instance belongs to one model: `AddROCrateImport.apply` creates it together with the mixin it is
    bound to, so a customization instance reused for several models does not share state between them.
    """

    base_import: str = DATACITE_JSON_MIMETYPE
    """Mimetype or code of the model's import the converted metadata is handed to: its DataCite import by
    default, or `IDENTITY_IMPORT` for a `schema_cls` that produces the record's own structure."""

    deserializer: Any = None
    """A deserializer used instead of the model's import (an escape hatch for a model without one)."""

    schema_cls: type[Schema] = ROCrateToDataCiteSchema
    """The conversion of the crate's root data entity: to DataCite (the default, or a subclass of it), or to
    the record's own structure (paired with `base_import=IDENTITY_IMPORT`)."""

    model_name: str = ""
    """`base_name` of the model, set by `AddROCrateImport.apply`; its imports are looked up in the built model."""

    def deserialize(self, body: bytes, *, import_key: str | None = None) -> dict:
        """Return the record data for the metadata, through the model's import.

        Args:
            body: the metadata, in the format `import_key` names.
            import_key: the mimetype (or code) of the model's import, if not `base_import` - the attached
                package's own `record.json` goes to `IDENTITY_IMPORT`.

        Raises:
            ValueError: if the model has no such import - a deployment error, worded like the XSLT
                import's.

        """
        if self.deserializer is not None:
            return self.deserializer.deserialize(body)
        key = import_key or self.base_import
        found = current_runtime.models[self.model_name].get_import(key)
        if found is None:
            raise ValueError(
                f"The model has no import {key!r} (base_import of the RO-Crate import); "
                "register a data import with that mimetype or pass deserializer="
            )
        data = found.deserializer.deserialize(body)
        # the identity import passes everything on: what comes from the crate must not set the record's
        # identifiers, ownership or access - a model's own (e.g. DataCite) import decides that itself
        return _portable_fields(data) if found.mimetype == IDENTITY_IMPORT else data


class ROCrateImportResourceMixin:
    """Add `POST <url_prefix>/import/rocrate` to a model's record resource.

    The route is appended to the resource's own rules; the drafts resource keeps its blueprint prefix
    empty and prefixes its rules itself, so the path follows that (`invenio_drafts_resources...
    resource.py`). Nothing else of the resource is changed: the model's error handlers, `url_prefix` and
    request identity apply to the view as to any other.
    """

    rocrate_import: ROCrateImportConfig
    """Set by `AddROCrateImport.apply` on the generated resource class."""

    service: Any
    """The resource's record service, set by the generated class."""

    config: Any
    """The resource's config, set by the generated class."""

    def create_url_rules(self) -> list[dict]:
        """Return the resource's URL rules, with the import route appended."""
        path = f"{self.config.url_prefix}{ROUTE}" if has_drafts(self.service) else ROUTE
        return [*super().create_url_rules(), route("POST", path, self.import_rocrate)]  # ty: ignore[unresolved-attribute]

    def import_rocrate(self) -> Any:
        """Create a record from a detached crate, an attached package, or a package behind a URL.

        | Body | Transport |
        |---|---|
        | `application/ld+json` | a detached crate: the files are fetched from their URLs |
        | `application/zip` | an attached package in the body: written from it, in the request |
        | `application/json` `{"url": ...}` | an attached package behind a URL: written by a task |

        Returns:
            `{"record": ..., "import": {"files": [...], "publish": ...}}`, 201.

        Raises:
            InvalidContentType: for any other media type, or a foreign `profile` of the detached crate.
            ValidationError: if the crate, its file plan or the metadata is not acceptable.
            PermissionDeniedError: if the caller may not create the record or its files.
            ValueError: if the model has no data import (a deployment error).
            ArchiveError: if the package can not be read (see [oarepo_formats.rocrate.imports.archive][]).

        """
        publish = _flag("publish")
        notify_ok = _notifications_enabled()
        if request.mimetype == MIMETYPE:
            return self._import_detached(publish, notify_ok)
        if request.mimetype in ARCHIVE_MEDIA_TYPES and current_app.config["OAREPO_ROCRATE_IMPORT_ARCHIVE"]:
            try:
                return self._import_package_in_body(publish, notify_ok)
            except ArchiveTooLargeError as e:
                raise RequestEntityTooLarge(description=str(e)) from e
        if request.mimetype == URL_MIMETYPE and current_app.config["OAREPO_ROCRATE_IMPORT_ARCHIVE"]:
            return self._import_package_at_url(publish, notify_ok)
        raise InvalidContentType(allowed_mimetypes=[MIMETYPE, *ARCHIVE_MEDIA_TYPES, URL_MIMETYPE])

    def _import_detached(self, publish: bool, notify_ok: bool) -> Any:
        """Import a detached crate: create the record and download its files from their URLs."""
        crate = _read_crate()
        files_service = files_service_for(self.service)
        entries, downloads = _file_entries(crate, [], files_service)
        if downloads:
            _require_user()

        data = self.rocrate_import.deserialize(json.dumps(self.rocrate_import.schema_cls().dump(crate.root)).encode())
        data["files"] = {"enabled": bool(entries)}

        with UnitOfWork() as uow:
            item = self.service.create(g.identity, data, uow=uow)
            files = files_service.init_files(g.identity, item.id, entries, uow=uow) if entries else None
            # serialized before the commit: the post-commit task writes the files and may publish the draft,
            # which moves the entries under the record and deletes the draft's file records
            report = files.to_dict()["entries"] if files else []
            publish_outcome, item = self._import_publish(item, pending=bool(downloads), publish=publish, uow=uow)
            record = item.to_dict()
            if downloads:
                self._schedule_files(
                    uow,
                    record=record,
                    title=crate.name,
                    publish_outcome=publish_outcome,
                    notify_ok=notify_ok,
                    files=downloads,
                )
            uow.commit()

        return _created(record, report, publish_outcome)

    def _import_package_in_body(self, publish: bool, notify_ok: bool) -> Any:
        """Import an attached package from the request body, in one unit of work.

        The stored files are written in the request; referenced files are downloaded by one task after it
        (the same task that writes a package behind a URL), or are remote files, complete at once.
        """
        with ArchiveReader.from_body(request.stream) as reader:
            files_service = files_service_for(self.service)
            crate, plan, downloads, entries, data = self._plan_package(reader, files_service)
            if downloads:
                _require_user()

            with UnitOfWork() as uow:
                item = self.service.create(g.identity, data, uow=uow)
                files = files_service.init_files(g.identity, item.id, entries, uow=uow) if entries else None
                # with files disabled nothing is written: the report follows the entries
                status = {entry["key"]: entry["status"] for entry in files.to_dict()["entries"]} if files else {}
                if entries:
                    for planned in plan:
                        reason = write_member(files_service, g.identity, item.id, reader, planned, uow=uow)
                        if reason:
                            raise ValidationError({"files": [f"{planned.key!r}: {reason}"]})
                        status[planned.key] = "completed"
                # the stored files are written by now: only the downloads make the publish wait for a task
                publish_outcome, item = self._import_publish(item, pending=bool(downloads), publish=publish, uow=uow)
                record = item.to_dict()
                if downloads:
                    self._schedule_files(
                        uow,
                        record=record,
                        title=crate.name,
                        publish_outcome=publish_outcome,
                        notify_ok=notify_ok,
                        files=downloads,
                    )
                uow.commit()

        report = [{"key": entry["key"], "status": status.get(entry["key"])} for entry in entries]
        return _created(record, report, publish_outcome)

    def _import_package_at_url(self, publish: bool, notify_ok: bool) -> Any:
        """Plan an attached package behind a URL; one task writes its files and finishes the import."""
        url = _archive_url()
        with ArchiveReader.from_url(url) as reader:
            files_service = files_service_for(self.service)
            crate, plan, downloads, entries, data = self._plan_package(reader, files_service)
            if entries:
                _require_user()

            with UnitOfWork() as uow:
                item = self.service.create(g.identity, data, uow=uow)
                files = files_service.init_files(g.identity, item.id, entries, uow=uow) if entries else None
                report = files.to_dict()["entries"] if files else []
                publish_outcome, item = self._import_publish(item, pending=bool(entries), publish=publish, uow=uow)
                record = item.to_dict()
                if entries:
                    archive = {"url": url, "etag": reader.etag}
                    files = [*plan, *downloads]
                    self._schedule_files(
                        uow,
                        record=record,
                        title=crate.name,
                        publish_outcome=publish_outcome,
                        notify_ok=notify_ok,
                        files=files,
                        archive=archive,
                    )
                uow.commit()

        return _created(record, report, publish_outcome)

    def _plan_package(
        self, reader: ArchiveReader, files_service: Any
    ) -> tuple[ROCrate, list[PlannedFile], list[PlannedFile], list[dict], dict]:
        """Read an attached package up to the point where a record can be created from it.

        Both package transports share this: the shape of the archive, the metadata (crate or `record.json`),
        the file plan cross-checked against the archive's table of contents, the validated `init_files`
        entries, and the data the model's import produced. The referenced files are returned apart from the
        members: they are downloaded, not read from the archive.
        """
        package = reader.package
        crate, metadata, import_key = _package_metadata(
            reader,
            package,
            source=_metadata_source(),
            schema_cls=self.rocrate_import.schema_cls,
            base_import=self.rocrate_import.base_import,
            expected_schema=_model_schema(self.service.record_cls),
        )
        plan = plan_files(crate, package, reader)
        entries, downloads = _file_entries(crate, plan, files_service, _file_details(reader, package))
        data = self.rocrate_import.deserialize(metadata, import_key=import_key)
        data["files"] = {"enabled": bool(entries)}
        return crate, plan, downloads, entries, data

    def _schedule_files(  # noqa: PLR0913 - what the task needs to know of the import
        self,
        uow: UnitOfWork,
        *,
        record: dict,
        title: str | None,
        publish_outcome: str | None,
        notify_ok: bool,
        files: list[PlannedFile],
        archive: dict | None = None,
    ) -> None:
        """Register the task that writes the planned files after the commit, and then finishes the import."""
        payload = self._payload(record["id"], record, title, publish_outcome == "scheduled", notify_ok)
        if archive:
            payload["archive"] = archive
        payload["files"] = [_planned_payload(planned) for planned in files]
        uow.register(TaskOp(import_archive, payload=payload))

    def _payload(self, id_: str, record: dict, title: str | None, publish: bool, notify_ok: bool) -> dict:
        """Return the payload the import's tasks carry (one dict, so celery's JSON serializer takes it)."""
        return {
            "service_id": self.service.id,
            "id_": id_,
            "user_id": str(g.identity.id),
            "publish": publish,
            "notify_ok": notify_ok,
            "title": title,
            "revision_id": record.get("revision_id"),
        }

    def _import_publish(self, item: Any, *, pending: bool, publish: bool, uow: UnitOfWork) -> tuple[str | None, Any]:
        """Publish the imported record when `?publish` asked for it, and say what happened.

        Files that are not written yet (`pending`) delay the publish to the import's task: a draft with an
        unfinished transfer is not publishable, so only the permission is checked now. Otherwise the publish
        runs here, in the import's own unit of work, and a refusal refuses the whole import.

        Returns:
            `(outcome, item)` - `("published", the published item)`, `("scheduled", the draft)`, or
            `(None, item)` when `?publish` is absent or the model has no drafts (the create already
            published the record).

        Raises:
            PermissionDeniedError: if the caller may not publish, checked before anything is created.
            ValidationError: if the publish would have to wait for the files but the caller has no user
                identity - the task could neither publish nor mail as nobody.

        """
        if not publish or not has_drafts(self.service):
            return None, item
        # the result item exposes its record only privately, and `require_permission` needs the record
        self.service.require_permission(g.identity, "publish", record=item._record)  # noqa: SLF001
        if pending:
            if g.identity.id is None:
                raise ValidationError(
                    {"publish": ["?publish with files needs a user: the deferred publish runs as the importing user"]}
                )
            return "scheduled", item
        return "published", self.service.publish(g.identity, item.id, uow=uow)


def _require_user() -> None:
    """Refuse an import whose files a task writes after the response, unless a user makes it.

    The task writes the files into the draft as the importing user (and mails them the outcome); a system
    process or an anonymous identity has no user the task could act as, so its files would stay `pending`
    for good. Checked before anything is created.

    Raises:
        ValidationError: for an identity that is not a user.

    """
    try:
        int(g.identity.id)
    except TypeError, ValueError:
        raise ValidationError(
            {"files": ["Files written after the request need a user: they are written as the importing user"]}
        ) from None


def _flag(name: str) -> bool:
    """Return a boolean query argument, refusing a value that is neither true nor false.

    `?publish` means true, `?publish=false` means false. An unknown value is a client error rather than a
    silent `False`: a typo (`?publish=ture`) would otherwise leave a draft the caller meant to publish.

    Raises:
        ValidationError: for a value that is not one of the accepted spellings.

    """
    value = request.args.get(name)
    if value is None:
        return False
    lowered = value.strip().lower()
    if lowered in TRUE_FLAGS:
        return True
    if lowered in FALSE_FLAGS:
        return False
    accepted = "'', '1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'"
    raise ValidationError({name: [f"{value!r} is not one of {accepted}"]})


def _notifications_enabled() -> bool:
    """Return whether the "ok" mails were not switched off with `?notifications=off`.

    Raises:
        ValidationError: for any value other than `on` or `off`.

    """
    value = (request.args.get("notifications") or "on").strip().lower()
    if value not in ("on", "off"):
        raise ValidationError({"notifications": [f"{value!r} is not 'on' or 'off'"]})
    return value == "on"


def _read_crate() -> ROCrate:
    """Return the detached crate the request carries.

    Raises:
        InvalidContentType: if the body names a `profile` other than RO-Crate.
        ValidationError: if the document is not a detached RO-Crate.

    """
    profile = request.mimetype_params.get("profile")
    if profile and not profile.startswith(PROFILE_PREFIX):
        raise InvalidContentType(allowed_mimetypes=[f'{MIMETYPE}; profile="{PROFILE_PREFIX}<version>"'])
    return ROCrate(request.get_json())


def _file_entries(
    crate: ROCrate, plan: list[PlannedFile], files_service: Any, details: Mapping[str, dict] | None = None
) -> tuple[list[dict], list[PlannedFile]]:
    """Return the `init_files` entries of the crate's files and the ones this package downloads.

    Every file gets only its key: the size is enforced by `set_file_content(content_length=...)` and the
    checksum is compared after the write, so putting them on the entry would only store unverified values.
    A referenced file is a download ([_referenced_entry][oarepo_formats.rocrate.imports.resource._referenced_entry]),
    unless `?files=reference` keeps it a remote file (an `R` transfer), complete at once. Either gets the
    `details` the package states for its key: its `metadata` and `access.hidden` (see
    [_file_details][oarepo_formats.rocrate.imports.resource._file_details]).

    Validating here with `initial_file_schema` - the first thing `init_files` does - is what makes the
    import fail before anything is created: the allow-lists, the URL syntax and the key rules come from
    invenio, not from a copy in this package.

    Raises:
        ValidationError: for too many files, for a model without files that would get them, for a file URL
            the deployment does not allow, or when the files service's schema refuses an entry.

    """
    referenced = [crate_file for crate_file in crate.files if crate_file.url is not None]
    count = len(plan) + len(referenced)
    if not count or not current_app.config["OAREPO_ROCRATE_IMPORT_FILES"]:
        return [], []
    if files_service is None:
        raise ValidationError(
            {"files": ["The model has no files; import a crate without files, or set OAREPO_ROCRATE_IMPORT_FILES"]}
        )
    max_files = current_app.config["OAREPO_ROCRATE_IMPORT_MAX_FILES"]
    if count > max_files:
        raise ValidationError({"files": [f"The crate has {count} files, at most {max_files} are imported"]})
    mode = _referenced_files_mode()
    entries = [{"key": planned.key} for planned in plan]
    downloads: list[PlannedFile] = []
    for crate_file in referenced:
        entry, download = _referenced_entry(crate_file, mode)
        entries.append(entry)
        if download is not None:
            downloads.append(download)
    for entry in entries:
        entry.update((details or {}).get(entry["key"], {}))
    try:
        files_service.initial_file_schema.schema(many=True).load(entries)
    except ValidationError as e:
        raise ValidationError({"files": e.messages}) from e
    return entries, downloads


def _referenced_entry(crate_file: CrateFile, mode: str) -> tuple[dict, PlannedFile | None]:
    """Return the `init_files` entry of a referenced file, and the plan that downloads it (if it is one).

    With `mode` `reference` the file stays where it is (an `R` transfer) if its domain is allowed for remote
    files (`RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS`); otherwise - and always with `fetch` - it is
    downloaded by the import's task, and its domain must then be in
    `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`.
    """
    url = crate_file.url or ""
    remote_domains = current_app.config.get("RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS") or ()
    if mode == "reference" and urlparse(url).netloc in remote_domains:
        return {
            "key": crate_file.key,
            "transfer": {"type": REMOTE_TRANSFER_TYPE, "url": url},
            **({"size": crate_file.size} if crate_file.size is not None else {}),
            **({"checksum": crate_file.checksum} if crate_file.checksum else {}),
        }, None
    _check_url(url)  # before anything is created: a refused host is a 400, not a failed import
    return {"key": crate_file.key}, PlannedFile(
        key=crate_file.key,
        url=url,
        size=crate_file.size,
        md5=crate_file.checksums.get("md5"),
        sha512=crate_file.checksums.get("sha512"),
    )


def _file_details(reader: ArchiveReader, package: Any) -> dict[str, dict]:
    """Return the `metadata` and `access.hidden` of the package's files, from its `files.json`, by file key.

    An AIP carries `metadata/repository/files.json` - the files' entries as the repository had them - and
    the crate does not say which files were hidden, nor what metadata (a photo's title, ...) was set on
    them. Only those two are taken; the rest of an entry (size, checksum, storage, links) describes the
    source repository. A package without the member gets nothing.

    Raises:
        ValidationError: if the member is there but can not be read: ignoring it would turn hidden files
            into visible ones.

    """
    path = f"{package.crate_root}{FILES_MEMBER}"
    if path not in {entry.path for entry in reader.entries}:
        return {}
    try:
        document = json.loads(reader.read(path))
    except ValueError as e:
        raise ValidationError({"files": [f"The package's {FILES_MEMBER} is not JSON: {e}"]}) from e
    entries = document.get("entries") if isinstance(document, dict) else None
    if isinstance(entries, list):  # the files REST endpoint's own shape
        entries = {entry.get("key"): entry for entry in entries if isinstance(entry, dict)}
    if not isinstance(entries, dict):
        raise ValidationError({"files": [f"The package's {FILES_MEMBER} has no file entries"]})
    details: dict[str, dict] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        detail: dict[str, Any] = {}
        if isinstance(entry.get("metadata"), dict) and entry["metadata"]:
            detail["metadata"] = entry["metadata"]
        access = entry.get("access")
        if isinstance(access, dict) and isinstance(access.get("hidden"), bool):
            detail["access"] = {"hidden": access["hidden"]}
        if detail:
            details[key] = detail
    return details


def _created(record: dict, report: list[dict], publish_outcome: str | None) -> Any:
    """Return the import's answer: the record, the files and what `?publish` did."""
    return jsonify({"record": record, "import": {"files": report, "publish": publish_outcome}}), 201


def _package_metadata(  # noqa: PLR0913 - what the caller knows and the model decides
    reader: ArchiveReader,
    package: Any,
    *,
    source: str,
    schema_cls: type[Schema],
    base_import: str,
    expected_schema: str | None,
) -> tuple[ROCrate, bytes, str]:
    """Return the crate of an attached package and the bytes the model's import is handed.

    The metadata comes from the RO-Crate by default (`OAREPO_ROCRATE_IMPORT_METADATA = "crate"`), converted
    to DataCite. With `"record-json"` it comes from the package's own `metadata/record.json` instead - the
    record's API JSON, lossless for the same model and meaningless for another.
    """
    crate = ROCrate(json.loads(reader.read(package.descriptor)), attached=True)
    if source != "record-json":
        return crate, json.dumps(schema_cls().dump(crate.root)).encode(), base_import
    return crate, _record_json(reader, crate, package, expected_schema), IDENTITY_IMPORT


def _model_schema(record_cls: Any) -> str | None:
    """Return the `$schema` of the model's records: a `ConstantField`'s value, or a plain string."""
    field = getattr(record_cls, "schema", None)
    value = getattr(field, "value", field)
    return value if isinstance(value, str) and value else None


def _record_json(reader: ArchiveReader, crate: ROCrate, package: Any, expected_schema: str | None) -> bytes:
    """Return the package's own record JSON, after dropping what belongs to the source repository.

    Raises:
        ValidationError: if the package has no `record.json`, it is not JSON, or it declares another
            model's `$schema` (it is only lossless for the model that produced it).

    """
    member = _record_json_member(crate, package, {entry.path for entry in reader.entries})
    if member is None:
        raise ValidationError(
            {"metadata": ["The package carries no record.json; leave OAREPO_ROCRATE_IMPORT_METADATA at 'crate'"]}
        )
    content = reader.read(member)
    try:
        document = json.loads(content)
    except ValueError as e:
        raise ValidationError({"metadata": [f"The package's {member} is not JSON: {e}"]}) from e
    declared = document.get("$schema") if isinstance(document, dict) else None
    if expected_schema and declared and declared != expected_schema:
        raise ValidationError(
            {"metadata": [f"The package's record.json is for the model {declared!r}, not {expected_schema!r}"]}
        )
    return json.dumps(_portable_fields(document)).encode()


def _record_json_member(crate: ROCrate, package: Any, member_paths: set[str]) -> str | None:
    """Return the archive path of the package's `record.json`, if it carries one.

    The member is found the way the crate describes it - a `subjectOf` entry with
    `encodingFormat: application/json` - never by a hard-coded path: `include_exports` is configuration.
    """
    candidates = []
    for value in crate.root.get("subjectOf") or []:
        reference = value if isinstance(value, str) else (value or {}).get("@id")
        if not isinstance(reference, str):
            continue
        path = f"{package.crate_root}{unquote(reference).removeprefix('./')}"
        mimetype = "" if isinstance(value, str) else (value.get("encodingFormat") or "")
        if path in member_paths and path.endswith(".json"):
            candidates.append((path, mimetype))
    for path, mimetype in candidates:
        if mimetype.startswith("application/json"):
            return path
    return next((path for path, _ in candidates if path.endswith("record.json")), None)


PORTABLE_FIELDS = frozenset({"metadata", "custom_fields"})
"""The only parts of the record JSON the identity import takes: a package's `record.json`, or what a custom
`schema_cls` made of the crate.

Everything else describes the *source* repository or is the repository's own decision: identifiers (a `pids`
entry would claim its DOI), ownership, access, state, versions, links, `$schema`. It is an allowlist, so a
key a model adds later is left out until it is listed here, rather than passed through unnoticed - and a
schema that copies crate properties can not set them by accident.
"""


def _portable_fields(document: Any) -> dict:
    """Return the parts of the record JSON that describe the record itself (`PORTABLE_FIELDS`)."""
    if not isinstance(document, dict):
        raise ValidationError({"metadata": ["The record JSON (record.json, or the crate's conversion) is no object"]})
    return {key: value for key, value in document.items() if key in PORTABLE_FIELDS}


def _planned_payload(planned: PlannedFile) -> dict:
    """Return one planned file as the task's payload carries it."""
    return {
        "key": planned.key,
        "path": planned.path,
        "url": planned.url,
        "size": planned.size,
        "md5": planned.md5,
        "sha512": planned.sha512,
    }


REFERENCED_FILES_MODES = ("fetch", "reference")


def _referenced_files_mode() -> str:
    """Return what to do with referenced files: `fetch` them, or keep them as remote files (`reference`).

    `?files=` overrides `OAREPO_ROCRATE_IMPORT_REFERENCED_FILES` for one request.

    Raises:
        ValidationError: for any value but `fetch` and `reference`.

    """
    configured = current_app.config.get("OAREPO_ROCRATE_IMPORT_REFERENCED_FILES", "fetch")
    value = (request.args.get("files") or configured).strip().lower()
    if value not in REFERENCED_FILES_MODES:
        raise ValidationError({"files": [f"{value!r} is not one of {', '.join(REFERENCED_FILES_MODES)}"]})
    return value


METADATA_SOURCES = ("crate", "record-json")


def _metadata_source() -> str:
    """Return where an attached package's metadata comes from: the crate, or the package's `record.json`.

    `?metadata=` overrides `OAREPO_ROCRATE_IMPORT_METADATA` for one request.

    Raises:
        ValidationError: for any value but `crate` and `record-json`.

    """
    value = (request.args.get("metadata") or current_app.config["OAREPO_ROCRATE_IMPORT_METADATA"]).strip().lower()
    if value not in METADATA_SOURCES:
        raise ValidationError({"metadata": [f"{value!r} is not one of {', '.join(METADATA_SOURCES)}"]})
    return value


def _archive_url() -> str:
    """Return the archive URL of a `{"url": ...}` body.

    Raises:
        ValidationError: if the body is not an object with one http(s) `url`.

    """
    document = request.get_json(silent=True)
    url = document.get("url") if isinstance(document, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise ValidationError({"url": ['The body must be {"url": "https://..."} naming a ZIP archive']})
    return url.strip()
