# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""The task that writes an import's files after its response: it publishes the draft and mails the uploader.

[import_archive][oarepo_formats.rocrate.imports.tasks.import_archive] writes every planned file of an import
- the members of a **package behind a URL** (or of one in the request body), and the **referenced files**
of any import (downloaded from their URLs) - and finishes the import itself. A download gets a timeout,
retries, a size limit, a redirect checked against the allow-list and a verified checksum; nothing polls.

It **observes, it never overrides**: the user may publish, edit or delete the draft while it runs. A
published record, a deleted draft, or a draft whose `revision_id` changed after the import mean that the
user took over - nothing is published then.

Every failure reaches the uploader by mail, including the ones nobody expected: an exception that is not a
retry ends in a `rocrate-import.failed` mail with the reason `error`, because the task is
`ignore_result=True` and a traceback in the celery log would be the only trace of a lost import.
"""

from __future__ import annotations

from contextlib import nullcontext
from functools import partial
from typing import TYPE_CHECKING, Any

import requests
from celery import shared_task
from celery.exceptions import Retry
from flask import current_app
from invenio_access.permissions import SystemRoleNeed
from invenio_access.utils import get_identity
from invenio_accounts.models import User
from invenio_db.uow import UnitOfWork
from invenio_notifications.services.uow import NotificationOp
from invenio_notifications.tasks import broadcast_notification
from invenio_pidstore.errors import (
    PIDAlreadyExists,
    PIDDeletedError,
    PIDDoesNotExistError,
    PIDUnregistered,
)
from invenio_records_resources.proxies import current_service_registry
from invenio_records_resources.services.errors import FileKeyNotFoundError, PermissionDeniedError
from invenio_records_resources.services.files.transfer.base import TransferStatus
from marshmallow import ValidationError
from sqlalchemy.orm.exc import NoResultFound

from .archive import ArchiveError, ArchiveReader, PlannedFile, already_written, write_member
from .common import files_service_for, has_drafts
from .notifications import ImportCompleted, ImportFailed, ImportPublished, snapshot

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .notifications import ROCrateImportNotificationBuilder

GONE = "gone"
"""No draft and no record: the user deleted the import."""

PUBLISHED = "published"
"""No draft, but a published record: the user published it themselves."""

DRAFT = "draft"
"""The draft the import created is still there."""

RECORD = "record"
"""A model without drafts: the record the import created."""

RETRY_DELAY = 60
"""Seconds before the first retry of an archive import after a network error; doubled on every retry."""

NOT_FOUND_ERRORS = (PIDDoesNotExistError, PIDDeletedError, PIDUnregistered, NoResultFound)
"""What `read`/`read_draft`/`list_files` raise for an object that is not there any more.

