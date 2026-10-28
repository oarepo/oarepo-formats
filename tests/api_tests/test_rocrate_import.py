# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""RO-Crate import through the REST API, for all three transports.

| Transport | Body | Files |
|---|---|---|
| detached crate | `application/ld+json` | downloaded from their URLs by a task after the response |
| package in the body | `application/zip` | written in the request (referenced files by a task) |
| package behind a URL | `application/json` `{"url": ...}` | written by a task, by HTTP ranges |

The crates and packages are **produced by this package's own exports** of a record, so the round trip is
real. The remote hosts (the crate's file URLs, the archive URL) are `responses` fakes; celery runs eagerly,
so the downloads, the archive task, their retries and the mails have all happened when the request returns.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from types import SimpleNamespace

import pytest
import responses
from invenio_access.permissions import system_identity
from invenio_records_permissions.generators import SystemProcess
from marshmallow import Schema, fields
from sqlalchemy import func, select

from oarepo_formats.rocrate import IDENTITY_IMPORT
from oarepo_formats.rocrate.imports import jsonld
from oarepo_formats.rocrate.model import (
    ROCRATE_AIP_MIMETYPE,
    ROCRATE_BAGIT_MIMETYPE,
    ROCRATE_MIMETYPE,
    ROCRATE_ZIP_MIMETYPE,
)
from tests.conftest import FormatsTestPermissionPolicy
from tests.rocrate.documents import root_of
from tests.rocrate.imports.packages import RangeServer, big_package

IMPORT = "/import/rocrate"
FILE_CONTENT = b"a,b\n1,2\n"
FETCH_HOST = "127.0.0.1:5000"
"""The netloc of the exported crate's file URLs - the test app's own links, port included."""
FILE_URL = re.compile(rf"https://{re.escape(FETCH_HOST)}/api/.*/files/data\.csv/content$")

LD_JSON = "application/ld+json"
ZIP = "application/zip"
JSON = "application/json"


@pytest.fixture(scope="module")
def app_config(app_config):
    """Allow the two fake hosts and keep the mails in the outbox."""
    app_config["RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS"] = [FETCH_HOST, RangeServer.url.split("/")[2]]
    app_config["MAIL_SUPPRESS_SEND"] = True
    app_config["MAIL_DEFAULT_SENDER"] = "test@example.org"
    app_config["MAIL_DEFAULT_REPLY_TO"] = "test@example.org"
    return app_config


@pytest.fixture(scope="module")
def celery_config(celery_config):
    """Run the import's tasks in the request, retries included."""
    return {**celery_config, "task_always_eager": True, "task_eager_propagates": True}


def export(client, record_url: str, mimetype: str) -> bytes:
    response = client.get(record_url, headers={"Accept": mimetype})
    assert response.status_code == 200, response.data
    return response.data


# the record is set up by the tests themselves (`record_with_file`), so that a table can build only the export
# a case needs (`request.getfixturevalue`) without creating the record in the middle of the test
@pytest.fixture
def crate(client, record_url) -> bytes:
    """Return the detached RO-Crate export of a record with one file."""
    return export(client, record_url, ROCRATE_MIMETYPE)


@pytest.fixture
def crate_zip(client, record_url) -> bytes:
    """Return the plain attached RO-Crate (ZIP) export of the same record."""
    return export(client, record_url, ROCRATE_ZIP_MIMETYPE)


@pytest.fixture
def bagit_zip(client, record_url) -> bytes:
    """Return the BagIt export of the same record (it carries `metadata/record.json` and both manifests)."""
    return export(client, record_url, ROCRATE_BAGIT_MIMETYPE)


def fetch_callback(hosts):
    """Answer the crate's file URL the way the test configured it."""

    def answer(request):
        return hosts.fetch_status, hosts.fetch_headers, hosts.fetch_body

    return answer


@pytest.fixture(autouse=True)
def remote():
    """Fake the two remote hosts: the crate's file URL (`fetch_*`) and a static archive host (`archive`)."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        hosts = SimpleNamespace(fetch_status=200, fetch_body=FILE_CONTENT, fetch_headers={}, archive=RangeServer())
        mocked.add_callback(responses.GET, FILE_URL, callback=fetch_callback(hosts))
        hosts.archive.register(mocked)
        hosts.mock = mocked  # a test registers further URLs on it
        yield hosts


def post(client, records_url: str, data: bytes, content_type: str, query: str = ""):
    return client.post(f"{records_url}{IMPORT}{query}", data=data, content_type=content_type)


def post_url(client, records_url: str, query: str = "", url: str = RangeServer.url):
    return post(client, records_url, json.dumps({"url": url}).encode(), JSON, query)


def draft_count(formats_model, db) -> int:
    """How many drafts the database holds - a refused import must not leave one behind."""
    return db.session.execute(select(func.count()).select_from(formats_model.Draft.model_cls)).scalar_one()


def imported(client, records_url: str, record_id: str) -> tuple[dict, dict[str, str], dict[str, bytes]]:
    """Return what the import left: the record (published, else the draft), `{key: status}`, `{key: content}`."""
    base = f"{records_url}/{record_id}"
    if client.get(base).status_code != 200:  # not published: the draft is the import
        base = f"{base}/draft"
    record = client.get(base).get_json()
    entries = (client.get(f"{base}/files").get_json() or {}).get("entries") or []
    statuses = {entry["key"]: entry["status"] for entry in entries}
    contents = {key: client.get(f"{base}/files/{key}/content").data for key, s in statuses.items() if s == "completed"}
    return record, statuses, contents


def subjects(outbox) -> list[str]:
    return [message.subject for message in outbox]


#
# the archive mutations the tables use
#
def rezip(content: bytes, replace: dict[str, bytes] | None = None, drop: tuple[str, ...] = ()) -> bytes:
    """Return the archive with some members replaced or dropped."""
    replace = replace or {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            if not info.is_dir() and info.filename not in drop:
                target.writestr(info.filename, replace.get(info.filename, source.read(info.filename)))
    return buffer.getvalue()


def member(content: bytes, suffix: str) -> str:
    """Return the archive path ending with `suffix` (a bag's members are below its `<id>/`)."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return next(name for name in archive.namelist() if name.endswith(suffix))


def edit_crate(content: bytes, edit) -> bytes:
    """Return the package with `edit(document)` applied to its RO-Crate metadata."""
    path = member(content, "ro-crate-metadata.json")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        document = json.loads(archive.read(path))
    edit(document)
    return rezip(content, {path: json.dumps(document).encode()})


def wrong_md5(content: bytes) -> bytes:
    def edit(document):
        for entity in document["@graph"]:
            if "md5" in entity:
                entity["md5"] = "0" * 32

    return edit_crate(content, edit)


def wrong_sha512(content: bytes) -> bytes:
    return rezip(content, {member(content, "manifest-sha512.txt"): f"{'0' * 128}  data/data/data.csv\n".encode()})


def broken_files_json(content: bytes) -> bytes:
    """Add a `files.json` that is not JSON: it may hide files, so it can not be ignored."""
    crate_root = member(content, "ro-crate-metadata.json").removesuffix("ro-crate-metadata.json")
    buffer = io.BytesIO(rezip(content))
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr(f"{crate_root}metadata/repository/files.json", b"{not json")
    return buffer.getvalue()


def without_files(content: bytes) -> bytes:
    def edit(document):
        for entity in document["@graph"]:
            entity.pop("hasPart", None)

    return rezip(edit_crate(content, edit), drop=(member(content, "data/data/data.csv"),))


def without_member(content: bytes) -> bytes:
    return rezip(content, drop=(member(content, "data/data.csv"),))


#
# 1. everything that is refused leaves nothing behind
#
def crate_body(content_type: str = LD_JSON, edit=None):
    def build(get, remote):
        document = json.loads(get("crate"))
        return json.dumps(edit(document) if edit else document).encode(), content_type

    return build


def package_body(fixture: str, mutate):
    return lambda get, remote: (mutate(get(fixture)), ZIP)


def url_body(url: str = RangeServer.url, *, accept_ranges: bool = True):
    def build(get, remote):
        remote.archive.content = get("bagit_zip")
        remote.archive.accept_ranges = accept_ranges
        return json.dumps({"url": url}).encode(), JSON

    return build


def denied(action: str) -> dict:
    return {"policy": {action: (SystemProcess(),)}}


@pytest.mark.parametrize(
    ("build", "query", "patch", "status", "message"),
    [
        pytest.param(crate_body("text/plain"), "", {}, 415, None, id="foreign-media-type"),
        pytest.param(crate_body(JSON), "", {}, 400, "url", id="json-without-a-url"),
        pytest.param(
            crate_body('application/ld+json; profile="https://example.org/other/1.0"'),
            "",
            {},
            415,
            None,
            id="foreign-profile",
        ),
        pytest.param(crate_body(edit=lambda document: {"@graph": []}), "", {}, 400, "RO-Crate", id="invalid-crate"),
        pytest.param(
            crate_body(),
            "",
            {"config": {"RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS": []}},
            400,
            "not allowed",
            id="fetch-domain-not-allowed",
        ),
        pytest.param(crate_body(), "?notifications=bogus", {}, 400, "notifications", id="notifications-typo"),
        pytest.param(crate_body(), "?publish=ture", {}, 400, "publish", id="publish-typo"),
        pytest.param(crate_body(), "", denied("can_create"), 403, None, id="create-denied"),
        # the rule the import is built on: the record and its files are created in one unit of work
        pytest.param(crate_body(), "", denied("can_draft_create_files"), 403, None, id="files-denied-rolls-back"),
        pytest.param(crate_body(), "?publish", denied("can_publish"), 403, None, id="publish-denied"),
        pytest.param(package_body("crate_zip", without_member), "", {}, 400, "has no member", id="missing-member"),
        pytest.param(package_body("bagit_zip", wrong_sha512), "", {}, 400, "sha512", id="sha512-mismatch"),
        pytest.param(package_body("crate_zip", wrong_md5), "", {}, 400, "md5", id="md5-mismatch-strict"),
        pytest.param(package_body("bagit_zip", broken_files_json), "", {}, 400, "files.json", id="broken-files-json"),
        pytest.param(url_body(accept_ranges=False), "", {}, 400, "Accept-Ranges", id="url-without-ranges"),
        pytest.param(url_body("https://evil.example.org/p.zip"), "", {}, 400, "not allowed", id="url-not-allowed"),
    ],
)
def test_a_refused_import_creates_nothing(
    request,
    app,
    record_with_file,
    import_client,
    records_url,
    remote,
    formats_model,
    db,
    monkeypatch,
    build,
    query,
    patch,
    status,
    message,
):
    data, content_type = build(request.getfixturevalue, remote)
    for key, value in patch.get("config", {}).items():
        monkeypatch.setitem(app.config, key, value)
    for action, generators in patch.get("policy", {}).items():
        monkeypatch.setattr(FormatsTestPermissionPolicy, action, generators)
    before = draft_count(formats_model, db)

    response = post(import_client, records_url, data, content_type, query)

    assert response.status_code == status, response.data
    if message:
        assert message in response.get_data(as_text=True)
    assert draft_count(formats_model, db) == before


#
# 2. a detached crate: the files are fetched from their URLs
#
@pytest.mark.parametrize(
    ("query", "publish", "mail"),
    [
        pytest.param("", None, "RO-Crate import finished: Example Title", id="draft"),
        pytest.param("?publish", "scheduled", "RO-Crate import published: Example Title", id="published-after-fetch"),
        pytest.param("?notifications=off", None, None, id="notifications-off"),
    ],
)
def test_a_crate_is_imported_and_its_files_downloaded(app, import_client, records_url, crate, query, publish, mail):
    with app.extensions["mail"].record_messages() as outbox:
        response = post(import_client, records_url, crate, LD_JSON, query)

    assert response.status_code == 201, response.data
    body = response.get_json()
    # the metadata came through the model's own DataCite import, which keeps the title only
    assert body["record"]["metadata"]["title"] == "Example Title"
    assert "description" not in body["record"]["metadata"]
    assert body["import"]["publish"] == publish
    record, statuses, contents = imported(import_client, records_url, body["record"]["id"])
    assert record["is_published"] is bool(publish)
    assert statuses == {"data.csv": "completed"}
    assert contents == {"data.csv": FILE_CONTENT}
    if mail:
        assert subjects(outbox) == [mail]
        assert record["links"]["self_html"] in outbox[0].body
    else:
        assert outbox == []


@pytest.mark.parametrize(("query", "publish"), [("", None), ("?publish", "published")])
def test_a_crate_without_files_is_finished_in_the_request(app, import_client, records_url, crate, query, publish):
    """Nothing to fetch: the publish (if asked for) runs in the request, and no mail is needed."""
    document = json.loads(crate)
    root_of(document).pop("hasPart", None)

    with app.extensions["mail"].record_messages() as outbox:
        response = post(import_client, records_url, json.dumps(document).encode(), LD_JSON, query)

    assert response.status_code == 201, response.data
    assert response.get_json()["import"]["publish"] == publish
    assert response.get_json()["record"]["is_published"] is bool(publish)
    assert outbox == []


@pytest.mark.parametrize("query", ["?publish", "?publish&notifications=off"])
def test_a_failed_download_leaves_the_draft_and_is_always_mailed(app, import_client, records_url, crate, remote, query):
    remote.fetch_status, remote.fetch_body = 404, b"not found"

    with app.extensions["mail"].record_messages() as outbox:
        response = post(import_client, records_url, crate, LD_JSON, query)

    assert response.status_code == 201, response.data
    record, statuses, _ = imported(import_client, records_url, response.get_json()["record"]["id"])
    assert record["is_published"] is False
    # the file that failed is deleted, so that the draft stays publishable by hand
    assert statuses == {}
    assert subjects(outbox) == ["RO-Crate import failed: Example Title"]
    # a one-line reason of ours, not the remote host's error page
    for text in ("a file could not be fetched", "data.csv", "HTTP 404"):
        assert text in outbox[0].body
    assert "not found" not in outbox[0].body


def without_md5(crate: bytes) -> bytes:
    """Return the crate with a wrong `md5` on its files."""
    document = json.loads(crate)
    for entity in document["@graph"]:
        if "md5" in entity:
            entity["md5"] = "0" * 32
    return json.dumps(document).encode()


def download_fails(app, client, records_url: str, body: bytes) -> str:
    """Import the crate, expect the failure mail, and return its body - the draft has no files left."""
    with app.extensions["mail"].record_messages() as outbox:
        response = post(client, records_url, body, LD_JSON)

    assert response.status_code == 201, response.data
    record, statuses, _ = imported(client, records_url, response.get_json()["record"]["id"])
    assert record["is_published"] is False
    assert statuses == {}
    assert subjects(outbox) == ["RO-Crate import failed: Example Title"]
    return outbox[0].body


def test_a_download_that_redirects_out_of_the_allow_list_is_refused(app, import_client, records_url, crate, remote):
    """A redirect must not be a way around `RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS`."""
    remote.fetch_status, remote.fetch_headers = 302, {"Location": "https://evil.example.org/data.csv"}

    assert "evil.example.org' is not allowed" in download_fails(app, import_client, records_url, crate)


def test_a_download_longer_than_the_crate_says_is_refused(app, import_client, records_url, crate, remote):
    remote.fetch_body = FILE_CONTENT * 10

    assert "longer than its stated size" in download_fails(app, import_client, records_url, crate)


def test_a_downloaded_file_must_match_the_crate_checksum(app, import_client, records_url, crate, remote):
    assert "does not match its checksum" in download_fails(app, import_client, records_url, without_md5(crate))


#
# 3. an attached package: in the body, or behind a URL
#
@pytest.mark.parametrize(
    ("package", "mutate", "transport", "query", "config", "publish", "files", "mails"),
    [
        pytest.param("crate_zip", None, "body", "", {}, None, ["data.csv"], [], id="crate-zip-in-the-body"),
        pytest.param(
            "bagit_zip", None, "body", "?publish", {}, "published", ["data.csv"], [], id="bag-in-the-body-published"
        ),
        # a stored md5 that mismatches means the source was corrupt already: `log` imports anyway
        pytest.param(
            "crate_zip",
            wrong_md5,
            "body",
            "",
            {"OAREPO_ROCRATE_IMPORT_VERIFY": "log"},
            None,
            ["data.csv"],
            [],
            id="md5-mismatch-logged",
        ),
        pytest.param(
            "bagit_zip",
            None,
            "url",
            "",
            {},
            None,
            ["data.csv"],
            ["RO-Crate import finished: Example Title"],
            id="bag-at-a-url",
        ),
        pytest.param(
            "bagit_zip",
            None,
            "url",
            "?publish",
            {},
            "scheduled",
            ["data.csv"],
            ["RO-Crate import published: Example Title"],
            id="bag-at-a-url-published",
        ),
        pytest.param(
            "bagit_zip", without_files, "url", "?publish", {}, "published", [], [], id="no-files-at-a-url-published"
        ),
    ],
)
def test_a_package_is_imported(
    request,
    app,
    record_with_file,
    import_client,
    records_url,
    remote,
    monkeypatch,
    package,
    mutate,
    transport,
    query,
    config,
    publish,
    files,
    mails,
):
    content = request.getfixturevalue(package)
    content = mutate(content) if mutate else content
    for key, value in config.items():
        monkeypatch.setitem(app.config, key, value)

    with app.extensions["mail"].record_messages() as outbox:
        if transport == "body":
            response = post(import_client, records_url, content, ZIP, query)
        else:
            remote.archive.content = content
            response = post_url(import_client, records_url, query)

    assert response.status_code == 201, response.data
    assert response.get_json()["import"]["publish"] == publish
    record, statuses, contents = imported(import_client, records_url, response.get_json()["record"]["id"])
    assert record["is_published"] is bool(publish)
    # the package's metadata members (README, record.json, ...) are not record files
    assert statuses == dict.fromkeys(files, "completed")
    assert contents == dict.fromkeys(files, FILE_CONTENT)
    assert subjects(outbox) == mails


def test_the_record_json_can_be_the_metadata_source(app, import_client, records_url, bagit_zip, monkeypatch):
    """`OAREPO_ROCRATE_IMPORT_METADATA = "record-json"` takes the record's own API JSON instead of the crate."""
    monkeypatch.setitem(app.config, "OAREPO_ROCRATE_IMPORT_METADATA", "record-json")

    response = post(import_client, records_url, bagit_zip, ZIP)

    assert response.status_code == 201, response.data
    assert response.get_json()["record"]["metadata"]["title"] == "Test"  # the record's title, not the crate's


@pytest.fixture
def worker_retries(app, monkeypatch):
    """Let an eager task retry the way it does in a worker.

    Two things stand in the way in eager mode, and a worker has neither:

    - flask-celeryext's `AppContextTask.__call__` calls `Task.__call__`, which pushes a fresh request, so
      `Task.retry` takes the task for one called directly and re-raises the error. A worker installs
      celery's stack protection first (`setup_worker_optimizations`), which runs the task on the request
      the tracer pushed;
    - `task_eager_propagates` makes the tracer re-raise the `Retry` instead of running the task again.

    Both are undone after the test (`monkeypatch` restores `Task.__call__` and the setting).
    """
    from celery.app import task as celery_task
    from celery.app import trace

    monkeypatch.setitem(app.extensions["invenio-celery"].celery.conf, "task_eager_propagates", value=False)
    monkeypatch.setattr(celery_task.Task, "__call__", celery_task.Task.__call__)
    monkeypatch.setattr(celery_task.Task, "_stackprotected", False, raising=False)
    trace._install_stack_protection()


def replaced_before_the_task(server: RangeServer, bagit: bytes) -> None:
    server.content = bagit
    server.next_etags = ['"v1"', '"v2"']  # the request's HEAD, then the task's


def drops(times: int):
    """Serve a package larger than the preloaded tail, whose member reads drop their connection `times` times."""

    def setup(server: RangeServer, bagit: bytes) -> None:
        server.content = big_package()
        server.drop_member_reads("data/data.csv", times)

    return setup


@pytest.mark.parametrize(
    ("setup", "files", "mail", "text"),
    [
        pytest.param(
            lambda server, bagit: setattr(server, "content", wrong_sha512(bagit)),
            [],
            "RO-Crate import failed: Example Title",
            "does not match its checksum",
            id="sha512-mismatch",
        ),
        pytest.param(
            replaced_before_the_task,
            [],
            "RO-Crate import failed: Example Title",
            "the archive could not be read any more",
            id="replaced-before-the-task",
        ),
        # a dropped connection is retried (mid-write, where the files service swallows the error) ...
        pytest.param(drops(1), ["data.csv"], "RO-Crate import finished: A dataset", None, id="connection-dropped-once"),
        # ... and when the retries run out, the unwritten file is deleted and the failure mailed
        pytest.param(
            drops(100),
            [],
            "RO-Crate import failed: A dataset",
            "the archive could not be read any more",
            id="connection-always-drops",
        ),
    ],
)
def test_a_package_at_a_url_is_written_by_the_task(
    worker_retries, app, import_client, records_url, bagit_zip, remote, setup, files, mail, text
):
    setup(remote.archive, bagit_zip)

    with app.extensions["mail"].record_messages() as outbox:
        response = post_url(import_client, records_url)

    assert response.status_code == 201, response.data
    _, statuses, contents = imported(import_client, records_url, response.get_json()["record"]["id"])
    assert statuses == dict.fromkeys(files, "completed")
    if files:
        with zipfile.ZipFile(io.BytesIO(remote.archive.content)) as archive:
            assert contents == {"data.csv": archive.read("data/data.csv")}
    assert subjects(outbox) == [mail]
    if text:
        assert text in outbox[0].body


@pytest.mark.parametrize(
    ("query", "title"),
    [
        # the crate's metadata goes through the test model's DataCite stub; record.json is the record's own
        pytest.param("", "Example Title", id="from-the-crate"),
        pytest.param("?metadata=record-json", "Test", id="from-record-json"),
    ],
)
def test_an_aip_is_imported_like_a_bag(client, grant, records_url, record_url, record_with_file, query, title):
    """Its payload files and metadata come in; the repository members (versions, files.json, ...) are no files."""
    grant("aip-access")  # the holder downloads the AIP and imports it
    aip = export(client, record_url, ROCRATE_AIP_MIMETYPE)

    response = post(client, records_url, aip, ZIP, query)

    assert response.status_code == 201, response.data
    record, statuses, contents = imported(client, records_url, response.get_json()["record"]["id"])
    assert record["metadata"]["title"] == title
    assert statuses == {"data.csv": "completed"}
    assert contents == {"data.csv": FILE_CONTENT}


def test_an_aip_brings_back_the_hidden_flag_and_the_metadata_of_its_files(
    client, grant, db, search_clear, location, formats_model, service, draft_files_service, add_file, records_url
):
    """`files.json` restores what the crate does not say: which files were hidden, and their metadata."""
    draft = service.create(system_identity, {"metadata": {"title": "Test"}, "files": {"enabled": True}})
    add_file(draft.id, "shown.txt", b"shown")
    add_file(draft.id, "hidden.txt", b"hidden")
    add_file(draft.id, "photo.jpg", b"jpeg")
    file_records = {f.key: f for f in draft_files_service.list_files(system_identity, draft.id)._results}
    file_records["hidden.txt"].model.json = {**file_records["hidden.txt"].model.json, "access": {"hidden": True}}
    file_records["photo.jpg"].model.json = {**file_records["photo.jpg"].model.json, "metadata": {"title": "A photo"}}
    db.session.commit()
    formats_model.Draft.index.refresh()
    grant("aip-access")
    aip = export(client, f"{records_url}/{draft.id}/draft", ROCRATE_AIP_MIMETYPE)

    response = post(client, records_url, aip, ZIP)

    assert response.status_code == 201, response.data
    files = client.get(f"{records_url}/{response.get_json()['record']['id']}/draft/files").get_json()["entries"]
    by_key = {entry["key"]: entry for entry in files}
    assert set(by_key) == {"shown.txt", "hidden.txt", "photo.jpg"}
    assert by_key["hidden.txt"]["access"]["hidden"] is True
    assert not (by_key["shown.txt"].get("access") or {}).get("hidden")
    assert by_key["photo.jpg"]["metadata"] == {"title": "A photo"}


class CrateToRecordSchema(Schema):
    """A deployment's own mapping: the crate straight to the record's structure, no DataCite in between.

    That only `metadata` and `custom_fields` of its output pass is tested in `tests/rocrate/imports/
    test_resource.py`: the test model has no `pids` or `access` to show it here.
    """

    metadata = fields.Method("get_metadata")

    def get_metadata(self, root: dict) -> dict:
        """Return the record's metadata: here only the title, from the crate's `name`."""
        return {"title": f"Imported: {jsonld.text(root.get('name'))}"}


def test_a_crate_converted_by_an_own_schema_goes_to_the_identity_import(
    import_client, records_url, crate, formats_model, monkeypatch
):
    config = formats_model.RecordResource.rocrate_import
    monkeypatch.setattr(config, "schema_cls", CrateToRecordSchema)
    monkeypatch.setattr(config, "base_import", IDENTITY_IMPORT)

    response = post(import_client, records_url, crate, LD_JSON)

    assert response.status_code == 201, response.data
    record = response.get_json()["record"]
    assert record["metadata"]["title"] == "Imported: Example Title"  # no DataCite stub in between


def stating(**checksums: str):
    """Return an edit that replaces the checksums `data.csv`'s entity states."""

    def edit(document: dict) -> dict:
        entity = next(e for e in document["@graph"] if e.get("name") == "data.csv")
        for algorithm in ("md5", "sha1", "sha256", "sha512"):
            entity.pop(algorithm, None)
        entity.update(checksums)
        return document

    return edit


@pytest.mark.parametrize(
    ("edit", "status"),
    [
        # a checksum the import can not verify is not taken for an md5
        pytest.param(stating(sha256=hashlib.sha256(FILE_CONTENT).hexdigest()), "completed", id="sha256-only"),
        pytest.param(stating(sha512=hashlib.sha512(FILE_CONTENT).hexdigest()), "completed", id="right-sha512"),
        pytest.param(stating(sha512="0" * 128), None, id="wrong-sha512"),
    ],
)
def test_a_download_is_verified_against_the_checksum_of_its_algorithm(
    app, import_client, records_url, crate, edit, status
):
    document = edit(json.loads(crate))

    response = post(import_client, records_url, json.dumps(document).encode(), LD_JSON)

    assert response.status_code == 201, response.data
    _, statuses, _ = imported(import_client, records_url, response.get_json()["record"]["id"])
    assert statuses.get("data.csv") == status
