# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""Tests of the RO-Crate import task: what it decides from the state of the draft and its files.

Tested with a stubbed service and a captured `broadcast_notification`, so its decisions are pinned without
a database, a search index or celery. Only the cases the REST tests can not stage are here (the user acting
while the task runs, refusals and errors of the publish, a model without drafts); the outcomes a request
reaches - the completed, published and failed mails, `?notifications=off` - are tested in
`tests/api_tests/test_rocrate_import.py`.
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from typing import Self

import pytest
import requests
from flask import Flask
from invenio_pidstore.errors import PIDAlreadyExists, PIDDoesNotExistError
from invenio_records_resources.services.errors import FileKeyNotFoundError, PermissionDeniedError
from marshmallow import ValidationError

from oarepo_formats.rocrate.imports import tasks
from oarepo_formats.rocrate.imports.archive import ArchiveError

DRAFT = {
    "id": "abc",
    "links": {"self_html": "https://example.org/drafts/abc"},
    "is_draft": True,
    "is_published": False,
    "revision_id": 3,
}
"""The draft the import created, as `read_draft` returns it."""

RECORD = {
    "id": "abc",
    "links": {"self_html": "https://example.org/records/abc"},
    "is_draft": False,
    "is_published": True,
}
"""The published record, as `read` returns it."""


class Item:
    """A result item: `to_dict()` is all the task uses."""

    def __init__(self, data: dict):
        """Wrap the dict a service would return."""
        self._data = data

    def to_dict(self) -> dict:
        """Return the dict."""
        return self._data


class Answers:
    """A record service stub that answers as configured."""

    id = "test-service"

    def __init__(self, *, record=None):
        """Configure what `read` answers (a dict, or an exception to raise)."""
        self.record = record

    @staticmethod
    def _answer(value) -> Item:
        """Return the value as an item, or raise it when it is an exception."""
        if isinstance(value, Exception):
            raise value
        return Item(value)

    def read(self, identity, id_) -> Item:
        """Return the record."""
        return self._answer(self.record)


class Service(Answers):
    """A service of a model with drafts: it reads the draft and publishes."""

    def __init__(self, *, draft=None, record=None, publish=None):
        """Configure what `read_draft`, `read` and `publish` answer."""
        super().__init__(record=record)
        self.draft = draft
        self.publish_result = publish
        self.published: list[str] = []

    def read_draft(self, identity, id_) -> Item:
        """Return the draft."""
        return self._answer(self.draft)

    def publish(self, identity, id_, uow=None) -> Item:
        """Record the publish and return the configured answer."""
        if isinstance(self.publish_result, Exception):
            raise self.publish_result
        self.published.append(id_)
        return self._answer(self.publish_result)


class Files:
    """A files service stub listing the given entries."""

    def __init__(self, entries=None, error=None):
        """Configure the entries to list, or the error to raise."""
        self.entries = entries or []
        self.error = error

    def list_files(self, identity, id_) -> Item:
        """Return the entries."""
        if self.error:
            raise self.error
        return Item({"entries": self.entries})


class Broadcast:
    """Collects what would be sent, instead of queueing it."""

    def __init__(self):
        """Start with nothing collected."""
        self.notifications: list[dict] = []

    def delay(self, payload) -> None:
        """Collect one dumped notification."""
        self.notifications.append(payload)


class Uow:
    """A unit of work that runs the registered operations at once."""

    def __enter__(self) -> Self:
        """Enter the block."""
        return self

    def __exit__(self, *exc) -> bool:
        """Leave the block; nothing is rolled back."""
        return False

    def register(self, op) -> None:
        """Run the operation's post-commit hook, as a real commit would."""
        op.on_post_commit(self)

    def commit(self) -> None:
        """Do nothing: the operations already ran."""


@pytest.fixture
def app():
    """Return a bare application: the task needs a config and a logger, nothing else."""
    return Flask("test")


