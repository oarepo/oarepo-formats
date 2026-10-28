# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Provenance of the record and of the package as RO-Crate actions.

[add_provenance][oarepo_formats.rocrate.exports.provenance.add_provenance] describes the history of the record
and the packaging itself with `CreateAction` and `UpdateAction` entities (RO-Crate 1.3, *Detailing
provenance of entities*): every action has an `object` (the root data entity), an `endTime` and an
`instrument`, and a failed action an `actionStatus` and an `error`.

The record actions come from the serialized record - `created`, `updated`, `is_published`,
`versions.index`, `parent.id` and, on RDM models, `metadata.publication_date` and
`parent.access.owned_by`. There is no database lookup and no audit log; the actions are therefore only
as detailed as the record itself.

The RO-Crate AIP export adds two more sources, for holders of `aip-access`:

- [add_audit_provenance][oarepo_formats.rocrate.exports.provenance.add_audit_provenance] - one action per audit
  log entry, with the recorded user as its agent (name and e-mail are kept: the AIP is not a public
  download, see [oarepo_formats.rocrate.exports.aip][]);
- [add_check_provenance][oarepo_formats.rocrate.exports.provenance.add_check_provenance] - one `AssessAction`
  per curation check run.

A person is one entity per user id - [person_entity][oarepo_formats.rocrate.exports.provenance.person_entity]
merges what several sources say about the same person. The BagIt export references its owner as
`#owner-<user id>` without a name or an e-mail, because every reader of the record can download it; the
AIP uses `#user-<user id>` with everything the sources provide.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from flask import current_app

from ... import __version__
from .zip import METADATA_FILE

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from oarepo_runtime.api import Export

    from .zip import CrateContent

FORMATS_URL = "https://github.com/oarepo/oarepo-formats"
"""`@id` of the oarepo-formats software application, the `instrument` of the actions it records."""

ROOT = {"@id": "./"}
"""`object` of a curation action: the action concerns the dataset as a whole, not one of its files."""

COMPLETED = {"@id": "http://schema.org/CompletedActionStatus"}
FAILED = {"@id": "http://schema.org/FailedActionStatus"}
ACTIVE = {"@id": "http://schema.org/ActiveActionStatus"}

AUDIT_ACTIONS: Mapping[str, tuple[str, str]] = {
    "draft.create": ("CreateAction", "Draft created"),
    "draft.new_version": ("CreateAction", "New version created"),
    "draft.edit": ("UpdateAction", "Draft edited"),
    "record.publish": ("UpdateAction", "Record published"),
    "draft.delete": ("UpdateAction", "Draft deleted"),
    "record.aip_export": ("CreateAction", "Archival Information Package created"),
}
"""Audit action id -> (`@type`, `name`) of its RO-Crate action; an unknown id is kept as the name."""

AUDIT_ACTION_SUFFIXES: Mapping[str, tuple[str, str]] = {
    "grant_update": ("UpdateAction", "Access grants changed"),
    "secret_link_update": ("UpdateAction", "Secret links changed"),
    "access_settings_update": ("UpdateAction", "Access settings changed"),
}

CHECK_STATUSES: Mapping[str, dict[str, str]] = {
    "completed": COMPLETED,
    "error": FAILED,
    "pending": ACTIVE,
    "running": ACTIVE,
}
"""Check run status -> `actionStatus` of its `AssessAction`."""


def add_provenance(crate: CrateContent, obj: dict, *, person_prefix: str = "#owner-") -> None:
    """Add the actions of the record and of the packaging to the crate, in place.

    Args:
        crate: the crate being built; its metadata document is changed in place.
        obj: the serialized record (API JSON) the crate was built from.
        person_prefix: `@id` prefix of the owner entity; the AIP export uses `#user-` for every person.

    """
    repository = repository_application()
    formats = _formats_application()
    instruments = {application["@id"]: application for application in (formats, repository) if application}
    actions = []

    created = obj.get("created")
    if created:
        action = _action("#record-created", "CreateAction", "Record created in the repository", created, repository)
        owner = _owner(obj)
        if owner:
            action["agent"] = person_entity(crate, owner, prefix=person_prefix)
        actions.append(action)

    if obj.get("is_published"):
        index = (obj.get("versions") or {}).get("index")
        name = f"Version {index} published" if index is not None else "Version published"
        action = _action("#version-published", "UpdateAction", name, None, repository)
        published = (obj.get("metadata") or {}).get("publication_date")
        if published:
            action["endTime"] = published
        actions.append(action)

    updated = obj.get("updated")
    if updated and updated != created:
        actions.append(_action("#record-modified", "UpdateAction", "Record last modified", updated, repository))

    bag = _action("#bag-created", "CreateAction", "RO-Crate BagIt package created", _now(), formats)
    bag["result"] = {"@id": METADATA_FILE}
    actions.append(bag)

    crate.graph.extend(actions)
    crate.graph.extend(instruments.values())