Deliberately not `LookupError`: that is the base of `KeyError` and `IndexError`, so a bug in a service
component, a link template or `to_dict()` would be read as "the draft is gone" and the task would stop
without telling anyone.
"""


def _run_import(task: Any, payload: dict, body: Any) -> None:
    """Resolve the importing user and the service, and run `body(service, identity)` under the guard.

    The guard is the task's last resort, because it is `ignore_result=True` and a traceback in the celery
    log would be the only trace of a lost import:

    - a network error is retried with a growing delay while the task has retries left (`max_retries`);
    - when they run out, or for any other exception, the files nobody will write any more are deleted (a
      `pending` file would block the publish forever) and the failure is mailed.
    """
    identity = _user_identity(payload["user_id"])
    if identity is None:
        current_app.logger.warning(
            "RO-Crate import %s: the importing user %s is gone, nothing to report", payload["id_"], payload["user_id"]
        )
        return
    service = current_service_registry.get(payload["service_id"])
    try:
        body(service, identity)
    except Retry:
        raise
    except requests.RequestException as e:
        retries = task.request.retries
        if retries < (task.max_retries or 0):
            # the payload carries the failures recorded so far, so the next run reports them too
            raise task.retry(kwargs={"payload": payload}, exc=e, countdown=RETRY_DELAY * 2**retries) from e
        current_app.logger.warning("RO-Crate import %s: the files could not be read: %s", payload["id_"], e)
        failures = _discard_unwritten(service, identity, payload, str(e))
        reason = "archive" if payload.get("archive") else "files"
        _notify(ImportFailed, identity, payload, always=True, reason=reason, error=str(e), files=failures)
    except Exception as e:
        current_app.logger.exception("RO-Crate import %s failed", payload["id_"])
        failures = _discard_unwritten(service, identity, payload, str(e))
        _notify(ImportFailed, identity, payload, always=True, reason="error", error=str(e), files=failures)


def _discard_unwritten(service: Any, identity: Any, payload: dict, error: str) -> list[dict]:
    """Delete the planned files that were not written; return them with the failures recorded earlier."""
    failed = payload.get("failures") or []
    failed_keys = {failure["key"] for failure in failed}
    planned = [PlannedFile(**file) for file in payload.get("files") or [] if file["key"] not in failed_keys]
    return [*failed, *_discard(files_service_for(service), identity, payload, planned, error)]


def _complete(service: Any, identity: Any, payload: dict) -> None:
    """Finish a written import: observe it, then publish it or mail the outcome."""
    try:
        state, item = _observe(service, identity, payload["id_"])
    except PermissionDeniedError as e:
        _notify(ImportFailed, identity, payload, always=True, reason="permission", error=str(e))
        return
    if state in (GONE, PUBLISHED):
        current_app.logger.info("RO-Crate import %s: %s, nothing left to do", payload["id_"], state)
        return
    entries = _entries(service, identity, payload)
    if entries is None:
        return
    # every planned file was written or reported; an entry still pending or failed (one the user added, or
    # a failure a lost run did not record) must not be published as if the import were complete
    unfinished = [entry for entry in entries if entry.get("status") != TransferStatus.COMPLETED]
    if unfinished:
        _notify(ImportFailed, identity, payload, always=True, reason="files", files=unfinished)
        return
    _finish(service, identity, payload, item, entries)


def _finish(service: Any, identity: Any, payload: dict, item: dict | None, entries: list[dict]) -> None:
    """Act on a finished import: report it, or publish it first (and report that instead)."""
    if not payload["publish"]:
        _notify(ImportCompleted, identity, payload, item=item, files=entries)
        return
    if item is None or item.get("revision_id") != payload["revision_id"]:
        _notify(ImportFailed, identity, payload, always=True, reason="modified")
        return
    _publish(service, identity, payload, entries)


def _observe(service: Any, identity: Any, id_: str) -> tuple[str, dict | None]:
    """Return where the import stands and the object it is about, read once.

    A model without drafts has no draft: its record is the import's object. For a model with drafts the
    draft is read first; when it is gone, the record decides between "the user published it" and "the user
    deleted it".

    Raises:
        PermissionDeniedError: if the caller's rights changed; the caller reports it.

    """
    if not has_drafts(service):
        # a model without drafts publishes on create: `is_published` is its normal state, not "the user
        # published it while the task waited"
        return _read_record(service, identity, id_, draftless=True)
    try:
        return DRAFT, service.read_draft(identity, id_).to_dict()
    except NOT_FOUND_ERRORS:
        return _read_record(service, identity, id_)


def _read_record(service: Any, identity: Any, id_: str, *, draftless: bool = False) -> tuple[str, dict | None]:
    """Return `("record", record)`, `("published", record)` or `("gone", None)`."""
    try:
        item = service.read(identity, id_).to_dict()
    except NOT_FOUND_ERRORS:
        return GONE, None
    if draftless or not item.get("is_published"):
        return RECORD, item
    return PUBLISHED, item


def _entries(service: Any, identity: Any, payload: dict) -> list[dict] | None:
    """Return the files' serialized entries, or None when they can not be followed any more."""
    try:
        return files_service_for(service).list_files(identity, payload["id_"]).to_dict()["entries"]
    except PermissionDeniedError as e:
        _notify(ImportFailed, identity, payload, always=True, reason="permission", error=str(e))
        return None
    except NOT_FOUND_ERRORS:
        current_app.logger.info("RO-Crate import %s: the draft is gone, nothing to follow", payload["id_"])
        return None


def _publish(service: Any, identity: Any, payload: dict, entries: list[dict]) -> None:
    """Publish the draft, mailing the outcome; a publish that races the user's own is not an error."""
    try:
        with UnitOfWork() as uow:
            record = service.publish(identity, payload["id_"], uow=uow)
            if payload["notify_ok"]:
                uow.register(
                    NotificationOp(
                        ImportPublished.build(
                            identity.id, snapshot(record, payload["title"]), files=_described(entries)
                        )
                    )
                )
            uow.commit()
    except (ValidationError, PermissionDeniedError, PIDAlreadyExists) as e:
        if _observe(service, identity, payload["id_"])[0] == PUBLISHED:
            current_app.logger.info("RO-Crate import %s: published by the user during the import", payload["id_"])
            return
        _notify(ImportFailed, identity, payload, always=True, reason="publish", error=str(e))