@pytest.fixture
def sent(monkeypatch):
    """Capture every notification, including the one the publish registers on its unit of work."""
    from invenio_notifications.services import uow as notification_uow

    broadcast = Broadcast()
    monkeypatch.setattr(tasks, "broadcast_notification", broadcast)
    monkeypatch.setattr(notification_uow, "broadcast_notification", broadcast)
    monkeypatch.setattr(tasks, "UnitOfWork", Uow)
    return broadcast


@pytest.fixture
def run(app, sent, monkeypatch):
    """Return a callable that runs the task's finishing step with the given service and files."""
    monkeypatch.setattr(
        tasks, "_user_identity", lambda user_id: None if user_id == "gone" else SimpleNamespace(id=int(user_id))
    )
    # `Service` stands in for a drafts service, `Answers` for the service of a model without drafts
    monkeypatch.setattr(tasks, "has_drafts", lambda service: isinstance(service, Service))

    def _run(service: Service, files: Files, **payload):
        monkeypatch.setattr(tasks, "current_service_registry", SimpleNamespace(get=lambda service_id: service))
        monkeypatch.setattr(tasks, "files_service_for", lambda record_service: files)
        base = {
            "service_id": service.id,
            "id_": "abc",
            "user_id": "1",
            "publish": False,
            "notify_ok": True,
            "title": "Example Title",
            "files": [],  # nothing planned: the failure paths have nothing to delete
            "revision_id": 3,
        }
        base.update(payload)
        with app.app_context():
            tasks._run_import(tasks.import_archive, base, partial(tasks._complete, payload=base))

    return _run


def types(sent: Broadcast) -> list[str]:
    """Return the types of the captured notifications."""
    return [notification["type"] for notification in sent.notifications]


def completed_entry(key: str = "data.csv") -> dict:
    """Return a file entry whose transfer finished."""
    return {"key": key, "status": "completed", "transfer": {"type": "L"}}


GONE = PIDDoesNotExistError("rec", "abc")
COMPLETED = [completed_entry()]
PUBLISH = {"publish": True}


@pytest.mark.parametrize(
    ("service", "files", "payload", "mails", "reason", "published"),
    [
        pytest.param(
            Service(draft={**DRAFT, "revision_id": 4}, publish=RECORD),
            COMPLETED,
            PUBLISH,
            ["failed"],
            "modified",
            [],
            id="edited-draft-is-not-published",
        ),
        pytest.param(Service(draft=GONE, record=RECORD), COMPLETED, PUBLISH, [], None, [], id="published-by-the-user"),
        pytest.param(Service(draft=GONE, record=GONE), COMPLETED, {}, [], None, [], id="deleted-by-the-user"),
        pytest.param(
            Service(draft=GONE, record=RECORD, publish=PIDAlreadyExists("rec", "abc")),
            COMPLETED,
            PUBLISH,
            [],
            None,
            [],
            id="publish-races-the-user",
        ),
        pytest.param(
            Service(draft=DRAFT, publish=ValidationError("One or more files have not completed their transfer")),
            COMPLETED,
            PUBLISH,
            ["failed"],
            "publish",
            [],
            id="publish-refused",
        ),
        pytest.param(
            Service(draft=DRAFT, publish=RuntimeError("the pid provider is down")),
            COMPLETED,
            PUBLISH,
            ["failed"],
            "error",
            [],
            id="unexpected-publish-error",
        ),
        pytest.param(
            Service(draft=PermissionDeniedError("read_draft")),
            COMPLETED,
            {},
            ["failed"],
            "permission",
            [],
            id="permission-lost",
        ),
        # `LookupError` is not "not found": a bug is reported, not read as a deleted draft
        pytest.param(Service(draft=KeyError("revision_id")), COMPLETED, {}, ["failed"], "error", [], id="key-error"),
        pytest.param(Service(draft=DRAFT), COMPLETED, {"user_id": "gone"}, [], None, [], id="gone-user"),
        pytest.param(Answers(record=RECORD), COMPLETED, {}, ["completed"], None, [], id="model-without-drafts"),
        # an entry the task did not write (the user added it, or a lost run did not report it) is not published
        pytest.param(
            Service(draft=DRAFT, publish=RECORD),
            [*COMPLETED, {"key": "other.csv", "status": "pending"}],
            PUBLISH,
            ["failed"],
            "files",
            [],
            id="an-unfinished-entry-is-not-published",
        ),
    ],
)
def test_the_task_decides(run, sent, app, service, files, payload, mails, reason, published):
    """What the task does with what it finds - the cases the REST tests can not stage."""
    run(service, Files(files), **payload)

    assert types(sent) == [f"rocrate-import.{mail}" for mail in mails]
    if reason:
        assert sent.notifications[0]["context"]["reason"] == reason
    assert getattr(service, "published", []) == published


