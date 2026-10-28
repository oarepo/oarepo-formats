# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The RO-Crate AIP export: the record's full Archival Information Package, for holders of `aip-access`.

It is the [RO-Crate BagIt export][oarepo_formats.rocrate.exports.bag] extended with everything the repository
knows about the record, so that the record can be reconstructed and understood without the repository:

```text
<id>-aip.zip
└── <id>/
    ├── bagit.txt
    ├── bag-info.txt              # OARepo-Package-Level: aip, OARepo-Audit-Log, OARepo-Checks, ...
    ├── data/
    │   ├── ro-crate-metadata.json
    │   ├── metadata/
    │   │   ├── record.json       # the complete, expanded record
    │   │   ├── datacite.xml, ...
    │   │   └── repository/
    │   │       ├── files.json
    │   │       ├── versions.json
    │   │       ├── requests.json
    │   │       ├── checks.json
    │   │       ├── audit-log.json
    │   │       ├── schema.json
    │   │       └── environment.json
    │   └── data/<file key>       # shown and hidden files
    ├── manifest-md5.txt
    ├── manifest-sha512.txt
    └── tagmanifest-sha512.txt
```

## Access

`serialize_object` checks `aip-access` before anything is read and answers 403 otherwise. After that
check every read uses `system_identity`: the record itself (re-read, expanded), all files including
hidden ones, the audit log, the requests with their timelines and the curation checks. That elevation is
deliberate - an AIP holds restricted content and personal data - and it is confined to
[content_identity][oarepo_formats.rocrate.exports.aip.ROCrateAIPSerializer.content_identity] and the collectors.

The export does not grant access to a record the holder can not open: the deployment adds `AIPAccess()`
to the model's permission policy (see [oarepo_formats.rocrate.permissions][]).

## Members

Each member under `metadata/repository/` is produced by a **collector** - a function from an
[AIPContext][oarepo_formats.rocrate.exports.aip.AIPContext] to a list of
[AIPMember][oarepo_formats.rocrate.exports.aip.AIPMember]s, serialized as JSON and described in the crate
like an included export. A collector returns an empty list
when its source does not exist for the model or the deployment (no drafts, invenio-requests/checks/audit
logs not installed or disabled), and a failure leaves the member out, logs the error and records a failed
action in the crate.

`OAREPO_ROCRATE_AIP_COLLECTORS` adds a deployment's own collectors, as import strings, after the defaults.

## The files

Hidden files are part of the payload (`files.json` keeps `access.hidden: true`); files whose upload is
not completed and files without stored content are left out of both the payload and `files.json`.
Remote files (`R` transfer) are kept as references: a web-based `File` with its download URL as `@id`
and the remote URL as `contentUrl`, no payload member, and the remote URL in `files.json` (`transfer.url`).

## Accountability

Every export is logged at INFO level and, when audit logs are enabled, recorded as a
`record.aip_export` audit log entry before the response is returned - so it is not in its own package,
the next one carries it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from flask import current_app, g
from invenio_access.permissions import system_identity
from invenio_audit_logs.proxies import current_audit_logs_service
from invenio_drafts_resources.auditlog.actions import RecordBaseAuditLog
from invenio_i18n import lazy_gettext as _
from invenio_records_resources.services.errors import PermissionDeniedError
from invenio_records_resources.services.files.transfer.constants import REMOTE_TRANSFER_TYPE

from ... import __version__
from ..permissions import has_aip_access
from .bag import BAGIT_INCLUDED_EXPORTS, ROCrateBagItSerializer
from .graph import to_json_bytes
from .provenance import (
    add_audit_provenance,
    add_check_provenance,
    member_failed_action,
    repository_application,
)
from .schema import DataCiteToROCrateSchema
from .zip import CrateContent, describe_member

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

FILES = "metadata/repository/files.json"
VERSIONS = "metadata/repository/versions.json"
REQUESTS = "metadata/repository/requests.json"
CHECKS = "metadata/repository/checks.json"
AUDIT_LOG = "metadata/repository/audit-log.json"
SCHEMA = "metadata/repository/schema.json"
ENVIRONMENT = "metadata/repository/environment.json"

