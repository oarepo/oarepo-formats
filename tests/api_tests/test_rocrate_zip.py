# SPDX-FileCopyrightText: 2026 CESNET z.s.p.o
# SPDX-License-Identifier: MIT

"""RO-Crate ZIP export through the REST API of a real oarepo-model model.

What the ZIP shares with the other archives (refusals, limits, a failing export, a record without files,
a profile) is in `test_exports.py`.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

from flask import g
from invenio_access.permissions import system_identity

from oarepo_formats.rocrate.exports.zip import ROCrateZipSerializer, describe_member
from oarepo_formats.rocrate.model import ROCRATE_ZIP_MIMETYPE
from tests.api_tests.conftest import exported
from tests.conftest import DATASET_V2_SCHEMA, DATASET_V2_XML_MIMETYPE, DATASET_V2_ZIP_PATH

README = "README.generated.md"


def test_the_zip_export(client, record_url, tmp_path):
    """The archive: its layout, the crate's metadata, the included exports and the files."""
    from rocrate.rocrate import ROCrate

    archive = exported(client, record_url, ROCRATE_ZIP_MIMETYPE)
    graph = archive.graph

    # metadata first, then the metadata exports, then the record's files under data/; the default
    # RDM exports (DataCite XML, ...) are left out because this model does not have them
    assert archive.names == ["ro-crate-metadata.json", README, DATASET_V2_ZIP_PATH, "data/data.csv"]
    root = graph["./"]
    assert root["cite-as"] == "https://doi.org/10.82433/b09z-4k37"
    assert root["hasPart"] == [{"@id": "data/data.csv"}, {"@id": README}, {"@id": DATASET_V2_ZIP_PATH}]
    assert root["subjectOf"] == [{"@id": README}, {"@id": DATASET_V2_ZIP_PATH}]

    # the record's file, with the stored checksum and its download URL
    assert archive.read("data/data.csv") == b"a,b\n1,2\n"
    assert graph["data/data.csv"]["contentUrl"].endswith(f"{record_url}/files/data.csv/content")
    assert graph["data/data.csv"]["md5"] == hashlib.md5(b"a,b\n1,2\n", usedforsecurity=False).hexdigest()

    # an included export is exactly what the REST API returns for its format, described as about the root
    content = archive.read(DATASET_V2_ZIP_PATH)
    assert content == client.get(record_url, headers={"Accept": DATASET_V2_XML_MIMETYPE}).data
    assert graph[DATASET_V2_ZIP_PATH] == {
        "@id": DATASET_V2_ZIP_PATH,
        "@type": "File",
        "name": "Dataset XML v2",
        "description": "Exported automatically by the repository from the metadata of the dataset.",
        "encodingFormat": DATASET_V2_XML_MIMETYPE,
        "contentSize": str(len(content)),
        "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
        "about": {"@id": "./"},
        "conformsTo": {"@id": DATASET_V2_SCHEMA},  # the export's oai_schema
    }

    # the generated README, which declares no schema
    assert archive.read(README).decode().startswith("# DATASET NAME: Example Title\n")
    assert graph[README]["encodingFormat"] == "text/markdown"
    assert "conformsTo" not in graph[README]

    # an independent RO-Crate reader understands the archive
    (tmp_path / "crate.zip").write_bytes(archive.zip.fp.getvalue())
    crate = ROCrate(tmp_path / "crate.zip")
    assert [e.id for e in crate.root_dataset["hasPart"]] == ["data/data.csv", README, DATASET_V2_ZIP_PATH]


def test_draft_zip_follows_the_draft_files(client, formats_model, draft_with_file, add_file):
    draft_url = f"{formats_model.RecordResourceConfig.url_prefix}/{draft_with_file.id}/draft"
    first = client.get(draft_url, headers={"Accept": ROCRATE_ZIP_MIMETYPE})
    names = zipfile.ZipFile(io.BytesIO(first.data)).namelist()
    assert [n for n in names if n.startswith("data/")] == ["data/data.csv"]

    add_file(draft_with_file.id, "more.txt", b"more")

    # The ETag is the record revision, which adding a draft file does not change. A client
    # revalidating with it must still get the new archive: the record endpoint does not answer 304.
    second = client.get(draft_url, headers={"Accept": ROCRATE_ZIP_MIMETYPE, "If-None-Match": first.headers["ETag"]})
    assert second.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(second.data)).namelist()
    assert sorted(n for n in names if n.startswith("data/")) == ["data/data.csv", "data/more.txt"]


def serialize(app, client, record_url, serializer) -> tuple[zipfile.ZipFile, dict[str, dict]]:
    """Serialize the record with a serializer that is not registered on the model."""
    record = client.get(record_url, headers={"Accept": "application/json"}).json
    with app.test_request_context():
        g.identity = system_identity
        response = serializer.serialize_object(record)
        archive = zipfile.ZipFile(io.BytesIO(b"".join(response.response)))
    return archive, {e["@id"]: e for e in json.loads(archive.read("ro-crate-metadata.json"))["@graph"]}


def test_exports_can_be_left_out(app, client, record_url):
    archive, graph = serialize(app, client, record_url, ROCrateZipSerializer(include_exports={}))

    assert archive.namelist() == ["ro-crate-metadata.json", "data/data.csv"]
    assert "subjectOf" not in graph["./"]


def test_extra_members(app, client, record_url):
    """A subclass adds an archive member next to the included exports and describes it in the metadata."""

    class LicenseZipSerializer(ROCrateZipSerializer):
        def extra_members(self, obj, crate):
            members = list(super().extra_members(obj, crate))
            describe_member(crate, "LICENSE.txt", name="License")
            return [*members, ("LICENSE.txt", b"CC0")]

    archive, graph = serialize(app, client, record_url, LicenseZipSerializer())

    assert archive.namelist() == ["ro-crate-metadata.json", README, "LICENSE.txt", "data/data.csv"]
    assert archive.read("LICENSE.txt") == b"CC0"
    assert graph["./"]["hasPart"] == [{"@id": "data/data.csv"}, {"@id": README}, {"@id": "LICENSE.txt"}]
    assert graph["LICENSE.txt"] == {"@id": "LICENSE.txt", "@type": "File", "name": "License"}