#
# writing the planned files: what a failure takes with it, and what a retried run remembers
#
class WritingFiles:
    """A files service stub that records what the task deletes."""

    def __init__(self):
        """Start with nothing deleted."""
        self.deleted: list[str] = []

    def delete_file(self, identity, id_, key, uow=None) -> None:
        """Record the deletion of a file that will not be written."""
        self.deleted.append(key)


@pytest.fixture
def extract(app, monkeypatch):
    """Return a callable that runs `_extract_files` with `outcomes`: key -> what writing it does."""
    files = WritingFiles()
    monkeypatch.setattr(tasks, "files_service_for", lambda service: files)
    monkeypatch.setattr(tasks, "UnitOfWork", Uow)
    monkeypatch.setattr(tasks, "already_written", lambda *args: False)

    def _extract(payload: dict, outcomes: dict):
        def write_member(files_service, identity, id_, reader, file, uow=None):
            outcome = outcomes[file.key]  # a file the run must not write is not in `outcomes`: a KeyError
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(tasks, "write_member", write_member)
        with app.app_context():
            failures, reason = tasks._extract_files(SimpleNamespace(id=1), None, payload)
        return [failure["key"] for failure in failures], reason, files.deleted

    return _extract


class FileGoneError(FileKeyNotFoundError):
    """The files service's "no such file", without the translated message (the bare app has no babel)."""

    def __init__(self):
        """Create the error."""
        Exception.__init__(self, "no such file")


def download(key: str) -> dict:
    return {"key": key, "path": None, "size": None, "url": f"https://x.org/{key}", "md5": None, "sha512": None}


def member(key: str) -> dict:
    return {"key": key, "path": f"data/{key}", "size": 1, "url": None, "md5": None, "sha512": None}


def test_a_retried_run_reports_the_failures_of_the_earlier_one(extract):
    """A retried run still reports what the earlier run failed.

    Run 1 fails `a` for good and is retried for `b`'s dropped connection; run 2 (the same payload, as the
    retry carries it) writes `b`, does not touch `a` again, and still reports it. Not testable through the
    REST API: an eager task's `retry` raises the error instead of running it again.
    """
    payload = {"id_": "abc", "files": [download("a.csv"), download("b.csv")]}
    with pytest.raises(requests.ConnectionError):
        extract(payload, {"a.csv": ArchiveError("HTTP 404"), "b.csv": requests.ConnectionError("reset")})

    assert extract(payload, {"b.csv": None}) == (["a.csv"], "files", ["a.csv"])


def test_a_file_deleted_meanwhile_fails_alone(extract):
    """A missing entry is that file's failure, not "the draft is gone": the others are still written."""
    payload = {"id_": "abc", "files": [download("a.csv"), download("b.csv")]}

    keys, reason, _ = extract(payload, {"a.csv": FileGoneError(), "b.csv": None})

    assert (keys, reason) == (["a.csv"], "files")


def test_a_broken_archive_takes_its_members_but_not_the_downloads(extract):
    payload = {"id_": "abc", "files": [member("m1.csv"), member("m2.csv"), download("d.csv")]}

    keys, reason, deleted = extract(payload, {"m1.csv": ArchiveError("the archive changed"), "d.csv": None})

    assert (keys, reason, deleted) == (["m1.csv", "m2.csv"], "archive", ["m1.csv", "m2.csv"])


def test_the_draft_gone_stops_the_run_without_failures(extract):
    payload = {"id_": "abc", "files": [download("a.csv"), download("b.csv")]}

    assert extract(payload, {"a.csv": GONE}) == ([], None, [])