def add_audit_provenance(
    crate: CrateContent,
    entries: Iterable[dict],
    *,
    person_prefix: str = "#user-",
    repository: dict | None = None,
) -> None:
    """Add one action per audit log entry, in place.

    Args:
        crate: the crate being built; its metadata document is changed in place.
        entries: the audit log entries as the service returned them.
        person_prefix: `@id` prefix of the agent entities.
        repository: the repository's `SoftwareApplication` entity, the `instrument` of every action.

    """
    instrument = {"@id": repository["@id"]} if repository else {"@id": FORMATS_URL}
    actions = []
    for entry in entries:
        action_id = entry.get("id")
        action_type, name = _audit_shape(str(entry.get("action") or ""))
        action: dict[str, Any] = {
            "@id": f"#audit-{action_id}",
            "@type": action_type,
            "name": name,
            "object": ROOT,
            "actionStatus": COMPLETED,
            "identifier": action_id,
            "instrument": instrument,
        }
        if entry.get("created"):
            action["endTime"] = entry["created"]
        user = entry.get("user") or {}
        if user.get("id"):
            action["agent"] = person_entity(
                crate, user["id"], prefix=person_prefix, name=user.get("username"), email=user.get("email")
            )
        actions.append(action)
    crate.graph.extend(actions)


def add_check_provenance(crate: CrateContent, runs: Iterable[dict], configs: Iterable[dict]) -> None:
    """Add one `AssessAction` per curation check run, and the check as its instrument, in place.

    Args:
        crate: the crate being built; its metadata document is changed in place.
        runs: the runs as serialized into `checks.json` (each with `id`, `config_id`, `status`,
            `start_time`/`end_time` and `result`).
        configs: the configurations of `checks.json`, for the check id, the community slug and the
            action's `description`.

    """
    configurations = {config.get("id"): config for config in configs}
    actions = []
    instruments: dict[str, dict] = {}
    for run in runs:
        config = configurations.get(run.get("config_id"), {})
        check_id = str(config.get("check_id") or "check")
        action: dict[str, Any] = {
            "@id": f"#check-run-{run['id']}",
            "@type": "AssessAction",
            "name": _check_name(check_id, (config.get("community") or {}).get("slug")),
            "object": ROOT,
            "identifier": run["id"],
            "instrument": {"@id": f"#check-{check_id}"},
            "actionStatus": CHECK_STATUSES.get(str(run.get("status")), ACTIVE),
            "description": _check_result(run.get("result")),
        }
        if run.get("start_time"):
            action["startTime"] = run["start_time"]
        if run.get("end_time"):
            action["endTime"] = run["end_time"]
        actions.append(action)
        instruments[check_id] = {"@id": f"#check-{check_id}", "@type": "SoftwareApplication", "name": check_id}
    crate.graph.extend(actions)
    crate.graph.extend(instruments.values())


def failed_export_action(export: Export, path: str, error: str) -> dict:
    """Return the action describing an included export that was left out of the package.

    Args:
        export: the export that is missing.
        path: its archive path (the `include_exports` value).
        error: the message describing why it is missing.

    """
    return {
        "@id": f"#export-{export.code}-failed",
        "@type": "CreateAction",
        "name": f"Export {export.code} ({path}) failed",
        "object": ROOT,
        "instrument": {"@id": FORMATS_URL},
        "endTime": _now(),
        "actionStatus": FAILED,
        "error": error,
    }