def _notify(  # noqa: PLR0913 - the options of one mail
    builder: type[ROCrateImportNotificationBuilder],
    identity: Any,
    payload: dict,
    *,
    always: bool = False,
    item: Any = None,
    files: Iterable[dict] | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    """Send one notification, unless it is an "ok" one and `?notifications=off` was given."""
    if not always and not payload["notify_ok"]:
        return
    notification = builder.build(
        identity.id,
        snapshot(item, payload["title"]),
        files=_described(files or ()),
        reason=reason,
        error=error,
    )
    broadcast_notification.delay(notification.dumps())


def _described(entries: Iterable[dict]) -> list[dict]:
    """Return the files as the mails show them: the key, the status and the transfer's error."""
    return [
        {
            "key": entry.get("key"),
            "status": entry.get("status"),
            "error": entry.get("error") or (entry.get("transfer") or {}).get("error"),
        }
        for entry in entries
    ]


def _user_identity(user_id: str) -> Any:
    """Return the identity of the importing user, as a login would build it, or None if the user is gone."""
    try:
        user = User.query.filter_by(id=int(user_id)).one_or_none()
    except TypeError, ValueError:
        return None
    if user is None:
        return None
    identity = get_identity(user)
    identity.provides.update({SystemRoleNeed("authenticated_user"), SystemRoleNeed("any_user")})
    return identity


@shared_task(
    bind=True,
    ignore_result=True,
    max_retries=3,
    acks_late=True,
)
def import_archive(self: Any, payload: dict) -> None:
    """Write an import's planned files - archive members and downloads - then finish the import.

    One task per import: it opens the archive once (if the plan has members), writes every planned file (a
    unit of work per file, so completed files survive a later failure), and finishes the import - no
    polling, because this task knows when it is done.

    A file whose bytes do not match the package, or that can not be read, is deleted and listed in the
    failure mail; the record is then left unpublished. A dropped connection is retried (`max_retries`, with
    a growing delay) and only the files not written yet are written again; when the retries run out, those
    files are deleted and the failure is mailed.

    Args:
        self: the bound task, used for `retry`.
        payload: `service_id`, `id_`, `user_id`, `revision_id`, `publish`, `notify_ok` and `title` of the
            import, plus `files` (the plan: an archive member's path, or a URL to download) and, for a
            package behind a URL, `archive` (the URL and its ETag) - a plain dict, so that celery's JSON
            serializer carries it.

    """
    _run_import(self, payload, partial(_write_archive, payload=payload))


def _write_archive(service: Any, identity: Any, payload: dict) -> None:
    """Write the planned files, then finish the import."""
    failures, reason = _extract_files(identity, service, payload)
    if failures:
        _notify(ImportFailed, identity, payload, always=True, reason=reason, files=failures)
        return
    _complete(service, identity, payload)


def _extract_files(identity: Any, service: Any, payload: dict) -> tuple[list[dict], str | None]:
    """Write every planned file; return the failures and the reason the mail gives.

    The reason is the first that applies of `archive` (the archive broke: its remaining members fail with it,
    the downloads still run), `error` (a file failed unexpectedly), `files` (a download failed, or an entry
    was deleted meanwhile) and `checksum` (a digest mismatch). A network error is not handled here: it
    propagates, and the task retries it - with the failures recorded so far in the payload, so that the next
    run reports them instead of losing them.

    The archive is opened again here - the request read only its table of contents and the metadata - and
    every member is a ranged read of the object, so nothing is held in memory between the two phases. A plan
    of downloads only (a detached crate) opens no archive at all.
    """
    files_service = files_service_for(service)
    # the failures of earlier runs of this task (it was retried after a network error): those files are
    # deleted already, and are reported, not written again
    failures: list[dict] = payload.setdefault("failures", [])
    failed_keys = {failure["key"] for failure in failures}
    planned = [PlannedFile(**file) for file in payload["files"] if file["key"] not in failed_keys]
    reasons: set[str] = set(payload.setdefault("reasons", []))

    def fail(files: list[PlannedFile], error: str, reason: str) -> None:
        failures.extend(_discard(files_service, identity, payload, files, error))
        reasons.add(reason)
        payload["reasons"] = sorted(reasons)

    archive = payload.get("archive")
    reader = None
    if archive and any(file.path for file in planned):
        try:
            reader = ArchiveReader.from_url(archive["url"], etag=archive.get("etag"))
        except ArchiveError as e:
            # the archive changed, expired or is unreachable: no member can be written (the downloads can)
            current_app.logger.warning("RO-Crate import %s: the archive failed: %s", payload["id_"], e)
            fail([file for file in planned if file.path], str(e), "archive")
            planned = [file for file in planned if not file.path]
    with reader if reader is not None else nullcontext():
        for file in planned:
            if file.key in failed_keys:  # a member discarded with the archive, below
                continue
            try:
                error, reason = _write_planned(files_service, identity, payload, reader, file)
            except NOT_FOUND_ERRORS:
                current_app.logger.info("RO-Crate import %s: the draft is gone, stopping", payload["id_"])
                return [], None
            if error is None:
                continue
            if reason == "archive":
                # a member that can not be read means the archive broke: the other members fail with it
                failing = [other for other in planned if other.path and other.key not in failed_keys]
                failed_keys.update(other.key for other in failing)
            else:
                failing = [file]
            fail(failing, error, reason)
    return failures, _reason_of_mail(reasons)


def _write_planned(
    files_service: Any, identity: Any, payload: dict, reader: ArchiveReader | None, file: PlannedFile
) -> tuple[str | None, str]:
    """Write one planned file; return why it is not in the draft and the mail's reason, or `(None, "")`.

    The reason is `archive` for a member that can not be read (the archive broke), `files` for a download
    that fails (an HTTP error, a refused host, a size - that file's own problem) or an entry the user
    deleted meanwhile, `checksum` for a digest mismatch and `error` for anything unexpected.

    Raises:
        requests.RequestException: a network error, which the task retries.
        PIDDoesNotExistError: (and the other `NOT_FOUND_ERRORS`) when the draft itself is gone.

    """
    try:
        with UnitOfWork() as uow:
            mismatch = write_member(files_service, identity, payload["id_"], reader, file, uow=uow)
            uow.commit()
    except Retry, requests.RequestException:
        raise
    except ArchiveError as e:
        current_app.logger.warning("RO-Crate import %s: %s failed: %s", payload["id_"], file.key, e)
        return str(e), "files" if file.url else "archive"
    except FileKeyNotFoundError:
        current_app.logger.info("RO-Crate import %s: %s is no longer in the draft", payload["id_"], file.key)
        return "The file is no longer in the draft", "files"
    except NOT_FOUND_ERRORS:
        raise
    except Exception as e:
        # the file's unit of work rolled back, so its entry is `pending` again: the caller deletes it
        current_app.logger.exception("RO-Crate import %s: %s failed", payload["id_"], file.key)
        return str(e), "error"
    if mismatch:  # a digest mismatch: `write_member` deleted the entry already
        return mismatch, "checksum"
    return None, ""


REASON_PRIORITY = ("archive", "error", "files", "checksum")
"""The reason a failure mail gives when files failed for several: the first of these that applies."""


def _reason_of_mail(reasons: set[str]) -> str | None:
    """Return the one reason the failure mail states, for the reasons files failed for."""
    return next((reason for reason in REASON_PRIORITY if reason in reasons), None)


def _discard(files_service: Any, identity: Any, payload: dict, planned: list[PlannedFile], error: str) -> list[dict]:
    """Delete the entries of members that will not be written, and describe them for the failure mail.

    A pending entry left behind would block the draft from ever being published, and nothing would follow
    it any more: an archive import does not poll, so the deletion is what "failed" means here. An entry
    that was written already (an earlier member, or an earlier run) is kept.
    """
    failures = []
    for file in planned:
        try:
            if already_written(files_service, identity, payload["id_"], file.key):
                continue  # a file written before the failure stays
            files_service.delete_file(identity, payload["id_"], file.key)
        except (*NOT_FOUND_ERRORS, FileKeyNotFoundError):
            current_app.logger.info("RO-Crate import %s: %s is already gone", payload["id_"], file.key)
        failures.append({"key": file.key, "status": "failed", "error": error})
    return failures
