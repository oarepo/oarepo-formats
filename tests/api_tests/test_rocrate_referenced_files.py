# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""A record with a stored and a referenced (remote, `R` transfer) file, through the archives and back.

The archives carry the stored file and describe the remote one as a web-based `File` (its download URL as
`@id`, no member); the import writes the first from the archive and downloads the second, or - with
`?files=reference` and an allowed domain - keeps it a remote file.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
import responses
from invenio_access.permissions import system_identity

from oarepo_formats.rocrate.model import (
    ROCRATE_AIP_MIMETYPE,
    ROCRATE_BAGIT_MIMETYPE,
    ROCRATE_MIMETYPE,
    ROCRATE_ZIP_MIMETYPE,
)
from tests.api_tests.conftest import exported
from tests.api_tests.test_rocrate_import import export, imported, post, post_url
from tests.rocrate.documents import root_of
from tests.rocrate.imports.packages import RangeServer

STORED_CONTENT = b"a,b\n1,2\n"
REMOTE_CONTENT = b"CDF remote content"
REMOTE_HOST = "remote.example.org"
REMOTE_URL = f"https://{REMOTE_HOST}/big.nc"
FETCH_HOST = "127.0.0.1:5000"
"""The netloc of the exported crate's file URLs - the test app's own links, port included."""
STORED_URL = re.compile(rf"https://{re.escape(FETCH_HOST)}/api/.*/files/data\.csv/content$")
REFERENCED_URL = re.compile(rf"https://{re.escape(FETCH_HOST)}/api/.*/files/big\.nc/content$")


@pytest.fixture(scope="module")
def app_config(app_config):
    """Allow the fake hosts: for fetches, and for remote files (the source record's and the imports')."""
    app_config["RECORDS_RESOURCES_FILES_ALLOWED_DOMAINS"] = [FETCH_HOST, RangeServer.url.split("/")[2]]
    app_config["RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS"] = [REMOTE_HOST, FETCH_HOST]
    app_config["MAIL_SUPPRESS_SEND"] = True
    app_config["MAIL_DEFAULT_SENDER"] = "test@example.org"
    app_config["MAIL_DEFAULT_REPLY_TO"] = "test@example.org"
    return app_config


@pytest.fixture(scope="module")
def celery_config(celery_config):
    """Run the import's tasks in the request."""
    return {**celery_config, "task_always_eager": True, "task_eager_propagates": True}


@pytest.fixture(autouse=True)
def remote():
    """Fake the exported file URLs (what a fetch downloads) and a static archive host."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        hosts = SimpleNamespace(archive=RangeServer())
        mocked.add(responses.GET, STORED_URL, body=STORED_CONTENT)
        mocked.add(responses.GET, REFERENCED_URL, body=REMOTE_CONTENT)
        hosts.archive.register(mocked)
        yield hosts


@pytest.fixture
def mixed_record(db, search_clear, location, formats_model, service, add_file, draft_files_service):
    """Return a published record with a stored `data.csv` and a remote `big.nc`."""
    draft = service.create(system_identity, {"metadata": {"title": "Mixed"}, "files": {"enabled": True}})
    add_file(draft.id, "data.csv", STORED_CONTENT)
    draft_files_service.init_files(
        system_identity,
        draft.id,
        data=[{"key": "big.nc", "size": len(REMOTE_CONTENT), "transfer": {"type": "R", "url": REMOTE_URL}}],
    )
    record = service.publish(system_identity, draft.id)
    formats_model.Record.index.refresh()
    return record


@pytest.fixture
def mixed_url(formats_model, mixed_record) -> str:
    return f"{formats_model.RecordResourceConfig.url_prefix}/{mixed_record.id}"


def referenced_entity(metadata: dict) -> dict:
    """Return the `File` entity of the remote file, after checking that the root lists it."""
    (entity,) = (e for e in metadata["@graph"] if e.get("@type") == "File" and e.get("localPath") == "big.nc")
    assert {"@id": entity["@id"]} in root_of(metadata)["hasPart"]
    return entity


def transfers(client, records_url: str, record_id: str) -> dict[str, str]:
    """Return `{key: transfer type}` of the imported record's files (published, else the draft)."""
    base = f"{records_url}/{record_id}"
    if client.get(base).status_code != 200:
        base = f"{base}/draft"
    return {entry["key"]: entry["transfer"]["type"] for entry in client.get(f"{base}/files").get_json()["entries"]}


#
# export: the archives carry the stored file and reference the remote one
#
@pytest.mark.parametrize("accept", [ROCRATE_ZIP_MIMETYPE, ROCRATE_BAGIT_MIMETYPE])
def test_an_archive_carries_the_stored_file_and_references_the_remote_one(client, mixed_url, accept, tmp_path):
    archive = exported(client, mixed_url, accept, tmp_path)

    assert archive.read("data/data.csv") == STORED_CONTENT
    assert not any(name.endswith("big.nc") for name in archive.names)
    entity = referenced_entity(archive.metadata)
    assert REFERENCED_URL.match(entity["@id"])
    assert entity["contentSize"] == str(len(REMOTE_CONTENT))
    assert "contentUrl" not in entity  # where the content is stays the source repository's business
    if archive.bag is not None:
        assert archive.bag.is_valid()  # the remote file is in no manifest, and needs not be
        assert not any(path.endswith("big.nc") for path in archive.bag.payload_files())


