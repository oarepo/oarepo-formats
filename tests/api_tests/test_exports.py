# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""What the export formats share, through the REST API: refusals and limits, and the ZIP/BagIt behaviour.

The BagIt export is the RO-Crate ZIP's crate inside a bag, so the behaviour of the crate (a failing export
is left out, a record without files, a declared profile) is one test over both formats. Each format's own
package is tested in its module (`test_rocrate_zip.py`, `test_rocrate_bagit.py`, `test_rocrate_aip.py`).
"""

from __future__ import annotations

import pytest
from invenio_access.permissions import system_identity
from invenio_records_resources.services.errors import PermissionDeniedError
from invenio_records_resources.services.files.service import FileService

from oarepo_formats.readme.model import README_MIMETYPE
from oarepo_formats.rocrate.model import ROCRATE_AIP_MIMETYPE, ROCRATE_BAGIT_MIMETYPE, ROCRATE_ZIP_MIMETYPE
from tests.api_tests.conftest import exported
from tests.conftest import DATASET_V2_ZIP_PATH

ZIP = ROCRATE_ZIP_MIMETYPE
BAGIT = ROCRATE_BAGIT_MIMETYPE
AIP = ROCRATE_AIP_MIMETYPE
UNQUOTED_ROCRATE = "application/ld+json;profile=https://w3id.org/ro/crate"
"""The spec's own (unquoted) example: werkzeug cuts the URI at ":", so it can not select the export."""
README = "README.generated.md"


def deny_files(monkeypatch, app) -> None:
    def deny(*args, **kwargs):
        raise PermissionDeniedError("read_files")

    monkeypatch.setattr(FileService, "read_archive", deny)


def size_limit(monkeypatch, app) -> None:
    monkeypatch.setitem(app.config, "RECORDS_RESOURCES_ARCHIVE_DOWNLOAD_MAX_SIZE", 1)


@pytest.mark.parametrize(
    ("accept", "action", "target", "patch", "status"),
    [
        pytest.param(ZIP, None, "record", deny_files, 403, id="zip-files-not-readable"),
        pytest.param(BAGIT, None, "record", deny_files, 403, id="bagit-files-not-readable"),
        pytest.param(ZIP, None, "record", size_limit, 400, id="zip-over-the-size-limit"),
        pytest.param(BAGIT, None, "record", size_limit, 400, id="bagit-over-the-size-limit"),
        pytest.param(ZIP, "administration-access", "record", size_limit, 200, id="zip-admin-has-no-size-limit"),
        pytest.param(BAGIT, "administration-access", "record", size_limit, 200, id="bagit-admin-has-no-size-limit"),
        pytest.param(AIP, None, "record", None, 403, id="aip-needs-aip-access"),
        pytest.param(AIP, "aip-access", "record", size_limit, 200, id="aip-has-no-size-limit"),
        pytest.param(ZIP, None, "search", None, 406, id="zip-of-a-search"),
        pytest.param(BAGIT, None, "search", None, 406, id="bagit-of-a-search"),
        pytest.param(README_MIMETYPE, None, "search", None, 406, id="readme-of-a-search"),
        pytest.param(UNQUOTED_ROCRATE, None, "record", None, 406, id="unquoted-rocrate-profile"),
    ],
)
def test_an_export_is_refused_or_served_before_streaming(
    app, client, grant, formats_model, record_url, monkeypatch, tmp_path, accept, action, target, patch, status
):
    """A refusal is a JSON error answered before anything is streamed, not a broken archive."""
    if action:
        grant(action)
    if patch:
        patch(monkeypatch, app)
    url = record_url if target == "record" else formats_model.RecordResourceConfig.url_prefix

    if status == 200:
        archive = exported(client, url, accept, tmp_path)
        assert "ro-crate-metadata.json" in archive.names
        assert archive.bag is None or archive.bag.is_valid()
    else:
        response = client.get(url, headers={"Accept": accept})
        assert response.status_code == status, response.data
        assert response.mimetype == "application/json"