PAGE_SIZE = 100
"""Page size of the paginated collector searches."""


@dataclass
class AIPContext:
    """What a collector reads the repository through."""

    record: dict
    """The unfiltered, expanded record (the system's view), the same `obj` the crate is built from."""

    record_api: Any
    """The record API object (the `_record` of the files service result), or None for a record without files."""

    model: Any
    """The oarepo-runtime model of the record."""

    is_draft: bool
    """Whether the exported record is a draft."""

    collected: dict[str, Any] = field(default_factory=dict)
    """Structured results a collector publishes for the provenance and `bag-info.txt`.

    `audit_log` holds the audit entries and `checks` the `{"configs": [...], "runs": [...]}` data that
    also went into the JSON members; their presence (even empty) means the source is enabled.
    """


@dataclass
class AIPMember:
    """One additional member of the package."""

    path: str
    """Crate path, e.g. `metadata/repository/files.json`."""

    content: Any
    """The content, serialized as JSON (UTF-8, `ensure_ascii=False`, sorted keys, two-space indent)."""

    description: str
    """The `description` of the member's `File` entity: what the member is for."""


Collector = Callable[[AIPContext], list[AIPMember]]


def collect_files(context: AIPContext) -> list[AIPMember]:
    """Return the record's file entries - every included file's metadata, as the files REST endpoint has it."""
    service = context.model.draft_file_service if context.is_draft else context.model.file_service
    if service is None:
        return []
    result = service.list_files(system_identity, context.record["id"])
    files = result.to_dict()
    entries = {entry["key"]: entry for entry in (files.get("entries") or []) if _has_content(entry)}
    # the URL of a remote file is load-only in the REST API (its downloads go through `/content`, for the
    # statistics); the package is a preservation copy, so it keeps where the content actually is
    remote_urls = {record.key: record.transfer.get("url") for record in result._results}  # noqa: SLF001
    for key, entry in entries.items():
        if _is_remote(entry) and remote_urls.get(key):
            entry["transfer"] = {**entry["transfer"], "url": remote_urls[key]}
    files["entries"] = entries
    return [AIPMember(FILES, files, "The record's files: key, size, checksum, mimetype, access, timestamps.")]


def collect_versions(context: AIPContext) -> list[AIPMember]:
    """Return the versions of the record's concept record, the newest first."""
    search_versions = getattr(context.model.service, "search_versions", None)
    if search_versions is None:  # a model without drafts
        return []
    versions = []
    page = 1
    while True:
        result = search_versions(system_identity, context.record["id"], params={"size": PAGE_SIZE, "page": page})
        hits = list(result.to_dict()["hits"]["hits"])
        versions.extend(_version(hit) for hit in hits)
        if len(hits) < PAGE_SIZE:
            break
        page += 1
    return [AIPMember(VERSIONS, {"versions": versions}, "The record's version chain, newest first.")]


def collect_requests(context: AIPContext) -> list[AIPMember]:
    """Return the requests on the record with their timelines: comments, decisions and their authors."""
    try:
        from invenio_requests.proxies import current_requests_service
    except ImportError:
        return []
    requests = []
    page = 1
    while True:
        result = current_requests_service.search(
            system_identity,
            params={"topic": {"record": context.record["id"]}, "size": PAGE_SIZE, "page": page},
            expand=True,
        )
        hits = list(result.to_dict()["hits"]["hits"])
        requests.extend({"request": hit, "events": _events(hit["id"])} for hit in hits)
        if len(hits) < PAGE_SIZE:
            break
        page += 1
    return [AIPMember(REQUESTS, {"requests": requests}, "The requests on the record and their timelines.")]


