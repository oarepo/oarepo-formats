# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""RO-Crate BagIt export through the REST API of a real oarepo-model model.

The bag is checked by bagit-python and the crate inside it by ro-crate-py, both independent readers. What
the bag shares with the other archives (refusals, limits, a failing export, a record without files, the
profiles) is in `test_exports.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from invenio_access.permissions import system_identity

from oarepo_formats.rocrate.model import ROCRATE_BAGIT_MIMETYPE
from tests.api_tests.conftest import exported
from tests.conftest import DATASET_V2_ZIP_PATH

COMPLETED = {"@id": "http://schema.org/CompletedActionStatus"}


def url_of(formats_model, record_id: str) -> str:
    return f"{formats_model.RecordResourceConfig.url_prefix}/{record_id}"


def test_the_bagit_export(client, record_url, record_with_file, tmp_path):
    """The bag: valid, its tag files and `bag-info.txt`, the crate inside it, and the record's history."""
    from rocrate.rocrate import ROCrate

    api = client.get(record_url, headers={"Accept": "application/json"}).json
    archive = exported(client, record_url, ROCRATE_BAGIT_MIMETYPE, tmp_path)
    bag = archive.bag

    # a valid bag, with both payload manifests and a tag manifest covering them
    assert bag.is_valid()
    assert bag.has_oxum()
    assert sorted(Path(path).name for path in bag.manifest_files()) == ["manifest-md5.txt", "manifest-sha512.txt"]
    assert sorted(Path(path).name for path in bag.tagmanifest_files()) == ["tagmanifest-sha512.txt"]
    assert sorted(bag.payload_files()) == sorted(
        [
            "data/README.generated.md",
            f"data/{DATASET_V2_ZIP_PATH}",
            "data/data/data.csv",
            "data/metadata/record.json",
            "data/ro-crate-metadata.json",
        ]
    )
    assert archive.read("data/data.csv") == b"a,b\n1,2\n"

    # bag-info.txt identifies the record and states its size
    payload = list(bag.payload_files())
    info = bag.info
    assert info["Internal-Sender-Identifier"] == record_with_file.id
    assert info["Bag-Group-Identifier"] == api["parent"]["id"]
    assert info["OARepo-Record-State"] == "published"
    assert info["External-Identifier"].startswith("urn:uuid:")
    assert info["External-Description"].startswith(f"RO-Crate BagIt export of the record {record_with_file.id}")
    assert info["Bag-Software-Agent"].startswith("oarepo-formats ")
    assert info["Payload-Oxum"] == f"{sum(archive.bag_path(p).stat().st_size for p in payload)}.{len(payload)}"
    assert info["Bagging-Date"]

    # the payload is the crate, readable by an independent reader
    crate = ROCrate(archive.bag_path("data"))
    assert crate.root_dataset.id == "./"
    assert [entity.id for entity in crate.root_dataset["hasPart"]] == [
        "data/data.csv",
        "README.generated.md",
        DATASET_V2_ZIP_PATH,
        "metadata/record.json",
    ]
    # ... carrying the record's own JSON, i.e. what the API returns for `application/json`
    assert json.loads(archive.read("metadata/record.json")) == api

    # the record's history and the packaging, as provenance actions
    graph = archive.graph
    created = graph["#record-created"]
    assert created["@type"] == "CreateAction"
    assert created["object"] == {"@id": "./"}
    assert created["endTime"] == api["created"]
    assert created["actionStatus"] == COMPLETED
    assert graph["#version-published"]["@type"] == "UpdateAction"
    assert graph["#bag-created"]["result"] == {"@id": "ro-crate-metadata.json"}
    assert graph["#bag-created"]["instrument"] == {"@id": "https://github.com/oarepo/oarepo-formats"}
    assert graph["#bag-created"]["actionStatus"] == COMPLETED


def test_a_file_key_with_a_space_and_a_non_ascii_character(
    client, db, search_clear, location, formats_model, service, add_file, tmp_path
):
    draft = service.create(system_identity, {"metadata": {"title": "Test"}, "files": {"enabled": True}})
    add_file(draft.id, "sp ace ž.txt", b"data")
    record = service.publish(system_identity, draft.id)
    formats_model.Record.index.refresh()

    archive = exported(client, url_of(formats_model, record.id), ROCRATE_BAGIT_MIMETYPE, tmp_path)

    assert archive.bag.is_valid()
    # the manifest keeps the space and the non-ASCII character (RFC 8493 encodes only %, CR, LF) ...
    assert b"  data/data/sp ace \xc5\xbe.txt\n" in archive.bag_path("manifest-sha512.txt").read_bytes()
    assert archive.read("data/sp ace ž.txt") == b"data"
    # ... while the crate's @id is percent-encoded
    assert "data/sp%20ace%20%C5%BE.txt" in archive.graph


def test_a_stored_md5_that_does_not_match_the_file_makes_the_bag_invalid(
    client, db, service, record_url, record_with_file, tmp_path, caplog
):
    """The manifest takes the stored checksum, so corrupted content in storage is detected, not hidden."""
    record = service.read(system_identity, record_with_file.id)._record
    record.files["data.csv"].object_version.file.checksum = "md5:" + "0" * 32
    db.session.commit()

    archive = exported(client, record_url, ROCRATE_BAGIT_MIMETYPE, tmp_path)

    assert not archive.bag.is_valid()
    assert "stored md5" in caplog.text


def test_a_draft_is_marked_as_such(client, formats_model, draft_with_file, tmp_path):
    url = f"{url_of(formats_model, draft_with_file.id)}/draft"

    archive = exported(client, url, ROCRATE_BAGIT_MIMETYPE, tmp_path)

    assert archive.bag.is_valid()
    assert archive.bag.info["OARepo-Record-State"] == "draft"
    assert "#version-published" not in archive.graph


def test_constant_bag_info_fields_do_not_repeat_the_exports_own(app, client, record_url, monkeypatch, tmp_path):
    monkeypatch.setitem(
        app.config,
        "OAREPO_ROCRATE_BAGIT_INFO",
        {"Source-Organization": "Example University", "Internal-Sender-Identifier": "ignored"},
    )

    archive = exported(client, record_url, ROCRATE_BAGIT_MIMETYPE, tmp_path)

    info = archive.bag_path("bag-info.txt").read_text()
    assert "Source-Organization: Example University\n" in info
    assert info.count("Internal-Sender-Identifier:") == 1  # the export's own field is not repeated