@pytest.mark.parametrize("accept", [ZIP, BAGIT])
def test_a_failing_export_is_left_out_and_recorded(
    client, formats_model, record_url, monkeypatch, caplog, tmp_path, accept
):
    """One broken format must not make the archive unavailable."""
    export = next(e for e in formats_model.exports if e.code == "dataset-v2-xml")

    def fail(obj):
        raise RuntimeError("stylesheet broken")

    monkeypatch.setattr(export.serializer, "serialize_object", fail)

    archive = exported(client, record_url, accept, tmp_path)

    assert DATASET_V2_ZIP_PATH not in archive.names
    assert DATASET_V2_ZIP_PATH not in archive.graph
    assert {"@id": DATASET_V2_ZIP_PATH} not in archive.graph["./"]["subjectOf"]
    assert {"@id": README} in archive.graph["./"]["subjectOf"]  # the other exports are still there
    assert "export 'dataset-v2-xml' of record" in caplog.text
    assert "stylesheet broken" in caplog.text
    if archive.bag:  # a bag records the failure as a provenance action, and stays valid
        assert archive.bag.is_valid()
        action = archive.graph["#export-dataset-v2-xml-failed"]
        assert action["actionStatus"] == {"@id": "http://schema.org/FailedActionStatus"}
        assert action["error"] == "RuntimeError: stylesheet broken"


@pytest.mark.parametrize("accept", [ZIP, BAGIT])
def test_a_record_without_files_is_archived_with_its_metadata(
    client, db, search_clear, formats_model, service, tmp_path, accept
):
    """A record with files disabled is archived, not refused: it has no files, only the metadata."""
    draft = service.create(system_identity, {"metadata": {"title": "Test"}, "files": {"enabled": False}})
    record = service.publish(system_identity, draft.id)
    formats_model.Record.index.refresh()

    archive = exported(client, f"{formats_model.RecordResourceConfig.url_prefix}/{record.id}", accept, tmp_path)

    assert "ro-crate-metadata.json" in archive.names
    assert not [name for name in archive.names if name.startswith("data/")]
    assert archive.bag is None or archive.bag.is_valid()


ROCRATE_PROFILE = {"@id": "https://example.org/profiles/rocrate/1.0", "name": "RO-Crate profile", "version": "1.0"}
BAGIT_PROFILE = {"@id": "https://example.org/profiles/bagit/1.0", "name": "BagIt profile", "version": "1.0"}


@pytest.mark.parametrize(
    ("accept", "config", "declared"),
    [
        pytest.param(ZIP, {"OAREPO_ROCRATE_PROFILE": ROCRATE_PROFILE}, [ROCRATE_PROFILE], id="zip-one-profile"),
        pytest.param(
            BAGIT,
            {"OAREPO_ROCRATE_PROFILE": ROCRATE_PROFILE, "OAREPO_ROCRATE_BAGIT_PROFILE": BAGIT_PROFILE},
            [ROCRATE_PROFILE, BAGIT_PROFILE],
            id="bagit-both-profiles",
        ),
        pytest.param(ZIP, {}, [], id="zip-no-profile"),
    ],
)
def test_a_configured_profile_is_declared(app, client, record_url, monkeypatch, tmp_path, accept, config, declared):
    for key, value in config.items():
        monkeypatch.setitem(app.config, key, value)

    graph = exported(client, record_url, accept, tmp_path).graph

    references = [{"@id": profile["@id"]} for profile in declared]
    # one profile is a single reference, more are a list
    assert graph["./"].get("conformsTo") == (references[0] if len(references) == 1 else references or None)
    for profile in declared:
        assert graph[profile["@id"]] == {**profile, "@type": ["CreativeWork", "Profile"]}
    # the descriptor still conforms to the RO-Crate specification, not to the profile
    assert graph["ro-crate-metadata.json"]["conformsTo"] == {"@id": "https://w3id.org/ro/crate/1.3"}