def collect_checks(context: AIPContext) -> list[AIPMember]:
    """Return the curation check configurations and runs of the record and its draft, if checks are enabled."""
    if not current_app.config.get("CHECKS_ENABLED", False) or context.record_api is None:
        return []
    try:
        from invenio_checks.api import ChecksAPI
    except ImportError:
        return []
    runs = [
        *_check_runs(ChecksAPI.get_runs(context.record_api, is_draft=False)),
        *_check_runs(ChecksAPI.get_runs(context.record_api, is_draft=True)),
    ]
    configs = {}
    for run in runs:
        configs[run["config_id"]] = run.pop("config")
    data: dict[str, Any] = {"configs": list(configs.values()), "runs": runs}
    context.collected["checks"] = data
    return [AIPMember(CHECKS, data, "The curation checks configured for the record and the runs they made.")]


def collect_audit_log(context: AIPContext) -> list[AIPMember]:
    """Return the audit log entries of the record and of its parent, if audit logging is enabled."""
    if not current_app.config.get("AUDIT_LOGS_ENABLED", False) or "invenio-audit-logs" not in current_app.extensions:
        return []
    query = f'resource.type:record AND resource.id:"{context.record["id"]}"'
    parent_id = (context.record.get("parent") or {}).get("id")
    if parent_id:
        query = f'({query}) OR (resource.type:parent AND resource.id:"{parent_id}")'
    entries = list(current_audit_logs_service.scan(system_identity, params={"q": query}).hits)
    entries.sort(key=lambda entry: str(entry.get("created") or ""))
    context.collected["audit_log"] = entries
    return [AIPMember(AUDIT_LOG, {"entries": entries}, "The record's audit log, oldest first, as recorded.")]


def collect_schema(context: AIPContext) -> list[AIPMember]:
    """Return the record's JSON Schema, its references resolved."""
    from invenio_jsonschemas.proxies import current_jsonschemas

    path = (context.record.get("$schema") or "").removeprefix("local://")
    if not path:
        return []
    schema = current_jsonschemas.get_schema(path, with_refs=True, resolved=True)
    return [AIPMember(SCHEMA, schema, "The JSON Schema of the record, references resolved.")]


def collect_environment(context: AIPContext) -> list[AIPMember]:
    """Return which software produced the record and can read it."""
    environment = {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": {
            "code": context.model.code,
            "name": str(context.model.name),
            "version": str(context.model.version),
        },
        "record": {"id": context.record["id"], "$schema": context.record.get("$schema")},
        "site": {
            "ui_url": current_app.config.get("SITE_UI_URL"),
            "api_url": current_app.config.get("SITE_API_URL"),
        },
        "software": {
            "oarepo-formats": __version__,
            **_installed_versions(("oarepo", "invenio")),
        },
    }
    return [AIPMember(ENVIRONMENT, environment, "The model, the site and the software versions of the export.")]


DEFAULT_AIP_COLLECTORS: tuple[Collector, ...] = (
    collect_files,
    collect_versions,
    collect_requests,
    collect_checks,
    collect_audit_log,
    collect_schema,
    collect_environment,
)
"""The members the AIP carries, in the order of [oarepo_formats.rocrate.exports.aip][]; extend with the config."""

COLLECTOR_PATHS: Mapping[Collector, str] = {
    collect_files: FILES,
    collect_versions: VERSIONS,
    collect_requests: REQUESTS,
    collect_checks: CHECKS,
    collect_audit_log: AUDIT_LOG,
    collect_schema: SCHEMA,
    collect_environment: ENVIRONMENT,
}
"""Member path of each default collector, to name it when it fails."""


class RecordAIPExportAuditLog(RecordBaseAuditLog):
    """Audit log entry of an AIP export of a record, registered under `invenio_audit_logs.actions`."""

    id = "record.aip_export"
    message_template = _("An Archival Information Package of the record was exported.")  # ty: ignore[invalid-assignment]