def member_failed_action(path: str, error: str) -> dict:
    """Return the action describing an additional member that could not be collected.

    Args:
        path: the crate path the member would have had.
        error: the message describing why it is missing.

    """
    return {
        "@id": f"#member-{path.replace('/', '-')}-failed",
        "@type": "CreateAction",
        "name": f"Member {path} could not be collected",
        "object": ROOT,
        "instrument": {"@id": FORMATS_URL},
        "endTime": _now(),
        "actionStatus": FAILED,
        "error": error,
    }


def person_entity(
    crate: CrateContent,
    user_id: str | int,
    *,
    prefix: str = "#user-",
    name: str | None = None,
    email: str | None = None,
) -> dict:
    """Return a reference to the person entity of a user, adding or completing it in place.

    One entity per user id: what a source knows (a username, an e-mail) is added to what another source
    already recorded, so the audit log, the requests and the record's owner share one entity.

    Args:
        crate: the crate being built; its metadata document is changed in place.
        user_id: the user id.
        prefix: `@id` prefix of the entity.
        name: the username or full name, if a source provides one.
        email: the e-mail address, if a source provides one.

    """
    entity_id = f"{prefix}{user_id}"
    entity = _entity(crate, entity_id)
    if entity is None:
        entity = {"@id": entity_id, "@type": "Person"}
        crate.graph.append(entity)
    entity.setdefault("identifier", str(user_id))
    if name:
        entity.setdefault("name", name)
    if email:
        entity.setdefault("email", email)
    return {"@id": entity_id}


def repository_application() -> dict | None:
    """Return the repository as a `SoftwareApplication` entity, or None if `SITE_UI_URL` is not configured."""
    url = current_app.config.get("SITE_UI_URL")
    if not url:
        return None
    return {
        "@id": url,
        "@type": "SoftwareApplication",
        "name": current_app.config.get("THEME_SITENAME") or url,
        "url": url,
    }


def _action(action_id: str, action_type: str, name: str, end_time: str | None, repository: dict | None) -> dict:
    """Return a completed action on the root data entity, with the repository as its instrument if known."""
    action = {
        "@id": action_id,
        "@type": action_type,
        "name": name,
        "object": ROOT,
        "actionStatus": COMPLETED,
    }
    if end_time:
        action["endTime"] = end_time
    if repository:
        action["instrument"] = {"@id": repository["@id"]}
    return action


def _audit_shape(action: str) -> tuple[str, str]:
    """Return the (`@type`, `name`) of the RO-Crate action of an audit action id."""
    if action in AUDIT_ACTIONS:
        return AUDIT_ACTIONS[action]
    for suffix, shape in AUDIT_ACTION_SUFFIXES.items():
        if action.endswith(suffix):
            return shape
    return "UpdateAction", action


def _check_name(check_id: str, slug: str | None) -> str:
    """Return the human-readable name of a check run: the check and, if known, its community."""
    return f"Check {check_id} ({slug})" if slug else f"Check {check_id}"


def _check_result(result: dict | list | None) -> str:
    """Return the one-line result of a check run: `passed`, or the number of errors and warnings."""
    if isinstance(result, list):  # a run that records its findings as a list
        return f"{len(result)} findings" if result else "passed"
    errors = len((result or {}).get("errors") or [])
    warnings = len((result or {}).get("warnings") or [])
    if errors or warnings:
        return f"{errors} errors, {warnings} warnings"
    return "passed"


def _entity(crate: CrateContent, entity_id: str) -> dict | None:
    """Return the entity with `@id` `entity_id`, if the crate has one."""
    for entity in crate.graph:
        if entity.get("@id") == entity_id:
            return entity
    return None


def _owner(obj: dict) -> str | None:
    """Return the owner of the record, if the model exposes it (`parent.access.owned_by`, RDM models)."""
    owned_by = ((obj.get("parent") or {}).get("access") or {}).get("owned_by")
    if isinstance(owned_by, dict):
        owned_by = owned_by.get("user")
    return str(owned_by) if owned_by else None


def _formats_application() -> dict:
    """Return oarepo-formats, the software that produced the package, as a `SoftwareApplication` entity."""
    return {"@id": FORMATS_URL, "@type": "SoftwareApplication", "name": "oarepo-formats", "version": __version__}


def _now() -> str:
    """Return the current time as an ISO 8601 string, as an action's `endTime`."""
    return datetime.now(UTC).isoformat(timespec="seconds")