def test_the_aip_references_the_remote_file_and_keeps_its_url(client, grant, mixed_url, tmp_path):
    grant("aip-access")
    archive = exported(client, mixed_url, ROCRATE_AIP_MIMETYPE, tmp_path)

    assert archive.bag.is_valid()
    assert not any(name.endswith("big.nc") for name in archive.names)
    entity = referenced_entity(archive.metadata)
    assert REFERENCED_URL.match(entity["@id"])
    assert entity["contentUrl"] == REMOTE_URL  # the preservation copy says where the content actually is
    entries = json.loads(archive.read("metadata/repository/files.json"))["entries"]
    assert set(entries) == {"data.csv", "big.nc"}
    assert entries["big.nc"]["transfer"] == {"type": "R", "url": REMOTE_URL}


#
# import: the stored file from the archive, the referenced one fetched or referenced
#
@pytest.mark.parametrize(
    ("query", "publish", "big_nc"),
    [
        # a downloaded file is a stored one once its content arrived: its transfer type becomes `L`
        pytest.param("", None, "L", id="fetched-by-default"),
        pytest.param("?publish", "scheduled", "L", id="published-after-the-fetch"),
        pytest.param("?files=reference", None, "R", id="referenced"),
        pytest.param("?files=reference&publish", "published", "R", id="referenced-published-in-the-request"),
    ],
)
def test_a_mixed_package_is_imported(import_client, records_url, mixed_url, query, publish, big_nc):
    package = export(import_client, mixed_url, ROCRATE_ZIP_MIMETYPE)

    response = post(import_client, records_url, package, "application/zip", query)

    assert response.status_code == 201, response.data
    body = response.get_json()
    assert body["import"]["publish"] == publish
    assert {entry["key"] for entry in body["import"]["files"]} == {"data.csv", "big.nc"}
    record_id = body["record"]["id"]
    record, statuses, contents = imported(import_client, records_url, record_id)
    assert record["is_published"] is bool(publish)
    assert statuses == {"data.csv": "completed", "big.nc": "completed"}
    assert transfers(import_client, records_url, record_id) == {"data.csv": "L", "big.nc": big_nc}
    assert contents["data.csv"] == STORED_CONTENT
    if big_nc == "L":
        assert contents["big.nc"] == REMOTE_CONTENT


def test_a_referenced_file_whose_domain_is_not_allowed_for_remote_files_is_downloaded(
    app, import_client, records_url, mixed_url, monkeypatch
):
    package = export(import_client, mixed_url, ROCRATE_ZIP_MIMETYPE)
    monkeypatch.setitem(app.config, "RECORDS_RESOURCES_FILES_ALLOWED_REMOTE_DOMAINS", [REMOTE_HOST])

    response = post(import_client, records_url, package, "application/zip", "?files=reference")

    assert response.status_code == 201, response.data
    record_id = response.get_json()["record"]["id"]
    assert transfers(import_client, records_url, record_id) == {"data.csv": "L", "big.nc": "L"}  # fetched
    assert imported(import_client, records_url, record_id)[2]["big.nc"] == REMOTE_CONTENT


def test_a_mixed_package_at_a_url_is_finished_by_the_import_task(import_client, records_url, mixed_url, remote):
    remote.archive.content = export(import_client, mixed_url, ROCRATE_BAGIT_MIMETYPE)

    response = post_url(import_client, records_url, "?publish")

    assert response.status_code == 201, response.data
    assert response.get_json()["import"]["publish"] == "scheduled"
    record, statuses, contents = imported(import_client, records_url, response.get_json()["record"]["id"])
    assert record["is_published"] is True
    assert statuses == {"data.csv": "completed", "big.nc": "completed"}
    assert contents == {"data.csv": STORED_CONTENT, "big.nc": REMOTE_CONTENT}


def test_a_detached_crate_can_reference_its_files(import_client, records_url, mixed_url):
    crate = export(import_client, mixed_url, ROCRATE_MIMETYPE)

    response = post(import_client, records_url, crate, "application/ld+json", "?files=reference&publish")

    assert response.status_code == 201, response.data
    assert response.get_json()["import"]["publish"] == "published"  # nothing to fetch: published at once
    record_id = response.get_json()["record"]["id"]
    assert transfers(import_client, records_url, record_id) == {"data.csv": "R", "big.nc": "R"}


def test_an_unknown_files_mode_is_refused(import_client, records_url, mixed_url):
    crate = export(import_client, mixed_url, ROCRATE_MIMETYPE)

    response = post(import_client, records_url, crate, "application/ld+json", "?files=copy")

    assert response.status_code == 400
    assert "'copy' is not one of fetch, reference" in response.get_data(as_text=True)


def test_a_broken_archive_does_not_take_the_downloads_with_it(app, import_client, records_url, mixed_url, remote):
    """The archive changes between the request and the task: its member fails, the download still runs."""
    remote.archive.content = export(import_client, mixed_url, ROCRATE_BAGIT_MIMETYPE)
    remote.archive.next_etags = ['"v1"', '"v2"']  # the request's HEAD, then the task's

    with app.extensions["mail"].record_messages() as outbox:
        response = post_url(import_client, records_url)

    assert response.status_code == 201, response.data
    _, statuses, contents = imported(import_client, records_url, response.get_json()["record"]["id"])
    assert statuses == {"big.nc": "completed"}
    assert contents == {"big.nc": REMOTE_CONTENT}
    assert [message.subject for message in outbox] == ["RO-Crate import failed: Example Title"]
    assert "data.csv" in outbox[0].body