class ROCrateAIPSerializer(ROCrateBagItSerializer):
    """Serialize a record as its Archival Information Package, for holders of `aip-access`.

    It is the BagIt export with the record re-read as the system sees it, hidden files, the audit log,
    the requests, the curation checks and the rest of the repository's information about the record (see
    the module documentation). `serialize_object` raises `PermissionDeniedError` for a caller without
    `aip-access`, before anything is read.
    """

    package_level = "aip"

    person_prefix = "#user-"

    def __init__(
        self,
        schema_cls: type[DataCiteToROCrateSchema] = DataCiteToROCrateSchema,
        include_exports: Mapping[str, str] = BAGIT_INCLUDED_EXPORTS,
        extension: str = "-aip.zip",
    ):
        """Create the serializer; the arguments are those of `ROCrateBagItSerializer`."""
        super().__init__(
            schema_cls=_schema_with_hidden_files(schema_cls), include_exports=include_exports, extension=extension
        )

    def serialize_object(self, obj: dict) -> Any:
        """Return a streaming response with the record's AIP, or refuse the caller.

        Raises:
            PermissionDeniedError: if the caller does not hold `aip-access`.
            ValueError: as the BagIt export (no DataCite export, an unsafe file key, an unsafe record id).

        """
        if not has_aip_access(g.identity):
            raise PermissionDeniedError("aip-access")
        return super().serialize_object(obj)

    def content_identity(self) -> Any:
        """Return `system_identity`: after the entry check the package is read as the system."""
        return system_identity

    def referenced_file_properties(self, file_record: Any) -> dict[str, Any]:
        """Return the `contentUrl` of a remote file: where its content actually is.

        The REST API hides that URL (downloads go through `/content`, for the statistics), but the package
        is a preservation copy and has to say where the content lives. Only remote files get it: a stored
        file's `/content` may redirect too, but to a pre-signed storage URL that expires.
        """
        url = file_record.transfer.get("url")
        return {"contentUrl": url} if url else {}

    def _list_files(self, service: Any, obj: dict) -> Any:
        """List all the record's files, without `RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE`."""
        return service.list_files(system_identity, obj["id"])

    def record_to_export(self, obj: dict) -> dict:
        """Return the record as the system sees it, expanded: the view the whole package is built from.

        Files that have nothing to preserve - an unfinished upload, a file without stored content - are
        removed from the view, so they are in neither the payload, nor `hasPart`, nor `files.json`.
        """
        model = self._model(obj)
        service = model.service
        if obj.get("is_draft"):
            item = service.read_draft(system_identity, obj["id"], expand=True)
        else:
            item = service.read(system_identity, obj["id"], expand=True)
        record = item.to_dict()
        record.setdefault("$schema", obj["$schema"])
        record.setdefault("is_draft", bool(obj.get("is_draft")))
        files = record.get("files") or {}
        # the record's own entries do not say how a file is stored; the files service's do
        listed = self._file_entries(model, record)
        files["entries"] = {
            key: entry for key, entry in (files.get("entries") or {}).items() if _has_content(listed.get(key, entry))
        }
        record["files"] = files
        return record

    def _file_entries(self, model: Any, record: dict) -> dict[str, dict]:
        """Return the files service's entries of the record's files by key (with `transfer`), if it has files."""
        service = model.draft_file_service if record.get("is_draft") else model.file_service
        if service is None or not (record.get("files") or {}).get("enabled", True):
            return {}
        return {entry["key"]: entry for entry in service.list_files(system_identity, record["id"]).to_dict()["entries"]}

    def extend_package(self, crate: CrateContent, obj: dict) -> dict[str, str]:
        """Add the repository members and their provenance, and return the AIP's `bag-info.txt` fields."""
        model = self._model(obj)
        context = AIPContext(
            record=obj,
            record_api=crate.record or self._record_api(model, obj),
            model=model,
            is_draft=bool(obj.get("is_draft")),
        )
        for collector in self.collectors():
            try:
                members = collector(context)
            except Exception as e:
                current_app.logger.exception("RO-Crate AIP: collector %r of record %s failed", collector, obj["id"])
                crate.graph.append(member_failed_action(self._collector_path(collector), f"{type(e).__name__}: {e}"))
                continue
            for member in members:
                self._add_member(crate, member)
        if context.collected.get("audit_log") is not None:
            add_audit_provenance(
                crate,
                context.collected["audit_log"],
                person_prefix=self.person_prefix,
                repository=repository_application(),
            )
        checks = context.collected.get("checks")
        if checks:
            add_check_provenance(crate, checks["runs"], checks["configs"])
        return {
            "OARepo-Audit-Log": _inclusion(context.collected.get("audit_log"), "entries"),
            "OARepo-Checks": _inclusion((checks or {}).get("runs"), "runs"),
            "OARepo-Package-Complete": "false" if _has_failures(crate) else "true",
        }

    def record_export(self, obj: dict) -> None:
        """Record the export in the record's audit log, before the response is returned, if enabled."""
        entry = RecordAIPExportAuditLog.build(g.identity, obj["id"], parent=_parent_reference(obj))
        if entry is None:  # audit logging is disabled
            return
        try:
            current_audit_logs_service.create(system_identity, entry)
        except Exception:
            current_app.logger.exception(
                "RO-Crate AIP: could not record the export of record %s in the audit log", obj["id"]
            )

    def collectors(self) -> list[Collector]:
        """Return the default collectors and the deployment's (`OAREPO_ROCRATE_AIP_COLLECTORS`)."""
        configured = current_app.config.get("OAREPO_ROCRATE_AIP_COLLECTORS") or ()
        return [*DEFAULT_AIP_COLLECTORS, *(_import_callable(path) for path in configured)]

    def _add_member(self, crate: CrateContent, member: AIPMember) -> None:
        """Serialize a member as JSON, describe it in the crate and add it to the payload."""
        content = to_json_bytes(member.content, indent=2, sort_keys=True)
        describe_member(
            crate,
            member.path,
            name=member.path.rsplit("/", 1)[-1],
            description=member.description,
            encodingFormat="application/json",
            contentSize=str(len(content)),
            md5=hashlib.md5(content, usedforsecurity=False).hexdigest(),
            about={"@id": "./"},
        )
        crate.root_data_entity.setdefault("subjectOf", []).append({"@id": member.path})
        crate.members.append((member.path, content))

    def _model(self, obj: dict) -> Any:
        """Return the oarepo-runtime model of the record."""
        from oarepo_runtime.proxies import current_runtime

        return current_runtime.models_by_schema[obj["$schema"]]

    def _record_api(self, model: Any, obj: dict) -> Any:
        """Return the record API object, for models whose files service did not provide it."""
        return model.record_cls.pid.resolve(obj["id"], registered_only=False)

    def _collector_path(self, collector: Collector) -> str:
        """Return the member path of a collector, to name it in a failed action."""
        name = getattr(collector, "__name__", "collector")
        return COLLECTOR_PATHS.get(collector) or f"metadata/repository/{name}.json"


def _events(request_id: str) -> list[dict]:
    """Return the timeline of a request: its events, oldest first, with their authors resolved."""
    from invenio_requests.proxies import current_events_service

    events = []
    page = 1
    while True:
        result = current_events_service.search(
            system_identity, request_id, params={"size": PAGE_SIZE, "page": page}, expand=True
        )
        hits = list(result.to_dict()["hits"]["hits"])
        events.extend(hits)
        if len(hits) < PAGE_SIZE:
            return events
        page += 1


def _schema_with_hidden_files(schema_cls: type[DataCiteToROCrateSchema]) -> type[DataCiteToROCrateSchema]:
    """Return the mapping with the AIP's view of the record: hidden files are part of `hasPart`."""
    return type(f"AIP{schema_cls.__name__}", (schema_cls,), {"include_hidden_files": True})


def _has_content(entry: dict) -> bool:
    """Return whether a file entry describes a file there is something to preserve for.

    An unfinished upload (status not `completed`) and a broken file (no stored checksum, i.e. no stored
    content) are left out of the package entirely. A remote file (`R` transfer) has no stored content by
    design: it is kept, as a reference.
    """
    if entry.get("status", "completed") != "completed":
        return False
    return bool(entry.get("checksum")) or _is_remote(entry)


def _is_remote(entry: dict) -> bool:
    """Return whether a file entry of the files service describes a remote file (`R` transfer)."""
    return (entry.get("transfer") or {}).get("type") == REMOTE_TRANSFER_TYPE


def _version(hit: dict) -> dict:
    """Return the reconstruction-relevant fields of one version of the record."""
    return {
        "id": hit.get("id"),
        "index": (hit.get("versions") or {}).get("index"),
        "is_published": hit.get("is_published"),
        "created": hit.get("created"),
        "updated": hit.get("updated"),
        "pids": hit.get("pids"),
    }


def _check_runs(runs: Iterable[Any]) -> list[dict]:
    """Return the check runs as serialized data, each carrying its configuration for the caller to collect."""
    return [_check_run(run) for run in runs]


def _check_run(run: Any) -> dict:
    """Return one check run; `config` is popped by the caller into the configurations of the member."""
    return {
        "id": str(run.id),
        "config_id": str(run.config_id),
        "record_id": str(run.record_id),
        "is_draft": run.is_draft,
        "revision_id": run.revision_id,
        "start_time": _iso(run.start_time),
        "end_time": _iso(run.end_time),
        "status": run.status.name.lower(),
        "state": run.state,
        "result": run.result,
        "config": _check_config(run.config),
    }


def _check_config(config: Any) -> dict:
    """Return a check configuration with its community, so that it is identifiable without the repository."""
    return {
        "id": str(config.id),
        "check_id": config.check_id,
        "params": config.params,
        "severity": config.severity.error_value,
        "enabled": config.enabled,
        "community": _community(config.community_id),
    }


def _community(community_id: Any) -> dict[str, Any]:
    """Return the id, slug and title of a community, or only its id if it is gone or global."""
    if community_id is None:
        return {}
    community: dict[str, Any] = {"id": str(community_id)}
    try:
        from invenio_communities.proxies import current_communities

        result = current_communities.service.read(system_identity, str(community_id))
        community["slug"] = result.data.get("slug")
        community["title"] = (result.data.get("metadata") or {}).get("title")
    except Exception:  # noqa: BLE001 a deleted community must not lose the run
        current_app.logger.warning("RO-Crate AIP: community %s of a check run could not be read", community_id)
    return community


def _installed_versions(prefixes: tuple[str, ...]) -> dict[str, str]:
    """Return the versions of the installed distributions whose name starts with one of `prefixes`."""
    from importlib.metadata import distributions

    versions = {}
    for distribution in distributions():
        name = (distribution.metadata or {}).get("Name") or ""
        normalized = name.lower().replace("_", "-")
        if normalized.startswith(prefixes):
            versions[name] = distribution.version
    return versions


def _inclusion(value: Any, unit: str) -> str:
    """Return the `bag-info.txt` value of a source: `disabled`, or `included (<n> <unit>)`."""
    if value is None:
        return "disabled"
    return f"included ({len(value)} {unit})"


def _has_failures(crate: CrateContent) -> bool:
    """Return whether the crate records a member or an export that was left out."""
    return any(
        entity.get("@id", "").endswith("-failed") and entity.get("@id", "").startswith(("#member-", "#export-"))
        for entity in crate.graph
    )


def _parent_reference(obj: dict) -> Any:
    """Return the parent of the record for the audit log context, from the serialized record."""
    parent_id = (obj.get("parent") or {}).get("id")
    if not parent_id:
        return None
    return SimpleNamespace(pid=SimpleNamespace(pid_value=str(parent_id)))


def _iso(value: Any) -> str | None:
    """Return a datetime as an ISO 8601 string, or the value itself if it is already one."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _import_callable(path: str) -> Collector:
    """Import a collector from its `module:function` path."""
    module_name, _, attribute = path.partition(":")
    if not attribute:
        raise ValueError(f"AIP collector {path!r} is not of the form 'module:function'")
    return getattr(import_module(module_name), attribute)
